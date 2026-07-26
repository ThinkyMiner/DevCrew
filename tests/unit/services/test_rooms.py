from __future__ import annotations

import pytest

from app.domain.errors import TeamError
from app.domain.models import Persona, Provider, Room
from app.persistence.db import Database
from app.persistence.repositories import PersonaRepo, RoomRepo
from app.services.rooms import RoomService


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture
def personas(db):
    return PersonaRepo(db)


@pytest.fixture
def service(db, personas):
    return RoomService(RoomRepo(db), personas)


def _persona(personas, handle):
    return personas.create(
        Persona(name=handle.title(), handle=handle, provider=Provider.MOCK, model="m")
    )


def test_create_get_list_delete(service):
    r = service.create(Room(name="General"))
    assert service.get(r.id).name == "General"
    assert [x.id for x in service.list()] == [r.id]
    service.delete(r.id)
    assert service.list() == []


def test_get_missing_raises(service):
    with pytest.raises(TeamError):
        service.get("nope")


def test_rename_and_archive(service):
    r = service.create(Room(name="Old"))
    assert service.rename(r.id, "New").name == "New"
    assert service.get(r.id).name == "New"
    assert service.archive(r.id).archived is True
    assert service.archive(r.id, archived=False).archived is False


def test_update_edits_all_fields(service):
    from app.domain.models import ReplyMode

    r = service.create(Room(name="Old"))
    r.name = "New"
    r.topic = "A topic"
    r.default_reply_mode = ReplyMode.PARALLEL
    r.archived = True
    updated = service.update(r)
    assert updated.name == "New"
    fetched = service.get(r.id)
    assert fetched.topic == "A topic"
    assert fetched.default_reply_mode is ReplyMode.PARALLEL
    assert fetched.archived is True


def test_update_missing_raises(service):
    with pytest.raises(TeamError):
        service.update(Room(id="ghost", name="X"))


def test_list_member_personas_preserves_order(service, personas):
    r = service.create(Room(name="General"))
    a = _persona(personas, "alpha")
    b = _persona(personas, "beta")
    service.add_member(r.id, b.id)
    service.add_member(r.id, a.id)
    members = service.list_member_personas(r.id)
    assert [m.id for m in members] == [b.id, a.id]


def test_list_member_personas_skips_missing(db):
    """A membership id that no longer resolves to a persona is skipped, not fatal.

    A real DB cascade-deletes membership when a persona is removed, so we exercise
    the defensive skip via a RoomRepo whose membership lists an id the
    PersonaRepo cannot resolve.
    """
    from app.domain.models import Persona

    class _StubRoomRepo:
        def list_members(self, room_id):
            return ["real-id", "ghost-id"]

    class _StubPersonaRepo:
        def get(self, pid):
            if pid == "real-id":
                return Persona(name="Real", handle="real", provider=Provider.MOCK, model="m")
            return None

    svc = RoomService(_StubRoomRepo(), _StubPersonaRepo())  # type: ignore[arg-type]
    members = svc.list_member_personas("r1")
    assert [m.handle for m in members] == ["real"]


def test_create_room_auto_adds_dispatcher(service, personas):
    """A fresh room ships with the orchestrator persona already present, so the
    operator can tag it to route work without any setup (dispatcher is seeded)."""
    from app.services.persona_seed import DISPATCHER_HANDLE

    disp = personas.create(
        Persona(name="systemd", handle=DISPATCHER_HANDLE, provider=Provider.MOCK, model="m")
    )
    r = service.create(Room(name="General"))
    assert disp.id in [m.id for m in service.list_member_personas(r.id)]


def test_create_room_without_dispatcher_seeded_is_fine(service):
    # No dispatcher persona present -> room is simply empty; never crashes.
    r = service.create(Room(name="Empty"))
    assert service.list_member_personas(r.id) == []


def test_remove_member(service, personas):
    r = service.create(Room(name="General"))
    a = _persona(personas, "alpha")
    service.add_member(r.id, a.id)
    service.remove_member(r.id, a.id)
    assert service.list_member_personas(r.id) == []
