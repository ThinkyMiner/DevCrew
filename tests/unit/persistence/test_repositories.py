from __future__ import annotations

from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Message,
    PermissionMode,
    Persona,
    PersonaSession,
    Provider,
    ReplyMode,
    Room,
    RunRecord,
)
from app.persistence.db import Database
from app.persistence.repositories import (
    AuthorRepo,
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    RunRepo,
    SessionRepo,
)

# --- PersonaRepo ---------------------------------------------------------


def test_persona_create_fetch_roundtrip(db: Database) -> None:
    repo = PersonaRepo(db)
    p = Persona(
        name="Architect",
        handle="arch",
        provider=Provider.CLAUDE,
        model="opus",
        effort="high",
        system_prompt="be terse",
        mcp_servers=["fs", "git"],
        allowed_tools=["Read", "Grep"],
        working_dir="/tmp",
        permission_mode=PermissionMode.AUTO,
        is_template=True,
    )
    repo.create(p)
    got = repo.get(p.id)
    assert got == p


def test_persona_list_and_update_and_delete(db: Database) -> None:
    repo = PersonaRepo(db)
    p = Persona(name="A", handle="a", provider=Provider.MOCK, model="m")
    repo.create(p)
    assert [x.id for x in repo.list()] == [p.id]

    p.name = "Renamed"
    repo.update(p)
    assert repo.get(p.id).name == "Renamed"  # type: ignore[union-attr]

    repo.delete(p.id)
    assert repo.get(p.id) is None
    assert repo.list() == []


def test_persona_empty_json_columns_roundtrip(db: Database) -> None:
    repo = PersonaRepo(db)
    p = Persona(name="A", handle="a", provider=Provider.MOCK, model="m")
    repo.create(p)
    got = repo.get(p.id)
    assert got is not None
    assert got.mcp_servers == []
    assert got.allowed_tools == []


# --- AuthorRepo ----------------------------------------------------------


def test_author_roundtrip_and_defaults(db: Database) -> None:
    repo = AuthorRepo(db)
    boss = HumanAuthor(name="Boss", weight_note="Leadership input.")
    repo.create(boss)
    got = repo.get(boss.id)
    assert got == boss
    assert got.weight_enabled is True  # type: ignore[union-attr]


# --- RoomRepo + membership ----------------------------------------------


def test_room_roundtrip(db: Database) -> None:
    repo = RoomRepo(db)
    r = Room(name="General", topic="stuff", default_reply_mode=ReplyMode.PARALLEL)
    repo.create(r)
    assert repo.get(r.id) == r


def test_room_membership_preserves_order(db: Database) -> None:
    rooms, personas = RoomRepo(db), PersonaRepo(db)
    r = Room(name="R")
    rooms.create(r)
    p1 = Persona(name="P1", handle="p1", provider=Provider.MOCK, model="m")
    p2 = Persona(name="P2", handle="p2", provider=Provider.MOCK, model="m")
    p3 = Persona(name="P3", handle="p3", provider=Provider.MOCK, model="m")
    for p in (p1, p2, p3):
        personas.create(p)

    rooms.add_member(r.id, p2.id)
    rooms.add_member(r.id, p1.id)
    rooms.add_member(r.id, p3.id)
    assert rooms.list_members(r.id) == [p2.id, p1.id, p3.id]

    rooms.remove_member(r.id, p1.id)
    assert rooms.list_members(r.id) == [p2.id, p3.id]


# --- MessageRepo ---------------------------------------------------------


def test_message_roundtrip(db: Database) -> None:
    rooms, msgs = RoomRepo(db), MessageRepo(db)
    r = Room(name="R")
    rooms.create(r)
    m = Message(
        room_id=r.id,
        author_kind=AuthorKind.HUMAN,
        author_ref="me",
        content="hello",
        run_id="run-1",
    )
    msgs.create(m)
    assert msgs.get(m.id) == m


def test_message_repo_orders_chronologically(db: Database) -> None:
    rooms, msgs = RoomRepo(db), MessageRepo(db)
    r = Room(name="A")
    rooms.create(r)
    a = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="1")
    b = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="2")
    c = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="3")
    # Insert out of creation order to prove ordering is by created_at, not insert order.
    for m in (b, a, c):
        msgs.create(m)
    got = msgs.list_for_room(r.id)
    assert [m.id for m in got] == [a.id, b.id, c.id]


def test_message_quotes_persist_and_roundtrip(db: Database) -> None:
    rooms, msgs = RoomRepo(db), MessageRepo(db)
    r = Room(name="R")
    rooms.create(r)
    q1 = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="q1")
    q2 = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="q2")
    msgs.create(q1)
    msgs.create(q2)
    m = Message(
        room_id=r.id,
        author_kind=AuthorKind.HUMAN,
        author_ref="me",
        content="reply",
        quoted_message_ids=[q1.id, q2.id],
    )
    msgs.create(m)
    got = msgs.get(m.id)
    assert got is not None
    assert got.quoted_message_ids == [q1.id, q2.id]


# --- SessionRepo ---------------------------------------------------------


def test_session_upsert_and_get(db: Database) -> None:
    rooms, personas, sessions = RoomRepo(db), PersonaRepo(db), SessionRepo(db)
    r = Room(name="R")
    rooms.create(r)
    p = Persona(name="P", handle="p", provider=Provider.MOCK, model="m")
    personas.create(p)

    s = PersonaSession(room_id=r.id, persona_id=p.id, provider=Provider.MOCK)
    sessions.upsert(s)
    assert sessions.get(r.id, p.id) == s

    s.harness_session_id = "sess-1"
    s.last_seen_message_id = "m9"
    s.status = "running"
    sessions.upsert(s)
    got = sessions.get(r.id, p.id)
    assert got is not None
    assert got.harness_session_id == "sess-1"
    assert got.last_seen_message_id == "m9"
    assert got.status == "running"
    # upsert overwrites, never duplicates
    assert len(db.query("SELECT 1 FROM persona_session")) == 1


def test_session_get_missing_returns_none(db: Database) -> None:
    assert SessionRepo(db).get("nope", "nope") is None


# --- RunRepo -------------------------------------------------------------


def test_run_create_and_finalize(db: Database) -> None:
    repo = RunRepo(db)
    rr = RunRecord(
        room_id="r1",
        persona_id="p1",
        command_redacted="claude --print [REDACTED]",
        usage={"input_tokens": 1},
    )
    repo.create(rr)
    assert repo.get(rr.run_id) == rr

    rr.exit_code = 0
    rr.usage = {"input_tokens": 5, "output_tokens": 10}
    rr.log_path = "/logs/x.jsonl"
    repo.finalize(rr)
    got = repo.get(rr.run_id)
    assert got is not None
    assert got.exit_code == 0
    assert got.usage == {"input_tokens": 5, "output_tokens": 10}
    assert got.log_path == "/logs/x.jsonl"


# --- Cascade -------------------------------------------------------------


def test_deleting_room_cascades(db: Database) -> None:
    rooms = RoomRepo(db)
    personas = PersonaRepo(db)
    msgs = MessageRepo(db)
    sessions = SessionRepo(db)

    r = Room(name="R")
    rooms.create(r)
    p = Persona(name="P", handle="p", provider=Provider.MOCK, model="m")
    personas.create(p)
    rooms.add_member(r.id, p.id)
    msgs.create(Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="x"))
    sessions.upsert(PersonaSession(room_id=r.id, persona_id=p.id, provider=Provider.MOCK))

    rooms.delete(r.id)

    assert rooms.list_members(r.id) == []
    assert msgs.list_for_room(r.id) == []
    assert sessions.get(r.id, p.id) is None
    # persona itself survives room deletion
    assert personas.get(p.id) is not None
