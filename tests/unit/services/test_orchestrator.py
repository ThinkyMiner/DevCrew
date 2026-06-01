"""Tests for the ChatOrchestrator — the keystone that ties everything together.

No real CLI: every backend is a :class:`MockHarness`. To assert per-persona
prompts unambiguously (the mock has no notion of persona ids), we register a
*distinct* mock instance per provider and give the two personas different
providers, both backed by mocks. ``mock_b.last_prompt`` is then unambiguously B's.
"""

from __future__ import annotations

import pytest

from app.domain.events import RunError, TextDelta
from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Persona,
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
