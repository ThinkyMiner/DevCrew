from __future__ import annotations

import builtins
import json
import sqlite3
from datetime import datetime

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


def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_opt_dt(value: str | None) -> datetime | None:
    return _parse_dt(value) if value is not None else None


class PersonaRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Persona:
        return Persona.model_construct(
            id=row["id"],
            name=row["name"],
            handle=row["handle"],
            color=row["color"],
            provider=Provider(row["provider"]),
            model=row["model"],
            effort=row["effort"],
            system_prompt=row["system_prompt"],
            mcp_servers=json.loads(row["mcp_servers"]),
            allowed_tools=json.loads(row["allowed_tools"]),
            working_dir=row["working_dir"],
            permission_mode=PermissionMode(row["permission_mode"]),
            is_template=bool(row["is_template"]),
            created_at=_parse_dt(row["created_at"]),
        )

    def create(self, persona: Persona) -> Persona:
        self._db.execute(
            "INSERT INTO persona (id, name, handle, color, provider, model, effort, "
            "system_prompt, mcp_servers, allowed_tools, working_dir, permission_mode, "
            "is_template, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                persona.id,
                persona.name,
                persona.handle,
                persona.color,
                persona.provider.value,
                persona.model,
                persona.effort,
                persona.system_prompt,
                json.dumps(persona.mcp_servers),
                json.dumps(persona.allowed_tools),
                persona.working_dir,
                persona.permission_mode.value,
                int(persona.is_template),
                _dt(persona.created_at),
            ),
        )
        return persona

    def get(self, persona_id: str) -> Persona | None:
        rows = self._db.query("SELECT * FROM persona WHERE id = ?", (persona_id,))
        return self._row_to_model(rows[0]) if rows else None

    def list(self) -> list[Persona]:
        rows = self._db.query("SELECT * FROM persona ORDER BY created_at, id")
        return [self._row_to_model(r) for r in rows]

    def update(self, persona: Persona) -> Persona:
        self._db.execute(
            "UPDATE persona SET name = ?, handle = ?, color = ?, provider = ?, model = ?, "
            "effort = ?, system_prompt = ?, mcp_servers = ?, allowed_tools = ?, "
            "working_dir = ?, permission_mode = ?, is_template = ? WHERE id = ?",
            (
                persona.name,
                persona.handle,
                persona.color,
                persona.provider.value,
                persona.model,
                persona.effort,
                persona.system_prompt,
                json.dumps(persona.mcp_servers),
                json.dumps(persona.allowed_tools),
                persona.working_dir,
                persona.permission_mode.value,
                int(persona.is_template),
                persona.id,
            ),
        )
        return persona

    def delete(self, persona_id: str) -> None:
        self._db.execute("DELETE FROM persona WHERE id = ?", (persona_id,))


class AuthorRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> HumanAuthor:
        return HumanAuthor.model_construct(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            weight_note=row["weight_note"],
            weight_enabled=bool(row["weight_enabled"]),
        )

    def create(self, author: HumanAuthor) -> HumanAuthor:
        self._db.execute(
            "INSERT INTO human_author (id, name, color, weight_note, weight_enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                author.id,
                author.name,
                author.color,
                author.weight_note,
                int(author.weight_enabled),
            ),
        )
        return author

    def get(self, author_id: str) -> HumanAuthor | None:
        rows = self._db.query("SELECT * FROM human_author WHERE id = ?", (author_id,))
        return self._row_to_model(rows[0]) if rows else None

    def list(self) -> list[HumanAuthor]:
        rows = self._db.query("SELECT * FROM human_author ORDER BY name, id")
        return [self._row_to_model(r) for r in rows]

    def update(self, author: HumanAuthor) -> HumanAuthor:
        self._db.execute(
            "UPDATE human_author SET name = ?, color = ?, weight_note = ?, "
            "weight_enabled = ? WHERE id = ?",
            (
                author.name,
                author.color,
                author.weight_note,
                int(author.weight_enabled),
                author.id,
            ),
        )
        return author

    def delete(self, author_id: str) -> None:
        self._db.execute("DELETE FROM human_author WHERE id = ?", (author_id,))


class RoomRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Room:
        return Room.model_construct(
            id=row["id"],
            name=row["name"],
            topic=row["topic"],
            default_reply_mode=ReplyMode(row["default_reply_mode"]),
            archived=bool(row["archived"]),
            created_at=_parse_dt(row["created_at"]),
        )

    def create(self, room: Room) -> Room:
        self._db.execute(
            "INSERT INTO room (id, name, topic, default_reply_mode, archived, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                room.id,
                room.name,
                room.topic,
                room.default_reply_mode.value,
                int(room.archived),
                _dt(room.created_at),
            ),
        )
        return room

    def get(self, room_id: str) -> Room | None:
        rows = self._db.query("SELECT * FROM room WHERE id = ?", (room_id,))
        return self._row_to_model(rows[0]) if rows else None

    def list(self) -> list[Room]:
        rows = self._db.query("SELECT * FROM room ORDER BY created_at, id")
        return [self._row_to_model(r) for r in rows]

    def update(self, room: Room) -> Room:
        self._db.execute(
            "UPDATE room SET name = ?, topic = ?, default_reply_mode = ?, "
            "archived = ? WHERE id = ?",
            (
                room.name,
                room.topic,
                room.default_reply_mode.value,
                int(room.archived),
                room.id,
            ),
        )
        return room

    def delete(self, room_id: str) -> None:
        self._db.execute("DELETE FROM room WHERE id = ?", (room_id,))

    # --- membership (room_persona) ---

    def add_member(self, room_id: str, persona_id: str) -> None:
        rows = self._db.query(
            'SELECT COALESCE(MAX("order") + 1, 0) AS next_order '
            "FROM room_persona WHERE room_id = ?",
            (room_id,),
        )
        next_order = rows[0]["next_order"]
        self._db.execute(
            'INSERT INTO room_persona (room_id, persona_id, "order") VALUES (?, ?, ?)',
            (room_id, persona_id, next_order),
        )

    def list_members(self, room_id: str) -> builtins.list[str]:
        rows = self._db.query(
            'SELECT persona_id FROM room_persona WHERE room_id = ? ORDER BY "order"',
            (room_id,),
        )
        return [r["persona_id"] for r in rows]

    def remove_member(self, room_id: str, persona_id: str) -> None:
        self._db.execute(
            "DELETE FROM room_persona WHERE room_id = ? AND persona_id = ?",
            (room_id, persona_id),
        )


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _row_to_model(self, row: sqlite3.Row) -> Message:
        quoted = [
            q["quoted_message_id"]
            for q in self._db.query(
                "SELECT quoted_message_id FROM message_quote WHERE message_id = ? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return Message.model_construct(
            id=row["id"],
            room_id=row["room_id"],
            author_kind=AuthorKind(row["author_kind"]),
            author_ref=row["author_ref"],
            content=row["content"],
            quoted_message_ids=quoted,
            run_id=row["run_id"],
            created_at=_parse_dt(row["created_at"]),
        )

    def create(self, message: Message) -> Message:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO message (id, room_id, author_kind, author_ref, content, "
                "run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.room_id,
                    message.author_kind.value,
                    message.author_ref,
                    message.content,
                    message.run_id,
                    _dt(message.created_at),
                ),
            )
            if message.quoted_message_ids:
                conn.executemany(
                    "INSERT INTO message_quote (message_id, quoted_message_id) VALUES (?, ?)",
                    [(message.id, q) for q in message.quoted_message_ids],
                )
        return message

    def get(self, message_id: str) -> Message | None:
        rows = self._db.query("SELECT * FROM message WHERE id = ?", (message_id,))
        return self._row_to_model(rows[0]) if rows else None

    def list_for_room(self, room_id: str) -> list[Message]:
        rows = self._db.query(
            "SELECT * FROM message WHERE room_id = ? ORDER BY created_at, id",
            (room_id,),
        )
        return [self._row_to_model(r) for r in rows]


class SessionRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> PersonaSession:
        return PersonaSession.model_construct(
            room_id=row["room_id"],
            persona_id=row["persona_id"],
            provider=Provider(row["provider"]),
            harness_session_id=row["harness_session_id"],
            last_seen_message_id=row["last_seen_message_id"],
            status=row["status"],
        )

    def upsert(self, session: PersonaSession) -> PersonaSession:
        self._db.execute(
            "INSERT INTO persona_session (room_id, persona_id, provider, "
            "harness_session_id, last_seen_message_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(room_id, persona_id) DO UPDATE SET "
            "provider = excluded.provider, "
            "harness_session_id = excluded.harness_session_id, "
            "last_seen_message_id = excluded.last_seen_message_id, "
            "status = excluded.status",
            (
                session.room_id,
                session.persona_id,
                session.provider.value,
                session.harness_session_id,
                session.last_seen_message_id,
                session.status,
            ),
        )
        return session

    def get(self, room_id: str, persona_id: str) -> PersonaSession | None:
        rows = self._db.query(
            "SELECT * FROM persona_session WHERE room_id = ? AND persona_id = ?",
            (room_id, persona_id),
        )
        return self._row_to_model(rows[0]) if rows else None


class RunRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> RunRecord:
        return RunRecord.model_construct(
            run_id=row["run_id"],
            room_id=row["room_id"],
            persona_id=row["persona_id"],
            command_redacted=row["command_redacted"],
            exit_code=row["exit_code"],
            usage=json.loads(row["usage"]),
            log_path=row["log_path"],
            error_kind=row["error_kind"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_opt_dt(row["finished_at"]),
        )

    def create(self, run: RunRecord) -> RunRecord:
        self._db.execute(
            "INSERT INTO run_record (run_id, room_id, persona_id, command_redacted, "
            "exit_code, usage, log_path, error_kind, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.room_id,
                run.persona_id,
                run.command_redacted,
                run.exit_code,
                json.dumps(run.usage),
                run.log_path,
                run.error_kind,
                _dt(run.started_at),
                _dt(run.finished_at) if run.finished_at is not None else None,
            ),
        )
        return run

    def finalize(self, run: RunRecord) -> RunRecord:
        self._db.execute(
            "UPDATE run_record SET command_redacted = ?, exit_code = ?, usage = ?, "
            "log_path = ?, error_kind = ?, finished_at = ? WHERE run_id = ?",
            (
                run.command_redacted,
                run.exit_code,
                json.dumps(run.usage),
                run.log_path,
                run.error_kind,
                _dt(run.finished_at) if run.finished_at is not None else None,
                run.run_id,
            ),
        )
        return run

    def get(self, run_id: str) -> RunRecord | None:
        rows = self._db.query("SELECT * FROM run_record WHERE run_id = ?", (run_id,))
        return self._row_to_model(rows[0]) if rows else None
