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

Step 4 (parallel isolation) backs criterion 2 by proving the *negative*: in
parallel mode neither persona sees the other's same-turn reply (the snapshot
isolation that makes sequential cross-talk a deliberate, mode-specific feature).

Two DISTINCT ``MockHarness`` instances are registered (one for CLAUDE, one for
CODEX) so each persona's recorded prompts are unambiguous: ``@arch`` is
CLAUDE-mock-backed and ``@critic`` is CODEX-mock-backed. Cross-talk/isolation
assertions read each mock's ``.calls`` independently.
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

# Markers that let us trace a persona's reply text back into a later prompt.
# Distinct per step so a marker from an earlier turn (legitimately present in
# later history) can never be mistaken for a same-turn cross-talk/isolation leak.
ARCH_MARKER = "ARCH-MARKER-7f3a"  # @arch's reply in the SEQUENTIAL turn (step 3)
ARCH_PARALLEL_MARKER = "ARCH-PAR-1d4e"  # @arch's reply in the PARALLEL turn (step 4)
CRITIC_PARALLEL_MARKER = "CRITIC-PAR-9b2c"  # @critic's reply in the PARALLEL turn (step 4)


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


def _drain_until(ws, *types: str) -> list[dict]:
    """Read frames until one whose ``type`` is in ``types``; return all collected."""
    frames: list[dict] = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] in types:
            return frames


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
    # Script @arch (claude mock) to emit a unique marker. In sequential mode the
    # LATER persona (@critic, codex mock) must see @arch's same-turn reply.
    mocks.claude.script_reply("@arch @critic", ARCH_MARKER)
    critic_calls_before = len(mocks.codex.calls)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=me, text="@arch @critic debate it", reply_mode="sequential")

    new_critic_calls = mocks.codex.calls[critic_calls_before:]
    assert new_critic_calls, "@critic should have run in the sequential turn"
    assert any(ARCH_MARKER in c.prompt for c in new_critic_calls), (
        "SEQUENTIAL cross-talk: @critic's prompt must contain @arch's same-turn "
        f"reply marker {ARCH_MARKER!r}; got prompts: "
        f"{[c.prompt for c in new_critic_calls]!r}"
    )

    # === STEP 4: PARALLEL isolation (criterion 2, the negative) ==============
    # Script both to emit FRESH, parallel-only markers; in parallel neither
    # prompt may contain the other's same-turn reply (snapshot isolation). The
    # trigger phrase is chosen NOT to contain the step-3 script key "@arch
    # @critic" so the mock never sees an ambiguous double-match.
    mocks.claude.script_reply("PARALLEL-RUN", ARCH_PARALLEL_MARKER)
    mocks.codex.script_reply("PARALLEL-RUN", CRITIC_PARALLEL_MARKER)
    arch_before = len(mocks.claude.calls)
    critic_before = len(mocks.codex.calls)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        # Mentions ordered @critic @arch so the text does NOT contain the step-3
        # script key "@arch @critic" (which would double-match the claude mock).
        _post(ws, author_id=me, text="@critic @arch PARALLEL-RUN now", reply_mode="parallel")

    arch_parallel = mocks.claude.calls[arch_before:]
    critic_parallel = mocks.codex.calls[critic_before:]
    assert arch_parallel and critic_parallel, "both personas should run in parallel"
    # Each persona's fresh same-turn marker must NOT appear in the other's prompt.
    assert not any(CRITIC_PARALLEL_MARKER in c.prompt for c in arch_parallel), (
        "PARALLEL isolation: @arch must NOT see @critic's same-turn reply"
    )
    assert not any(ARCH_PARALLEL_MARKER in c.prompt for c in critic_parallel), (
        "PARALLEL isolation: @critic must NOT see @arch's same-turn reply"
    )

    # === STEP 5: quote-reply injects an earlier message (criterion 2) ========
    # Pick an earlier human message and quote it in a new post tagging @arch; the
    # quoted text must appear as a blockquote in @arch's prompt.
    msgs = _messages(client, room)
    quoted = next(m for m in msgs if m["content"] == "@arch what stack?")
    arch_before = len(mocks.claude.calls)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(
            ws,
            author_id=me,
            text="@arch revisit this",
            reply_mode="sequential",
            quoted_ids=[quoted["id"]],
        )
    arch_quote_calls = mocks.claude.calls[arch_before:]
    assert arch_quote_calls, "@arch should have run for the quote-reply"
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

    arch_before = len(mocks.claude.calls)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=boss, text="@arch ship it", reply_mode="sequential")
    boss_calls = mocks.claude.calls[arch_before:]
    assert any(boss_note in c.prompt for c in boss_calls), (
        "Boss weighting: the weight note must appear in @arch's prompt"
    )

    arch_before = len(mocks.claude.calls)
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=me, text="@arch and you?", reply_mode="sequential")
    me_calls = mocks.claude.calls[arch_before:]
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
    # app instance on the SAME db + same mocks (an app "restart"): the full
    # transcript must survive, and a NEW post to @arch must RESUME its stored
    # harness session (mock records resume_session_id == the persisted id).
    pre_restart_msgs = _messages(client, room)
    assert len(pre_restart_msgs) > 1

    # The session id @arch is using is whatever the claude mock allocated/kept.
    arch_session_calls = [c for c in mocks.claude.calls if "@arch" in c.prompt]
    assert arch_session_calls, "sanity: @arch ran at least once before restart"

    app2 = create_app(e2e_settings, registry=_build_registry(mocks))
    client2 = TestClient(app2)

    # Transcript reloaded intact from the same db.
    reloaded = _messages(client2, room)
    assert [m["id"] for m in reloaded] == [m["id"] for m in pre_restart_msgs], (
        "after restart the full transcript must reload from the same db"
    )

    # A new post to @arch on the restarted app resumes the stored harness session.
    calls_before = len(mocks.claude.calls)
    with client2.websocket_connect(f"/ws/rooms/{room}") as ws:
        _post(ws, author_id=me, text="@arch still there?", reply_mode="sequential")
    post_restart_calls = mocks.claude.calls[calls_before:]
    arch_resumes = [c for c in post_restart_calls if "@arch" in c.prompt]
    assert arch_resumes, "@arch should run on the restarted app"
    resumed_id = arch_resumes[-1].resume_session_id
    assert resumed_id is not None and resumed_id.startswith("mock-"), (
        "RESTART/RESUME (FR-D2): after an app restart @arch must resume its "
        f"previously stored harness session id; got {resumed_id!r}"
    )
