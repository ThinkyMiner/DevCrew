"""Persona-to-persona delegation: a persona's reply that @-mentions another
persona hands that persona a turn (auto-adding them to the room if needed),
bounded by depth/run caps and a once-per-post guard. Per-room toggle.

All backends are MockHarness. Replies are scripted by the unique ``→ @<handle>]``
substring that appears in each persona's own prompt, so a script keys to exactly
one persona.
"""

from __future__ import annotations

import pytest

from app.domain.models import HumanAuthor, Persona, Provider, ReplyMode, Room
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


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    repos = {
        "persona": PersonaRepo(db),
        "author": AuthorRepo(db),
        "room": RoomRepo(db),
        "message": MessageRepo(db),
        "session": SessionRepo(db),
        "run": RunRepo(db),
    }
    mock_a = MockHarness()  # claude personas
    mock_b = MockHarness()  # codex personas
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, mock_a)
    reg.register(Provider.CODEX, mock_b)
    orch = ChatOrchestrator(
        persona_repo=repos["persona"],
        author_repo=repos["author"],
        room_repo=repos["room"],
        message_repo=repos["message"],
        session_repo=repos["session"],
        run_repo=repos["run"],
        run_log=RunLogStore(tmp_path / "runs"),
        registry=reg,
    )
    me = repos["author"].create(HumanAuthor(name="Me", weight_enabled=False))
    return {"orch": orch, "repos": repos, "mock_a": mock_a, "mock_b": mock_b, "me": me}


def _persona(repos, room_id, name, handle, provider, *, member=True):
    p = repos["persona"].create(
        Persona(name=name, handle=handle, provider=provider, model="m", job=f"{name} job")
    )
    if member:
        repos["room"].add_member(room_id, p.id)
    return p


def _persona_messages(repos, room_id):
    from app.domain.models import AuthorKind

    return [
        m
        for m in repos["message"].list_for_room(room_id)
        if m.author_kind is AuthorKind.PERSONA and not m.content.startswith("[error:")
    ]


async def _drain(agen):
    return [item async for item in agen]


@pytest.mark.asyncio
async def test_reply_mentioning_member_triggers_delegated_run(env):
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_reply("→ @arch]", "On it. @back please sketch the API.")
    env["mock_b"].script_reply("→ @back]", "API sketched.")

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch design it", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    # backend ran exactly once, as a delegated turn, and saw architect's reply.
    assert len(env["mock_b"].calls) == 1
    assert "please sketch the API" in env["mock_b"].calls[0].prompt
    contents = [m.content for m in _persona_messages(repos, room.id)]
    assert "API sketched." in contents


@pytest.mark.asyncio
async def test_delegation_auto_adds_non_member_then_runs(env):
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    back = _persona(repos, room.id, "Backend", "back", Provider.CODEX, member=False)
    assert back.id not in repos["room"].list_members(room.id)
    env["mock_a"].script_reply("→ @arch]", "Bringing in @back to build it.")
    env["mock_b"].script_reply("→ @back]", "Built.")

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch plan it", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    assert back.id in repos["room"].list_members(room.id)  # auto-added
    assert len(env["mock_b"].calls) == 1


@pytest.mark.asyncio
async def test_delegation_respects_depth_cap(env):
    # arch(0) -> back(1) -> crit(2) -> dave(3, blocked by depth cap of 2).
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    _persona(repos, room.id, "Critic", "crit", Provider.CLAUDE)
    _persona(repos, room.id, "Dave", "dave", Provider.CODEX)
    env["mock_a"].script_reply("→ @arch]", "next @back")
    env["mock_b"].script_reply("→ @back]", "next @crit")
    env["mock_a"].script_reply("→ @crit]", "next @dave")
    env["mock_b"].script_reply("→ @dave]", "the end")

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    handles_that_replied = {
        repos["persona"].get(m.author_ref).handle for m in _persona_messages(repos, room.id)
    }
    assert {"arch", "back", "crit"} <= handles_that_replied
    assert "dave" not in handles_that_replied  # depth 3 blocked


@pytest.mark.asyncio
async def test_delegation_respects_run_cap(env):
    # arch tags 8 personas; only 6 delegated runs are allowed per post.
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    tags = []
    for i in range(8):
        h = f"b{i}"
        _persona(repos, room.id, f"B{i}", h, Provider.CODEX)
        tags.append(f"@{h}")
    env["mock_a"].script_reply("→ @arch]", "everyone: " + " ".join(tags))
    # bN use default mock replies.

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    delegated = [
        m
        for m in _persona_messages(repos, room.id)
        if repos["persona"].get(m.author_ref).handle != "arch"
    ]
    assert len(delegated) == 6  # capped


@pytest.mark.asyncio
async def test_persona_invoked_at_most_once_per_post(env):
    # arch -> back -> (back tags arch, but arch already ran => no re-invoke, no loop).
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_reply("→ @arch]", "@back take it")
    env["mock_b"].script_reply("→ @back]", "@arch back to you")

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    assert len(env["mock_a"].calls) == 1  # architect ran once, not re-invoked
    assert len(env["mock_b"].calls) == 1


@pytest.mark.asyncio
async def test_delegation_disabled_by_room_toggle(env):
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R", delegation_enabled=False))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_reply("→ @arch]", "@back please do it")
    env["mock_b"].script_reply("→ @back]", "done")

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch go", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    assert env["mock_b"].calls == []  # no delegation when toggle off


@pytest.mark.asyncio
async def test_roster_injected_into_system_prompt_when_enabled(env):
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch hi", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    sys_prompt = env["mock_a"].calls[0].system_prompt
    assert "@back" in sys_prompt  # architect told about its teammate


@pytest.mark.asyncio
async def test_roster_not_injected_when_disabled(env):
    repos, orch = env["repos"], env["orch"]
    room = repos["room"].create(Room(name="R", delegation_enabled=False))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)

    await _drain(
        orch.post_message(
            room.id, author=env["me"], text="@arch hi", reply_mode=ReplyMode.SEQUENTIAL
        )
    )

    assert "@back" not in env["mock_a"].calls[0].system_prompt
