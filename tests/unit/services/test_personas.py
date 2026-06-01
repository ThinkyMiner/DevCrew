from __future__ import annotations

import pytest

from app.domain.errors import TeamError
from app.domain.models import Persona, PersonaSession, Provider, Room
from app.persistence.db import Database
from app.persistence.repositories import PersonaRepo, RoomRepo, SessionRepo
from app.services.personas import PersonaService


def make_persona(*, handle: str, name: str, is_template: bool = False) -> Persona:
    return Persona(
        name=name,
        handle=handle,
        provider=Provider.MOCK,
        model="m",
        is_template=is_template,
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture
def repo(db):
    return PersonaRepo(db)


@pytest.fixture
def service(repo):
    return PersonaService(repo)


def test_create_get_list_update_delete(service):
    p = service.create(make_persona(handle="alpha", name="Alpha"))
    assert service.get(p.id).handle == "alpha"
    assert [x.id for x in service.list()] == [p.id]

    p.name = "Alpha2"
    service.update(p)
    assert service.get(p.id).name == "Alpha2"

    service.delete(p.id)
    assert service.list() == []


def test_get_missing_raises(service):
    with pytest.raises(TeamError):
        service.get("nope")


def test_duplicate_distinct_id_and_noncolliding_handle(service):
    src = service.create(make_persona(handle="alpha", name="Alpha"))
    dup = service.duplicate(src.id)
    assert dup.id != src.id
    assert dup.handle == "alpha-copy"
    assert dup.name == "Alpha (copy)"
    assert dup.is_template is False
    # both rows persist independently
    handles = {p.handle for p in service.list()}
    assert handles == {"alpha", "alpha-copy"}


def test_duplicate_twice_keeps_appending_copy(service):
    src = service.create(make_persona(handle="alpha", name="Alpha"))
    service.duplicate(src.id)  # alpha-copy
    second = service.duplicate(src.id)  # alpha-copy exists -> alpha-copy-copy
    assert second.handle == "alpha-copy-copy"


def test_update_preserves_existing_session(service, repo, db):
    """FR-P1: editing a persona must NOT destroy its (room,persona) session."""
    sessions = SessionRepo(db)
    room = RoomRepo(db).create(Room(name="General"))
    p = service.create(make_persona(handle="alpha", name="Alpha"))
    sessions.upsert(
        PersonaSession(
            room_id=room.id,
            persona_id=p.id,
            provider=Provider.MOCK,
            harness_session_id="sess-123",
            last_seen_message_id="msg-9",
        )
    )
    p.name = "Renamed"
    p.system_prompt = "new prompt"
    service.update(p)

    survived = sessions.get(room.id, p.id)
    assert survived is not None
    assert survived.harness_session_id == "sess-123"
    assert survived.last_seen_message_id == "msg-9"


def test_list_templates_and_create_from_template(service):
    tmpl = service.create(make_persona(handle="tmpl", name="Template", is_template=True))
    service.create(make_persona(handle="normal", name="Normal"))
    assert [t.id for t in service.list_templates()] == [tmpl.id]

    inst = service.create_from_template(tmpl.id)
    assert inst.is_template is False
    assert inst.handle == "tmpl-copy"


def test_create_from_template_rejects_non_template(service):
    p = service.create(make_persona(handle="alpha", name="Alpha"))
    with pytest.raises(TeamError):
        service.create_from_template(p.id)
