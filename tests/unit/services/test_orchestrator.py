"""Tests for the ChatOrchestrator — the keystone that ties everything together.

No real CLI: every backend is a :class:`MockHarness`. To assert per-persona
prompts unambiguously (the mock has no notion of persona ids), we register a
*distinct* mock instance per provider and give the two personas different
providers, both backed by mocks. ``mock_b.last_prompt`` is then unambiguously B's.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.events import RunDone, RunError, TextDelta
from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Persona,
    PersonaSession,
    Provider,
    ReplyMode,
    Room,
)
from app.harness.base import RunSpec
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry
from app.persistence.db import Database
from app.persistence.repositories import (
    AuthorRepo,
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    RunRepo,
    SessionRepo,
)
from app.persistence.run_log import RunLogStore
from app.services.orchestrator import ChatOrchestrator


class _RaisingBackend:
    """A backend whose ``run`` raises a typed harness error after yielding one delta."""

    name = "raising"
    supported_commands: set[str] = set()

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[RunSpec] = []

    async def run(self, spec: RunSpec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        yield TextDelta(text="partial-before-boom")
        raise self._exc

    async def send_command(self, session_id: str, command: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


class _AwaitingBackend:
    """A test-only backend with real await points so parallel workers interleave.

    Implements the :class:`AgentBackend` contract. ``run`` yields several
    ``TextDelta``s with ``await asyncio.sleep(0)`` between events (so the event
    loop hands control to a sibling worker), then a terminal ``RunDone``. It can
    optionally raise mid-stream, and records — via a ``cancelled`` flag set in a
    ``finally`` — whether its ``run`` generator was closed/cancelled before
    finishing (proving early-stop worker cancellation reaches the backend).
    """

    supported_commands: set[str] = set()

    def __init__(
        self,
        name: str,
        reply: str,
        *,
        n_deltas: int = 4,
        raise_after: int | None = None,
    ) -> None:
        self.name = name
        self._reply = reply
        self._n_deltas = n_deltas
        self._raise_after = raise_after
        self.calls: list[RunSpec] = []
        self.started = False
        self.finished = False
        self.cancelled = False

    async def run(self, spec: RunSpec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        self.started = True
        try:
            for i in range(self._n_deltas):
                await asyncio.sleep(0)
                yield TextDelta(text=f"{self._reply}-{i}")
                if self._raise_after is not None and i >= self._raise_after:
                    raise RuntimeError(f"{self.name}-boom")
            yield RunDone(session_id=f"sess-{self.name}")
            self.finished = True
        finally:
            if not self.finished:
                # closed early (GeneratorExit / raise) before terminal RunDone
                self.cancelled = True

    async def send_command(self, session_id: str, command: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover


class _ControlRaisingBackend:
    """A backend whose ``send_command`` yields one delta then raises a typed
    harness error mid-stream — to exercise the send_control error branch.
    """

    name = "control-raising"
    supported_commands: set[str] = {"/compact", "/clear"}

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[tuple[str, str]] = []

    async def run(self, spec: RunSpec):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover

    async def send_command(self, session_id: str, command: str):  # type: ignore[no-untyped-def]
        self.calls.append((session_id, command))
        yield TextDelta(text="partial-before-boom")
        raise self._exc


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture
def repos(db):
    return {
        "persona": PersonaRepo(db),
        "author": AuthorRepo(db),
        "room": RoomRepo(db),
        "message": MessageRepo(db),
        "session": SessionRepo(db),
        "run": RunRepo(db),
    }


@pytest.fixture
def mock_a():
    return MockHarness()


@pytest.fixture
def mock_b():
    return MockHarness()


@pytest.fixture
def registry(mock_a, mock_b):
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, mock_a)
    reg.register(Provider.CODEX, mock_b)
    return reg


@pytest.fixture
def run_log(tmp_path):
    return RunLogStore(tmp_path / "runs")


@pytest.fixture
def me(repos):
    return repos["author"].create(HumanAuthor(name="Me", weight_enabled=False))


@pytest.fixture
def boss(repos):
    return repos["author"].create(
        HumanAuthor(name="Boss", weight_note="Leadership input — weight heavily.")
    )


@pytest.fixture
def room(repos):
    return repos["room"].create(Room(name="General"))


@pytest.fixture
def persona_a(repos, room):
    p = repos["persona"].create(
        Persona(name="Alpha", handle="alpha", provider=Provider.CLAUDE, model="opus")
    )
    repos["room"].add_member(room.id, p.id)
    return p


@pytest.fixture
def persona_b(repos, room):
    p = repos["persona"].create(
        Persona(name="Beta", handle="beta", provider=Provider.CODEX, model="gpt")
    )
    repos["room"].add_member(room.id, p.id)
    return p


@pytest.fixture
def orch(repos, run_log, registry):
    return ChatOrchestrator(
        persona_repo=repos["persona"],
        author_repo=repos["author"],
        room_repo=repos["room"],
        message_repo=repos["message"],
        session_repo=repos["session"],
        run_repo=repos["run"],
        run_log=run_log,
        registry=registry,
    )


async def _drain(agen):
    return [item async for item in agen]


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_untagged_message_runs_nothing_but_persists(orch, repos, room, me, mock_a, mock_b):
    events = await _drain(
        orch.post_message(
            room.id, author=me, text="just thinking out loud", reply_mode=ReplyMode.SEQUENTIAL
        )
    )
    assert events == []
    assert mock_a.calls == [] and mock_b.calls == []
    msgs = repos["message"].list_for_room(room.id)
    assert len(msgs) == 1
    assert msgs[0].author_kind is AuthorKind.HUMAN
    assert msgs[0].content == "just thinking out loud"


@pytest.mark.asyncio
async def test_single_persona_runs_and_persists_reply_and_session(
    orch, repos, room, me, persona_a, mock_a
):
    mock_a.script_reply("alpha", "HELLO-FROM-ALPHA")
    events = await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    # at least the persona's text delta is yielded, tagged with its id
    assert any(pid == persona_a.id for pid, _ in events)
    assert len(mock_a.calls) == 1

    msgs = repos["message"].list_for_room(room.id)
    assert len(msgs) == 2
    reply = msgs[1]
    assert reply.author_kind is AuthorKind.PERSONA
    assert reply.author_ref == persona_a.id
    assert reply.content == "HELLO-FROM-ALPHA"
    assert reply.run_id is not None

    session = repos["session"].get(room.id, persona_a.id)
    assert session is not None
    assert session.harness_session_id == "mock-1"
    # pointer is the human message (latest at turn start), NOT the persona's own reply
    assert session.last_seen_message_id == msgs[0].id


@pytest.mark.asyncio
async def test_second_message_resumes_saved_session(orch, repos, room, me, persona_a, mock_a):
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha first", reply_mode=ReplyMode.SEQUENTIAL)
    )
    saved = repos["session"].get(room.id, persona_a.id).harness_session_id
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha second", reply_mode=ReplyMode.SEQUENTIAL)
    )
    assert mock_a.calls[-1].resume_session_id == saved


@pytest.mark.asyncio
async def test_sequential_b_sees_a_reply(
    orch, repos, room, me, persona_a, persona_b, mock_a, mock_b
):
    mock_a.script_reply("alpha", "ALPHA-SAYS-X")
    await _drain(
        orch.post_message(
            room.id, author=me, text="@alpha @beta discuss", reply_mode=ReplyMode.SEQUENTIAL
        )
    )
    b_prompt = mock_b.last_prompt
    assert b_prompt is not None
    assert "ALPHA-SAYS-X" in b_prompt


@pytest.mark.asyncio
async def test_parallel_b_does_not_see_a_reply(
    orch, repos, room, me, persona_a, persona_b, mock_a, mock_b
):
    mock_a.script_reply("alpha", "ALPHA-SAYS-X")
    mock_b.script_reply("beta", "BETA-SAYS-Y")
    await _drain(
        orch.post_message(
            room.id, author=me, text="@alpha @beta discuss", reply_mode=ReplyMode.PARALLEL
        )
    )
    assert "ALPHA-SAYS-X" not in (mock_b.last_prompt or "")
    assert "BETA-SAYS-Y" not in (mock_a.last_prompt or "")
    # both replies still persisted
    contents = [m.content for m in repos["message"].list_for_room(room.id)]
    assert "ALPHA-SAYS-X" in contents and "BETA-SAYS-Y" in contents


@pytest.mark.asyncio
async def test_quote_of_already_seen_message_renders_as_blockquote(
    orch, repos, room, me, persona_a, mock_a
):
    # Script a fixed reply so the mock's default echo does not re-introduce the
    # quoted text into later deltas and muddy the count.
    mock_a.script_reply("alpha", "OK-NOTED")
    # First turn: persona sees the message and replies; its pointer now sits at it.
    await _drain(
        orch.post_message(
            room.id, author=me, text="@alpha QUOTE-TARGET-TEXT", reply_mode=ReplyMode.SEQUENTIAL
        )
    )
    seen = repos["message"].list_for_room(room.id)[0]
    # Second turn: quote that already-seen message -> it is NOT in the new delta,
    # so it must be rendered as a blockquote.
    await _drain(
        orch.post_message(
            room.id,
            author=me,
            text="@alpha thoughts?",
            reply_mode=ReplyMode.SEQUENTIAL,
            quoted_ids=[seen.id],
        )
    )
    prompt = mock_a.last_prompt
    assert prompt is not None
    assert "  > @alpha QUOTE-TARGET-TEXT" in prompt  # rendered as a blockquote
    assert prompt.count("QUOTE-TARGET-TEXT") == 1  # not duplicated


@pytest.mark.asyncio
async def test_quote_in_delta_is_deduped_not_rendered_twice(
    orch, repos, room, me, persona_a, mock_a
):
    # The quoted message also falls in this persona's (first) delta -> dedup drops
    # the blockquote so the text appears exactly once (from the delta).
    await _drain(
        orch.post_message(room.id, author=me, text="context line", reply_mode=ReplyMode.SEQUENTIAL)
    )
    quoted = repos["message"].list_for_room(room.id)[0]
    await _drain(
        orch.post_message(
            room.id,
            author=me,
            text="@alpha thoughts?",
            reply_mode=ReplyMode.SEQUENTIAL,
            quoted_ids=[quoted.id],
        )
    )
    prompt = mock_a.last_prompt
    assert prompt is not None
    assert "  > context line" not in prompt  # NOT rendered as blockquote (deduped)
    assert prompt.count("context line") == 1  # appears once, from the delta


@pytest.mark.asyncio
async def test_boss_weight_note_appears_when_enabled(orch, repos, room, boss, persona_a, mock_a):
    await _drain(
        orch.post_message(room.id, author=boss, text="@alpha go", reply_mode=ReplyMode.SEQUENTIAL)
    )
    assert "Leadership input" in (mock_a.last_prompt or "")


@pytest.mark.asyncio
async def test_weight_note_absent_when_disabled(orch, repos, room, me, persona_a, mock_a):
    # `me` has weight_enabled=False and no note
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha go", reply_mode=ReplyMode.SEQUENTIAL)
    )
    assert "Author note" not in (mock_a.last_prompt or "")


@pytest.mark.asyncio
async def test_backend_error_isolated_other_personas_complete(
    repos, room, me, persona_a, persona_b, run_log, mock_b
):
    from app.domain.errors import HarnessTimeout

    failing = _RaisingBackend(HarnessTimeout("timed out"))
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, failing)  # persona_a -> fails
    reg.register(Provider.CODEX, mock_b)  # persona_b -> succeeds
    mock_b.script_reply("beta", "BETA-OK")

    orch = ChatOrchestrator(
        persona_repo=repos["persona"],
        author_repo=repos["author"],
        room_repo=repos["room"],
        message_repo=repos["message"],
        session_repo=repos["session"],
        run_repo=repos["run"],
        run_log=run_log,
        registry=reg,
    )
    events = await _drain(
        orch.post_message(
            room.id, author=me, text="@alpha @beta go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    # a RunError was yielded for persona_a
    a_errors = [ev for pid, ev in events if pid == persona_a.id and isinstance(ev, RunError)]
    assert len(a_errors) == 1
    assert a_errors[0].error_kind == "HarnessTimeout"

    # persona_b still completed
    contents = [m.content for m in repos["message"].list_for_room(room.id)]
    assert "BETA-OK" in contents
    # an error placeholder message exists for persona_a
    a_msgs = [
        m
        for m in repos["message"].list_for_room(room.id)
        if m.author_kind is AuthorKind.PERSONA and m.author_ref == persona_a.id
    ]
    assert len(a_msgs) == 1
    assert "error" in a_msgs[0].content.lower()
    assert a_msgs[0].run_id is not None

    # RunRecord for persona_a has error_kind set
    run = repos["run"].get(a_msgs[0].run_id)
    assert run is not None
    assert run.error_kind == "HarnessTimeout"


@pytest.mark.asyncio
async def test_tagged_override_bypasses_routing(orch, repos, room, me, persona_a, mock_a):
    # text mentions no one, but override forces persona_a
    await _drain(
        orch.post_message(
            room.id,
            author=me,
            text="no mentions here",
            reply_mode=ReplyMode.SEQUENTIAL,
            tagged_override=[persona_a],
        )
    )
    assert len(mock_a.calls) == 1


@pytest.mark.asyncio
async def test_resume_pointer_survives_fresh_session_lookup(
    orch, repos, room, me, persona_a, mock_a
):
    """FR-D2 building block: after a reply, a fresh get() shows saved id + pointer."""
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    msgs = repos["message"].list_for_room(room.id)
    fresh = repos["session"].get(room.id, persona_a.id)
    assert fresh is not None
    assert fresh.harness_session_id == "mock-1"
    assert fresh.last_seen_message_id == msgs[0].id


@pytest.mark.asyncio
async def test_run_record_finalized_with_usage(orch, repos, room, me, persona_a, mock_a):
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    reply = repos["message"].list_for_room(room.id)[1]
    run = repos["run"].get(reply.run_id)
    assert run is not None
    assert run.finished_at is not None
    assert run.exit_code == 0
    assert run.error_kind is None
    # default mock emits a Usage event
    assert run.usage.get("output_tokens") == 8
    # command_redacted carries a high-level descriptor, no system prompt / secrets
    assert "model=opus" in run.command_redacted
    assert "resume=" in run.command_redacted


def _orch_with_registry(repos, run_log, reg):
    return ChatOrchestrator(
        persona_repo=repos["persona"],
        author_repo=repos["author"],
        room_repo=repos["room"],
        message_repo=repos["message"],
        session_repo=repos["session"],
        run_repo=repos["run"],
        run_log=run_log,
        registry=reg,
    )


# --- I1: uniform isolation for NON-typed (untyped) errors ------------------- #


@pytest.mark.asyncio
async def test_sequential_untyped_error_isolated_other_personas_complete(
    repos, room, me, persona_a, persona_b, run_log, mock_b
):
    """A non-TeamError (RuntimeError) in persona A must NOT abort sibling B in
    SEQUENTIAL mode; A is still recorded (RunError + placeholder + RunRecord).
    """
    failing = _RaisingBackend(RuntimeError("boom"))  # NOT a TeamError
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, failing)  # persona_a -> raises RuntimeError
    reg.register(Provider.CODEX, mock_b)  # persona_b -> succeeds
    mock_b.script_reply("beta", "BETA-OK")

    orch = _orch_with_registry(repos, run_log, reg)
    events = await _drain(
        orch.post_message(
            room.id, author=me, text="@alpha @beta go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    # persona_a got a recorded RunError (kind = the exception class name)
    a_errors = [ev for pid, ev in events if pid == persona_a.id and isinstance(ev, RunError)]
    assert len(a_errors) == 1
    assert a_errors[0].error_kind == "RuntimeError"

    # persona_b STILL completed and persisted its reply (the bug: it was aborted)
    contents = [m.content for m in repos["message"].list_for_room(room.id)]
    assert "BETA-OK" in contents

    # error placeholder + finalized RunRecord with error_kind for persona_a
    a_msgs = [
        m
        for m in repos["message"].list_for_room(room.id)
        if m.author_kind is AuthorKind.PERSONA and m.author_ref == persona_a.id
    ]
    assert len(a_msgs) == 1
    assert "error" in a_msgs[0].content.lower()
    run = repos["run"].get(a_msgs[0].run_id)
    assert run is not None
    assert run.error_kind == "RuntimeError"
    assert run.exit_code == 1


# --- I2: real PARALLEL concurrency machinery -------------------------------- #


@pytest.mark.asyncio
async def test_parallel_events_interleave(repos, room, me, persona_a, persona_b, run_log):
    """Two awaiting backends in PARALLEL produce a genuinely interleaved stream:
    both persona ids appear among the first several events (not all-A-then-all-B).
    """
    back_a = _AwaitingBackend("A", "ALPHA", n_deltas=4)
    back_b = _AwaitingBackend("B", "BETA", n_deltas=4)
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, back_a)
    reg.register(Provider.CODEX, back_b)

    orch = _orch_with_registry(repos, run_log, reg)
    events = await _drain(
        orch.post_message(room.id, author=me, text="@alpha @beta go", reply_mode=ReplyMode.PARALLEL)
    )

    # Look at the persona ids of the first several yielded events; with real
    # await points both A and B must appear before either persona finishes its
    # 4 deltas (i.e. within the first 4 events we should see both ids).
    first_ids = [pid for pid, _ in events[:4]]
    assert persona_a.id in first_ids and persona_b.id in first_ids, first_ids
    # both replies persisted
    contents = [m.content for m in repos["message"].list_for_room(room.id)]
    assert any("ALPHA" in c for c in contents)
    assert any("BETA" in c for c in contents)


@pytest.mark.asyncio
async def test_parallel_early_stop_cancels_workers_and_cleans_up(
    repos, room, me, persona_a, persona_b, run_log
):
    """Consuming one event then aclose()-ing the generator must cancel in-flight
    workers: the backend's ``finally`` runs (cancelled flag set) and no asyncio
    task leaks.
    """
    back_a = _AwaitingBackend("A", "ALPHA", n_deltas=20)
    back_b = _AwaitingBackend("B", "BETA", n_deltas=20)
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, back_a)
    reg.register(Provider.CODEX, back_b)

    orch = _orch_with_registry(repos, run_log, reg)
    before = set(asyncio.all_tasks())

    agen = orch.post_message(
        room.id, author=me, text="@alpha @beta go", reply_mode=ReplyMode.PARALLEL
    )
    first = await agen.__anext__()
    assert first[0] in {persona_a.id, persona_b.id}
    await agen.aclose()
    # give cancelled tasks a tick to run their finally blocks
    await asyncio.sleep(0)

    # at least one in-flight backend was cancelled (finally ran -> flag set);
    # neither finished its full 20-delta stream
    assert back_a.cancelled or back_b.cancelled
    assert not (back_a.finished and back_b.finished)

    # no worker task leaked
    leaked = [t for t in (asyncio.all_tasks() - before) if not t.done()]
    assert leaked == [], leaked


@pytest.mark.asyncio
async def test_parallel_one_raises_others_survive_under_concurrency(
    repos, room, me, persona_a, persona_b, run_log
):
    """Under real concurrency, one awaiting backend raising mid-stream must not
    stop the other from persisting its reply; the raiser gets a RunError.
    """
    back_a = _AwaitingBackend("A", "ALPHA", n_deltas=4, raise_after=1)  # raises mid-stream
    back_b = _AwaitingBackend("B", "BETA", n_deltas=4)
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, back_a)
    reg.register(Provider.CODEX, back_b)

    orch = _orch_with_registry(repos, run_log, reg)
    events = await _drain(
        orch.post_message(room.id, author=me, text="@alpha @beta go", reply_mode=ReplyMode.PARALLEL)
    )

    a_errors = [ev for pid, ev in events if pid == persona_a.id and isinstance(ev, RunError)]
    assert len(a_errors) == 1
    assert a_errors[0].error_kind == "RuntimeError"

    contents = [m.content for m in repos["message"].list_for_room(room.id)]
    assert any("BETA" in c for c in contents)


# --- Task 4.8: control commands (send_control) ------------------------------ #


@pytest.mark.asyncio
async def test_send_control_supported_command_streams_and_finalizes(
    orch, repos, room, me, persona_a, mock_a
):
    # First post a message so persona_a has a live harness session.
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    session_before = repos["session"].get(room.id, persona_a.id)
    assert session_before is not None and session_before.harness_session_id == "mock-1"

    events = [ev async for ev in orch.send_control(room.id, persona_a.id, "/compact")]
    # mock emits "[compacted]" then RunDone
    assert any(isinstance(ev, TextDelta) and "compacted" in ev.text for ev in events)
    assert any(isinstance(ev, RunDone) for ev in events)

    session_after = repos["session"].get(room.id, persona_a.id)
    assert session_after is not None
    # /compact keeps the same session id (mock echoes session_id back)
    assert session_after.harness_session_id == "mock-1"
    # pointer preserved (a control command shows no new transcript)
    assert session_after.last_seen_message_id == session_before.last_seen_message_id
    # control commands do NOT persist a transcript message
    assert len(repos["message"].list_for_room(room.id)) == 2


@pytest.mark.asyncio
async def test_send_control_finalized_run_record(orch, repos, room, me, persona_a, mock_a):
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    await _drain_events(orch.send_control(room.id, persona_a.id, "/compact"))
    # the control RunRecord is finalized with exit_code 0
    rows = repos["run"]._db.query(  # type: ignore[attr-defined]
        "SELECT * FROM run_record WHERE command_redacted LIKE '%control /compact%'"
    )
    assert len(rows) == 1
    assert rows[0]["exit_code"] == 0
    assert rows[0]["error_kind"] is None
    assert rows[0]["finished_at"] is not None


@pytest.mark.asyncio
async def test_send_control_unsupported_command_raises_naming_supported(
    orch, repos, room, me, persona_a, mock_a
):
    await _drain(
        orch.post_message(room.id, author=me, text="@alpha hi", reply_mode=ReplyMode.SEQUENTIAL)
    )
    from app.domain.errors import HarnessError

    with pytest.raises(HarnessError) as excinfo:
        await _drain_events(orch.send_control(room.id, persona_a.id, "/bogus"))
    msg = str(excinfo.value)
    assert "/bogus" in msg
    assert "/compact" in msg and "/clear" in msg  # supported set is listed


@pytest.mark.asyncio
async def test_send_control_no_session_raises_session_not_found(
    orch, repos, room, me, persona_a, mock_a
):
    from app.domain.errors import SessionNotFound

    with pytest.raises(SessionNotFound):
        await _drain_events(orch.send_control(room.id, persona_a.id, "/compact"))


@pytest.mark.asyncio
async def test_send_control_unknown_persona_raises_session_not_found(orch, repos, room):
    from app.domain.errors import SessionNotFound

    with pytest.raises(SessionNotFound):
        await _drain_events(orch.send_control(room.id, "ghost", "/compact"))


@pytest.mark.asyncio
async def test_send_control_mid_stream_error_yields_runerror_and_preserves_session(
    repos, room, run_log, persona_a
):
    """Mid-stream backend error in send_control: a terminal RunError is YIELDED
    (not raised), the RunRecord is finalized with error_kind + exit_code 1, and
    the existing PersonaSession is left untouched (success-path upsert skipped).
    """
    from app.config.logging import run_id_var
    from app.domain.errors import HarnessTimeout

    backend = _ControlRaisingBackend(HarnessTimeout("boom"))
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, backend)  # persona_a -> CLAUDE
    orch = _orch_with_registry(repos, run_log, reg)

    # Seed an EXISTING live session so pre-run validation passes and we reach
    # the streaming path.
    repos["session"].upsert(
        PersonaSession(
            room_id=room.id,
            persona_id=persona_a.id,
            provider=Provider.CLAUDE,
            harness_session_id="sess-original",
            last_seen_message_id=None,
            status="idle",
        )
    )

    # 1. The terminal RunError is YIELDED, not raised.
    events = [ev async for ev in orch.send_control(room.id, persona_a.id, "/compact")]
    assert backend.calls == [("sess-original", "/compact")]
    run_errors = [ev for ev in events if isinstance(ev, RunError)]
    assert len(run_errors) == 1
    assert run_errors[0].error_kind == "HarnessTimeout"
    # the partial delta emitted before the boom was also streamed
    assert any(isinstance(ev, TextDelta) for ev in events)

    # 2. The RunRecord is finalized with error_kind set + exit_code == 1.
    rows = repos["run"]._db.query(  # type: ignore[attr-defined]
        "SELECT * FROM run_record WHERE command_redacted LIKE '%control /compact%'"
    )
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "HarnessTimeout"
    assert rows[0]["exit_code"] == 1
    assert rows[0]["finished_at"] is not None

    # 3. The PersonaSession is unchanged (success-path upsert was skipped).
    session_after = repos["session"].get(room.id, persona_a.id)
    assert session_after is not None
    assert session_after.harness_session_id == "sess-original"

    # 4. run_id_var was reset back to None.
    assert run_id_var.get() is None


async def _drain_events(agen):
    return [ev async for ev in agen]
