from __future__ import annotations

import pytest

from app.domain.errors import TeamError
from app.domain.models import Persona, PersonaSession, Provider, Room
from app.persistence.db import Database
from app.persistence.repositories import PersonaRepo, RoomRepo, SessionRepo
from app.services.session_store import SessionStore


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture
def sessions(db):
    return SessionRepo(db)


@pytest.fixture
def personas(db):
    return PersonaRepo(db)


@pytest.fixture
def store(sessions, personas):
    return SessionStore(sessions, personas)


@pytest.fixture
def room(db):
    return RoomRepo(db).create(Room(name="General"))


@pytest.fixture
def persona(personas):
    return personas.create(Persona(name="Alpha", handle="alpha", provider=Provider.MOCK, model="m"))


def test_reset_session_clears_id_and_pointer(store, sessions, room, persona):
    sessions.upsert(
        PersonaSession(
            room_id=room.id,
            persona_id=persona.id,
            provider=Provider.MOCK,
            harness_session_id="sess-9",
            last_seen_message_id="msg-5",
            status="idle",
        )
    )
    fresh = store.reset_session(room.id, persona.id)
    assert fresh.harness_session_id is None
    assert fresh.last_seen_message_id is None
    assert fresh.status == "idle"

    persisted = sessions.get(room.id, persona.id)
    assert persisted is not None
    assert persisted.harness_session_id is None
    assert persisted.last_seen_message_id is None
    assert persisted.provider is Provider.MOCK


def test_reset_session_creates_row_when_none_exists(store, sessions, room, persona):
    fresh = store.reset_session(room.id, persona.id)
    assert fresh.harness_session_id is None
    assert sessions.get(room.id, persona.id) is not None


def test_reset_unknown_persona_raises(store, room):
    with pytest.raises(TeamError):
        store.reset_session(room.id, "ghost")


def test_in_flight_tracking(store):
    assert store.is_busy("r1", "p1") is False
    store.mark_active("r1", "p1", "run-1")
    store.mark_active("r1", "p1", "run-2")
    assert store.active_runs("r1", "p1") == {"run-1", "run-2"}
    assert store.is_busy("r1", "p1") is True
    store.mark_done("r1", "p1", "run-1")
    assert store.active_runs("r1", "p1") == {"run-2"}
    store.mark_done("r1", "p1", "run-2")
    assert store.is_busy("r1", "p1") is False
    # other persona unaffected
    assert store.active_runs("r1", "p2") == set()
