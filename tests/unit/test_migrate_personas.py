"""Tests for the one-time persona-cleanup migration (scripts/migrate_personas).

Covers the risky parts against a temp DB: dedupe of unused copies, MERGE of an
in-use copy (its messages/membership/sessions re-point to the original before the
copy is deleted), human-name rename that preserves the existing system prompt,
and idempotency.
"""

from __future__ import annotations

import pytest

from app.domain.models import (
    AuthorKind,
    Message,
    Persona,
    PersonaSession,
    Provider,
    Room,
)
from app.persistence.db import Database
from app.persistence.repositories import (
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    SessionRepo,
)
from scripts.migrate_personas import migrate


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "team.db")
    database.init_schema()
    return database


def test_migrate_dedupes_renames_merges_and_seeds(db: Database) -> None:
    personas = PersonaRepo(db)
    rooms = RoomRepo(db)
    msgs = MessageRepo(db)
    sessions = SessionRepo(db)

    # Originals (the real team) — as they exist today: templates with rich prompts.
    personas.create(
        Persona(
            name="Architect",
            handle="architect",
            provider=Provider.CLAUDE,
            model="opus",
            system_prompt="ORIGINAL-ARCH-PROMPT",
            is_template=True,
        )
    )
    critic = personas.create(
        Persona(
            name="Critic",
            handle="critic",
            provider=Provider.CODEX,
            model="gpt-5.5",
            system_prompt="ORIGINAL-CRITIC-PROMPT",
            is_template=True,
        )
    )
    # An UNUSED copy (no messages / not a member): should just vanish.
    personas.create(
        Persona(
            name="Architect (copy)",
            handle="architect-copy-copy",
            provider=Provider.CLAUDE,
            model="opus",
        )
    )
    # An IN-USE copy: authored a message, is a room member, has a session.
    critic_copy = personas.create(
        Persona(name="Critic (copy)", handle="critic-copy", provider=Provider.CLAUDE, model="opus")
    )
    room = rooms.create(Room(name="R"))
    rooms.add_member(room.id, critic_copy.id)
    kept_msg = msgs.create(
        Message(
            room_id=room.id,
            author_kind=AuthorKind.PERSONA,
            author_ref=critic_copy.id,
            content="a real reply from the copy",
        )
    )
    sessions.upsert(
        PersonaSession(
            room_id=room.id,
            persona_id=critic_copy.id,
            provider=Provider.CLAUDE,
            harness_session_id="sess-1",
        )
    )

    report = migrate(db)

    # Copies gone.
    assert personas.get_by_handle("architect-copy-copy") is None
    assert personas.get_by_handle("critic-copy") is None
    # Merge: the copy's message now belongs to the ORIGINAL critic.
    assert msgs.get(kept_msg.id).author_ref == critic.id  # type: ignore[union-attr]
    # Merge: the original critic became a member (re-pointed), copy did not linger.
    assert critic.id in rooms.list_members(room.id)
    assert critic_copy.id not in rooms.list_members(room.id)
    # Rename to human names; is_template cleared; RICH PROMPT PRESERVED.
    ada = personas.get_by_handle("architect")
    assert ada is not None and ada.name == "Ada" and ada.is_template is False
    assert ada.system_prompt == "ORIGINAL-ARCH-PROMPT"
    linus = personas.get_by_handle("critic")
    assert linus is not None and linus.name == "Linus"
    assert linus.system_prompt == "ORIGINAL-CRITIC-PROMPT"
    # Orchestrator ensured present and usable.
    disp = personas.get_by_handle("systemd")
    assert disp is not None and disp.is_template is False
    # Report reflects what happened.
    assert report.removed_unused >= 1
    assert report.merged >= 1


def test_migrate_is_idempotent(db: Database) -> None:
    personas = PersonaRepo(db)
    personas.create(
        Persona(
            name="Architect",
            handle="architect",
            provider=Provider.CLAUDE,
            model="opus",
            is_template=True,
        )
    )
    migrate(db)
    n1 = len(personas.list())
    migrate(db)
    handles = [p.handle for p in personas.list()]
    assert len(handles) == len(set(handles))  # no dup systemd / no dup handles
    assert len(personas.list()) == n1
    assert personas.get_by_handle("architect").name == "Ada"  # type: ignore[union-attr]
