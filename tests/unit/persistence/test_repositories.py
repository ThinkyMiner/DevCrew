from __future__ import annotations

import pytest

from app.domain.errors import SessionNotFound
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
        job="System architect",
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


def test_persona_get_by_handle(db: Database) -> None:
    repo = PersonaRepo(db)
    p = Persona(name="Ada", handle="architect", provider=Provider.CLAUDE, model="opus")
    repo.create(p)
    got = repo.get_by_handle("architect")
    assert got is not None and got.id == p.id
    assert repo.get_by_handle("missing") is None


# --- RoomRepo + membership ----------------------------------------------


def test_room_roundtrip(db: Database) -> None:
    repo = RoomRepo(db)
    r = Room(name="General", topic="stuff", default_reply_mode=ReplyMode.PARALLEL)
    repo.create(r)
    assert repo.get(r.id) == r


def test_room_delegation_enabled_roundtrips(db: Database) -> None:
    repo = RoomRepo(db)
    r = Room(name="Quiet", delegation_enabled=False)
    repo.create(r)
    assert repo.get(r.id).delegation_enabled is False  # type: ignore[union-attr]
    r.delegation_enabled = True
    repo.update(r)
    assert repo.get(r.id).delegation_enabled is True  # type: ignore[union-attr]


def test_room_working_dir_roundtrips(db: Database) -> None:
    repo = RoomRepo(db)
    r = Room(name="Repo", working_dir="/Users/me/project")
    repo.create(r)
    assert repo.get(r.id).working_dir == "/Users/me/project"  # type: ignore[union-attr]
    r.working_dir = None
    repo.update(r)
    assert repo.get(r.id).working_dir is None  # type: ignore[union-attr]


def test_room_list_by_activity_orders_by_last_message_then_creation(db: Database) -> None:
    from datetime import UTC, datetime

    def dt(year: int) -> datetime:
        return datetime(year, 1, 1, tzinfo=UTC)

    rooms, msgs = RoomRepo(db), MessageRepo(db)
    a = rooms.create(Room(name="A", created_at=dt(2020)))
    rooms.create(Room(name="B", created_at=dt(2021)))  # no messages -> uses creation time
    c = rooms.create(Room(name="C", created_at=dt(2019)))
    msgs.create(
        Message(
            room_id=a.id,
            author_kind=AuthorKind.HUMAN,
            author_ref="me",
            content="newest",
            created_at=dt(2026),
        )
    )
    msgs.create(
        Message(
            room_id=c.id,
            author_kind=AuthorKind.HUMAN,
            author_ref="me",
            content="older",
            created_at=dt(2022),
        )
    )
    # Activity = last message time, falling back to room creation time:
    #   A -> 2026 (msg), C -> 2022 (msg), B -> 2021 (creation). Most recent first.
    assert [r.name for r in rooms.list_by_activity()] == ["A", "C", "B"]
    # list() is unchanged: still stable creation order.
    assert [r.name for r in rooms.list()] == ["C", "A", "B"]


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


def test_finalize_unknown_run_id_raises(db: Database) -> None:
    repo = RunRepo(db)
    rr = RunRecord(
        run_id="ghost",
        room_id="r1",
        persona_id="p1",
        command_redacted="claude --print [REDACTED]",
        usage={},
    )
    # never created -> finalize must fail loud, not silently no-op
    with pytest.raises(SessionNotFound, match="ghost"):
        repo.finalize(rr)


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
    base = Message(room_id=r.id, author_kind=AuthorKind.HUMAN, author_ref="me", content="x")
    msgs.create(base)
    # a quoted message so message_quote is populated and its cascade is exercised
    msgs.create(
        Message(
            room_id=r.id,
            author_kind=AuthorKind.HUMAN,
            author_ref="me",
            content="reply",
            quoted_message_ids=[base.id],
        )
    )
    sessions.upsert(PersonaSession(room_id=r.id, persona_id=p.id, provider=Provider.MOCK))

    # sanity: child rows exist before delete
    assert db.query("SELECT COUNT(*) AS n FROM message_quote")[0]["n"] == 1

    rooms.delete(r.id)

    assert rooms.list_members(r.id) == []
    assert msgs.list_for_room(r.id) == []
    assert sessions.get(r.id, p.id) is None
    # RAW counts prove child rows are actually deleted (not just filtered out)
    assert db.query("SELECT COUNT(*) AS n FROM room_persona")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM message")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM message_quote")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM persona_session")[0]["n"] == 0
    # persona itself survives room deletion
    assert personas.get(p.id) is not None


def test_run_list_for_room_in_start_order(db: Database) -> None:
    repo = RunRepo(db)
    a = RunRecord(room_id="r1", persona_id="p1", command_redacted="c", usage={})
    b = RunRecord(room_id="r1", persona_id="p2", command_redacted="c", usage={})
    other = RunRecord(room_id="r2", persona_id="p1", command_redacted="c", usage={})
    for rr in (a, b, other):
        repo.create(rr)

    got = repo.list_for_room("r1")
    assert [r.run_id for r in got] == [a.run_id, b.run_id]
    assert repo.list_for_room("empty") == []
