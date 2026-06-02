"""KEYSTONE end-to-end integration test (Task 7.1).

Boots the FULL app via :func:`create_app` on a temp db + temp logs dir with a
:class:`MockHarness`-backed registry (never a real CLI, zero token spend), then
drives a complete conversation over REST + WebSocket and asserts REAL outcomes
(persisted replies, recorded prompts, resumed sessions) — not just status codes.

This single test proves the seven success criteria from ``goal.md``:

==============================================================================
goal.md success criterion                         | step(s) below proving it
------------------------------------------------------------------------------
1. Multi-persona group chat works                 | step 1 (setup) + step 2
2. Personas can genuinely collaborate             | step 3 (sequential cross-
   (sequential debate + quote-reply)              |   talk) + step 5 (quote)
3. Authorship carries weight (Boss weighting)     | step 6
4. Sessions are durable and restartable           | step 9 (restart + resume)
5. Context is manageable (control commands)        | step 7 (/compact)
6. Everything is transparent (streaming + logs)    | step 2 (event frames) +
                                                   |   step 8 (run log on disk)
7. Errors are trivial to locate (error card +     | step 8
   one-click log link)                            |
==============================================================================

Step 4 (parallel isolation) backs criterion 2 by proving BOTH the *positive*
(each persona genuinely emits its own distinct marker reply this turn) AND the
*negative* (neither persona's prompt contains the OTHER's same-turn reply — the
snapshot isolation that makes sequential cross-talk a deliberate, mode-specific
feature).

Two DISTINCT ``MockHarness`` instances are registered (one for CLAUDE, one for
CODEX) so each persona's recorded prompts are unambiguous: ``@arch`` is
CLAUDE-mock-backed and ``@critic`` is CODEX-mock-backed. Cross-talk/isolation
assertions read each mock's ``.calls`` independently.

Script-key discipline (avoids the false-pass defect this test once had)
-----------------------------------------------------------------------
A ``MockHarness`` raises ``HarnessError`` if MORE THAN ONE registered script
substring matches a prompt (an intentional ambiguity guard). Because the
orchestrator re-injects prior human-post text and prior persona replies into
every later persona's delta/prompt, a single long-lived mock would accumulate
script keys across steps: from step 4 on, ``@arch``'s prompt would contain
multiple registered keys, the guard would raise, and ``@arch`` would *silently
error instead of replying* — yet the recorded-prompt assertions (captured before
the error) would still pass, a FALSE PASS that did not actually prove parallel
collaboration. We fix this two ways and keep both as invariants:

* Each scripted step installs a FRESH ``MockHarness`` for the relevant provider
  (:func:`_install_mock`) carrying ONLY that step's script, so stale keys can
  never linger and double-match. (A fresh instance resets ``.calls``, which is
  fine: every step asserts on the current step's calls. Resume still works
  because the mock echoes back a provided ``resume_session_id`` regardless of its
  internal counter — proven for real in step 9.)
* Each step's trigger phrase / script key is a UNIQUE token that does not appear
  in any prior human post or prior reply that the orchestrator re-injects into
  history, so the single active key matches only the intended turn.

The test asserts the POSITIVE reply for every scripted persona (its marker shows
up in the streamed event frames AND in the persisted transcript) — that positive
assertion is precisely what catches a persona that silently errored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

import app.api.health as health_mod
from app.api.app_factory import create_app
from app.config.settings import Settings
from app.domain.models import Provider
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry
from app.persistence.repositories import SessionRepo

# Reply MARKERS (the text a scripted persona emits) — distinct per step so a
# marker from an earlier turn (legitimately present in later history) can never
# be mistaken for a same-turn cross-talk/isolation leak.
ARCH_MARKER = "ARCH-MARKER-7f3a"  # @arch's reply in the SEQUENTIAL turn (step 3)
ARCH_PARALLEL_MARKER = "ARCH-PAR-1d4e"  # @arch's reply in the PARALLEL turn (step 4)
CRITIC_PARALLEL_MARKER = "CRITIC-PAR-9b2c"  # @critic's reply in the PARALLEL turn (step 4)
ARCH_QUOTE_MARKER = "ARCH-QUOTE-3e5b"  # @arch's reply in the QUOTE turn (step 5)
ARCH_BOSS_MARKER = "ARCH-BOSS-8c1d"  # @arch's reply in the BOSS-weight turn (step 6)

# Script-key TRIGGER tokens (the substring the mock keys on). Each is a unique
# nonce that appears in EXACTLY ONE human post text and in no reply marker, so on
# the step it is scripted it is the only key the fresh mock holds AND the only
# match in the prompt. Kept separate from the markers above on purpose.
TRIG_SEQ = "SEQ-TRIG-2a"  # step 3 sequential debate
TRIG_PAR = "PAR-TRIG-3b"  # step 4 parallel run
TRIG_QUOTE = "QUOTE-TRIG-4c"  # step 5 quote-reply
TRIG_BOSS = "BOSS-TRIG-5d"  # step 6 boss-weight post


# -- harness wiring ----------------------------------------------------------


@dataclass
class Mocks:
    """The two distinct mock backends, exposed so the test can read their calls."""

    claude: MockHarness
    codex: MockHarness


def _build_registry(mocks: Mocks) -> BackendRegistry:
    """A registry with a SEPARATE MockHarness per provider (no real CLI)."""
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, mocks.claude)
    reg.register(Provider.CODEX, mocks.codex)
    # MOCK falls back to the claude mock; unused by this test's personas but kept
    # registered so a stray @mock persona could never hit a real CLI.
    reg.register(Provider.MOCK, mocks.claude)
    return reg


def _install_mock(app, mocks: Mocks, provider: Provider) -> MockHarness:
    """Install a FRESH MockHarness for ``provider`` on the live registry.

    Returns it (and updates ``mocks``) so each scripted step starts from a clean
    script set — stale keys from earlier steps can never linger and double-match
    the ambiguity guard. Replacing a backend on the same registry instance takes
    effect for the next turn because the orchestrator resolves the backend per
    turn via ``registry.get_backend(provider)``.
    """
    fresh = MockHarness()
    app.state.registry.register(provider, fresh)
    if provider is Provider.CLAUDE:
        mocks.claude = fresh
    elif provider is Provider.CODEX:
        mocks.codex = fresh
    return fresh


# -- REST helpers ------------------------------------------------------------


def _author_id(client: TestClient, name: str) -> str:
    authors = client.get("/authors").json()
    return next(a["id"] for a in authors if a["name"] == name)


def _create_persona(client: TestClient, *, name: str, handle: str, provider: str) -> dict:
    resp = client.post(
        "/personas",
        json={"name": name, "handle": handle, "provider": provider, "model": "m"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_room(client: TestClient, name: str) -> str:
    resp = client.post("/rooms", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _add_member(client: TestClient, room_id: str, persona_id: str) -> None:
    resp = client.post(f"/rooms/{room_id}/members", json={"persona_id": persona_id})
    assert resp.status_code == 201, resp.text


def _messages(client: TestClient, room_id: str) -> list[dict]:
    resp = client.get(f"/rooms/{room_id}/messages")
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- WS helpers --------------------------------------------------------------


_MAX_FRAMES = 500  # generous upper bound; a single turn streams far fewer frames.


def _drain_until(ws, *types: str) -> list[dict]:
    """Read frames until one whose ``type`` is in ``types``; return all collected.

    Fails loudly instead of deadlocking (I1): an UNEXPECTED ``{"type": "error"}``
    frame (a transport-level error, never an expected terminal here) aborts the
    drain with a clear message, and a hard ``_MAX_FRAMES`` ceiling guards against
    a stream that never reaches a terminal type. Callers that legitimately expect
    an ``error`` terminal must pass ``"error"`` in ``types``.
    """
    frames: list[dict] = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] in types:
            return frames
        if frame["type"] == "error":
            raise AssertionError(
                f"drain hit an unexpected transport error frame: {frame!r}; "
                f"collected so far: {frames!r}"
            )
        if len(frames) > _MAX_FRAMES:
            raise AssertionError(
                f"drain exceeded {_MAX_FRAMES} frames without seeing {types!r}; "
                f"last frame: {frame!r}"
            )


def _persona_stream_text(frames: list[dict], persona_id: str) -> str:
    """Concatenate the text of all ``event`` text-deltas streamed for ``persona_id``."""
    parts: list[str] = []
    for f in frames:
        if f["type"] == "event" and f["persona_id"] == persona_id:
            event = f["event"]
            if event.get("kind") == "text":
                parts.append(event["text"])
    return "".join(parts)


def _persisted_reply_text(client: TestClient, room_id: str, persona_id: str) -> str:
    """The content of ``persona_id``'s LATEST persisted persona message in the room."""
    msgs = _messages(client, room_id)
    replies = [m for m in msgs if m["author_kind"] == "persona" and m["author_ref"] == persona_id]
    return replies[-1]["content"] if replies else ""


def _post(ws, *, author_id: str, text: str, reply_mode: str, quoted_ids=None) -> list[dict]:
    ws.send_json(
        {
            "type": "post",
            "author_id": author_id,
            "text": text,
            "reply_mode": reply_mode,
            "quoted_ids": list(quoted_ids or []),
        }
    )
    return _drain_until(ws, "turn_complete")


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def e2e_settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(data_dir=data_dir, db_path=data_dir / "team.db", logs_dir=data_dir / "logs")


@pytest.fixture(autouse=True)
def _fake_clis(monkeypatch):
    # Health route resolves CLIs via shutil.which — pretend both are present so
    # the app is "ready" without a real claude/codex install.
    monkeypatch.setattr(health_mod.shutil, "which", lambda name: f"/usr/bin/{name}")


# -- the keystone test -------------------------------------------------------


def test_full_conversation_proves_seven_success_criteria(e2e_settings):
    mocks = Mocks(claude=MockHarness(), codex=MockHarness())
    app = create_app(e2e_settings, registry=_build_registry(mocks))
    client = TestClient(app)

    # === STEP 1: multi-persona group chat setup (criterion 1) ================
    # Seeded Me/Boss authors exist; create two personas on DIFFERENT (mock-backed)
    # providers so per-persona prompts are unambiguous, then a room with both.
    me = _author_id(client, "Me")
    boss = _author_id(client, "Boss")
    arch = _create_persona(client, name="Architect", handle="arch", provider="claude")
    critic = _create_persona(client, name="Critic", handle="critic", provider="codex")
    room = _create_room(client, "war-room")
    _add_member(client, room, arch["id"])
    _add_member(client, room, critic["id"])

    members = client.get(f"/rooms/{room}/members").json()
    assert {m["handle"] for m in members} == {"arch", "critic"}

    # === STEP 2: tagging + reply, persisted with a run_id (criterion 1 + 6) ==
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(ws, author_id=me, text="@arch what stack?", reply_mode="sequential")

    event_frames = [f for f in frames if f["type"] == "event"]
    assert event_frames, "expected streamed event frames (live token streaming)"
    assert all(f["persona_id"] == arch["id"] for f in event_frames)
    assert any(f["event"]["kind"] == "text" for f in event_frames), "expected a text delta"

    msgs = _messages(client, room)
    arch_replies = [
        m for m in msgs if m["author_kind"] == "persona" and m["author_ref"] == arch["id"]
    ]
    assert arch_replies, "@arch's reply must be persisted"
    assert arch_replies[-1]["run_id"], "persisted reply must carry a run_id (one-hop traceability)"
    # @arch's prompt (claude mock) saw the directed line.
    assert any("@arch" in c.prompt for c in mocks.claude.calls)

    # === STEP 3: SEQUENTIAL debate cross-talk (criterion 2) ==================
    # FRESH mocks so only this step's key is live. Script @arch (claude mock) to
    # emit ARCH_MARKER when it sees the unique TRIG_SEQ token; @critic (codex) just
    # echoes by default. In sequential mode @arch ACTUALLY replies AND the LATER
    # persona (@critic) must see @arch's same-turn reply.
    _install_mock(app, mocks, Provider.CLAUDE)
    _install_mock(app, mocks, Provider.CODEX)
    mocks.claude.script_reply(TRIG_SEQ, ARCH_MARKER)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(
            ws, author_id=me, text=f"@arch @critic debate it {TRIG_SEQ}", reply_mode="sequential"
        )

    # POSITIVE: @arch genuinely emitted its marker (streamed AND persisted). This
    # is the assertion the old test lacked — it would have caught @arch silently
    # erroring out via the ambiguity guard.
    assert not any(f["type"] == "error_card" for f in frames), (
        f"step 3: no persona should have errored; frames: {frames!r}"
    )
    assert ARCH_MARKER in _persona_stream_text(frames, arch["id"]), (
        "SEQUENTIAL: @arch must actually emit its marker in the streamed frames"
    )
    assert ARCH_MARKER in _persisted_reply_text(client, room, arch["id"]), (
        "SEQUENTIAL: @arch's marker reply must be persisted in the transcript"
    )
    # CROSS-TALK: @critic's recorded prompt must contain @arch's same-turn reply.
    new_critic_calls = mocks.codex.calls
    assert new_critic_calls, "@critic should have run in the sequential turn"
    assert any(ARCH_MARKER in c.prompt for c in new_critic_calls), (
        "SEQUENTIAL cross-talk: @critic's prompt must contain @arch's same-turn "
        f"reply marker {ARCH_MARKER!r}; got prompts: "
        f"{[c.prompt for c in new_critic_calls]!r}"
    )

    # === STEP 4: PARALLEL collaboration + isolation (criterion 2) ============
    # FRESH mocks each carrying ONLY this step's key (TRIG_PAR). Script BOTH
    # personas to emit distinct parallel-only markers. We prove BOTH directions of
    # the contract: (positive) each persona ACTUALLY replies with its own marker
    # this turn, and (negative) neither persona's prompt contains the OTHER's
    # same-turn reply (snapshot isolation).
    _install_mock(app, mocks, Provider.CLAUDE)
    _install_mock(app, mocks, Provider.CODEX)
    mocks.claude.script_reply(TRIG_PAR, ARCH_PARALLEL_MARKER)
    mocks.codex.script_reply(TRIG_PAR, CRITIC_PARALLEL_MARKER)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(
            ws, author_id=me, text=f"@critic @arch {TRIG_PAR} now", reply_mode="parallel"
        )

    arch_parallel = mocks.claude.calls
    critic_parallel = mocks.codex.calls
    assert arch_parallel and critic_parallel, "both personas should run in parallel"
    # POSITIVE: both personas genuinely replied with their distinct markers, in the
    # streamed frames AND in the persisted transcript, and nobody errored.
    assert not any(f["type"] == "error_card" for f in frames), (
        f"step 4: no persona should have errored in the parallel turn; frames: {frames!r}"
    )
    assert ARCH_PARALLEL_MARKER in _persona_stream_text(frames, arch["id"]), (
        "PARALLEL: @arch must actually emit its marker this turn (streamed)"
    )
    assert CRITIC_PARALLEL_MARKER in _persona_stream_text(frames, critic["id"]), (
        "PARALLEL: @critic must actually emit its marker this turn (streamed)"
    )
    par_msgs = _messages(client, room)
    par_contents = [m["content"] for m in par_msgs if m["author_kind"] == "persona"]
    assert any(ARCH_PARALLEL_MARKER in c for c in par_contents), (
        "PARALLEL: @arch's marker reply must be persisted"
    )
    assert any(CRITIC_PARALLEL_MARKER in c for c in par_contents), (
        "PARALLEL: @critic's marker reply must be persisted"
    )
    # NEGATIVE (both directions): each persona's same-turn marker must NOT appear
    # in the other's recorded prompt.
    assert not any(CRITIC_PARALLEL_MARKER in c.prompt for c in arch_parallel), (
        "PARALLEL isolation: @arch must NOT see @critic's same-turn reply"
    )
    assert not any(ARCH_PARALLEL_MARKER in c.prompt for c in critic_parallel), (
        "PARALLEL isolation: @critic must NOT see @arch's same-turn reply"
    )

    # === STEP 5: quote-reply injects an earlier message (criterion 2) ========
    # Pick an earlier human message and quote it in a new post tagging @arch; the
    # quoted text must appear as a blockquote in @arch's prompt AND @arch must
    # genuinely reply (no silent error). FRESH claude mock keyed on TRIG_QUOTE.
    _install_mock(app, mocks, Provider.CLAUDE)
    mocks.claude.script_reply(TRIG_QUOTE, ARCH_QUOTE_MARKER)
    msgs = _messages(client, room)
    quoted = next(m for m in msgs if m["content"] == "@arch what stack?")
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(
            ws,
            author_id=me,
            text=f"@arch revisit this {TRIG_QUOTE}",
            reply_mode="sequential",
            quoted_ids=[quoted["id"]],
        )
    arch_quote_calls = mocks.claude.calls
    assert arch_quote_calls, "@arch should have run for the quote-reply"
    # POSITIVE: @arch genuinely replied (no error_card, marker streamed + persisted).
    assert not any(f["type"] == "error_card" for f in frames), (
        f"step 5: @arch must not have errored on the quote-reply; frames: {frames!r}"
    )
    assert ARCH_QUOTE_MARKER in _persona_stream_text(frames, arch["id"]), (
        "quote-reply: @arch must actually emit its marker (streamed)"
    )
    assert ARCH_QUOTE_MARKER in _persisted_reply_text(client, room, arch["id"]), (
        "quote-reply: @arch's marker reply must be persisted"
    )
    # And the quoted text must be injected as a blockquote in @arch's prompt.
    assert any("  > @arch what stack?" in c.prompt for c in arch_quote_calls), (
        "quote-reply: @arch's prompt must contain the quoted text as a blockquote; "
        f"got: {[c.prompt for c in arch_quote_calls]!r}"
    )

    # === STEP 6: Boss weighting (criterion 3) ================================
    # A Boss post (weight_enabled) injects the Boss weight note into the prompt;
    # a Me post does not.
    boss_author = client.get("/authors").json()
    boss_note = next(a for a in boss_author if a["id"] == boss)["weight_note"]
    assert boss_note, "seeded Boss must carry a weight note"

    # FRESH claude mock keyed on TRIG_BOSS so @arch genuinely replies AND we can
    # assert the Boss weight note reaches its prompt.
    _install_mock(app, mocks, Provider.CLAUDE)
    mocks.claude.script_reply(TRIG_BOSS, ARCH_BOSS_MARKER)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(
            ws, author_id=boss, text=f"@arch ship it {TRIG_BOSS}", reply_mode="sequential"
        )
    boss_calls = mocks.claude.calls
    # POSITIVE: @arch genuinely replied (no error_card, marker streamed + persisted).
    assert not any(f["type"] == "error_card" for f in frames), (
        f"step 6: @arch must not have errored on the Boss post; frames: {frames!r}"
    )
    assert ARCH_BOSS_MARKER in _persona_stream_text(frames, arch["id"]), (
        "Boss weighting: @arch must actually emit its marker on the Boss post (streamed)"
    )
    assert ARCH_BOSS_MARKER in _persisted_reply_text(client, room, arch["id"]), (
        "Boss weighting: @arch's marker reply must be persisted"
    )
    assert any(boss_note in c.prompt for c in boss_calls), (
        "Boss weighting: the weight note must appear in @arch's prompt"
    )

    # A Me post (fresh mock, default echo) must NOT carry the Boss weight note.
    _install_mock(app, mocks, Provider.CLAUDE)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=me, text="@arch and you?", reply_mode="sequential")
    me_calls = mocks.claude.calls
    assert me_calls and not any(boss_note in c.prompt for c in me_calls), (
        "a Me post must NOT carry the Boss weight note"
    )

    # === STEP 7: session control /compact streams + completes (criterion 5) ==
    # @arch (claude mock) has an existing session; /compact must stream events
    # and end with command_complete.
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        ws.send_json({"type": "command", "persona_id": arch["id"], "command": "/compact"})
        cmd_frames = _drain_until(ws, "command_complete")
    cmd_events = [f for f in cmd_frames if f["type"] == "event"]
    assert cmd_events, "/compact must stream at least one event"
    assert all(f["persona_id"] == arch["id"] for f in cmd_events)
    assert cmd_frames[-1] == {"type": "command_complete"}

    # === STEP 8: error visibility — error card + fetchable log (criterion 7) =
    # Swap @critic's backend (codex) for one whose run raises; a post tagging
    # @critic must yield an error_card with a run_id + log_url, and GET log_url
    # must return that run's JSONL.
    class RaisingHarness(MockHarness):
        async def run(self, spec):  # type: ignore[override]
            self.calls.append(spec)
            raise RuntimeError("intentional backend boom")
            yield  # pragma: no cover - makes this an async generator

    raising = RaisingHarness()
    app.state.registry.register(Provider.CODEX, raising)

    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        frames = _post(ws, author_id=me, text="@critic break", reply_mode="sequential")
    cards = [f for f in frames if f["type"] == "error_card"]
    assert len(cards) == 1, f"expected exactly one error card; got {frames!r}"
    card = cards[0]
    assert card["persona_id"] == critic["id"]
    assert card["error_kind"] == "RuntimeError"
    assert "boom" in card["message"]
    assert card["command_redacted"], "error card must carry the redacted command"
    run_id = card["run_id"]
    assert run_id, "error card must carry a run_id (one-hop traceability)"
    assert card["log_url"] == f"/runs/{run_id}/log"

    log_resp = client.get(card["log_url"])
    assert log_resp.status_code == 200, "the run log must be fetchable via the card's link"
    # The log is the run's JSONL on disk; it must be valid line-delimited JSON.
    lines = [ln for ln in log_resp.text.splitlines() if ln.strip()]
    assert lines, "run log JSONL must not be empty"
    for ln in lines:
        json.loads(ln)  # raises if not valid JSON — the log is structured per-event

    # === STEP 9: RESTART / RESUME durability (criterion 4) ===================
    # @arch persisted a harness session_id over the conversation. Build a SECOND
    # app instance on the SAME db (an app "restart"): the full transcript must
    # survive, and a NEW post to @arch must RESUME its OWN stored harness session
    # (resume_session_id == EXACTLY the persisted id — not merely "mock-"-shaped).
    pre_restart_msgs = _messages(client, room)
    assert len(pre_restart_msgs) > 1

    # Capture @arch's STORED harness session id from the persisted PersonaSession
    # (the authoritative source the restarted app will read to resume). This is
    # the id we demand strict equality against after the restart.
    stored_session = SessionRepo(app.state.db).get(room, arch["id"])
    assert stored_session is not None, "sanity: @arch must have a persisted session"
    stored_arch_session_id = stored_session.harness_session_id
    assert stored_arch_session_id is not None, "sanity: @arch's stored session id must be set"

    # Restart: fresh app + fresh registry. Use a FRESH claude mock so its .calls
    # are isolated to the post-restart turn; the mock echoes back whatever
    # resume_session_id it receives, so resume can only succeed if the restarted
    # app loaded the stored id from the db (not from any in-memory mock counter).
    restart_mocks = Mocks(claude=MockHarness(), codex=MockHarness())
    app2 = create_app(e2e_settings, registry=_build_registry(restart_mocks))
    client2 = TestClient(app2)

    # Transcript reloaded intact from the same db.
    reloaded = _messages(client2, room)
    assert [m["id"] for m in reloaded] == [m["id"] for m in pre_restart_msgs], (
        "after restart the full transcript must reload from the same db"
    )

    # A new post to @arch on the restarted app resumes the stored harness session.
    with client2.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=me, text="@arch still there?", reply_mode="sequential")
    arch_resumes = [c for c in restart_mocks.claude.calls if "@arch" in c.prompt]
    assert arch_resumes, "@arch should run on the restarted app"
    resumed_id = arch_resumes[-1].resume_session_id
    assert resumed_id == stored_arch_session_id, (
        "RESTART/RESUME (FR-D2): after an app restart @arch must resume its OWN "
        f"previously stored harness session id {stored_arch_session_id!r}; "
        f"got {resumed_id!r}"
    )
