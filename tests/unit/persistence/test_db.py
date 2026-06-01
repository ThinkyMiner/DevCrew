from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence.db import Database


def test_schema_creates_all_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "persona",
        "human_author",
        "room",
        "room_persona",
        "message",
        "message_quote",
        "persona_session",
        "run_record",
    } <= tables


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    assert db.query("PRAGMA foreign_keys")[0]["foreign_keys"] == 1


def test_transaction_commits(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO room (id, name, topic, default_reply_mode, archived, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", "Room", "", "sequential", 0, "2026-06-01T00:00:00+00:00"),
        )
    assert db.query("SELECT id FROM room")[0]["id"] == "r1"


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    with pytest.raises(ValueError):  # noqa: PT012
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO room (id, name, topic, default_reply_mode, archived, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("r1", "Room", "", "sequential", 0, "2026-06-01T00:00:00+00:00"),
            )
            raise ValueError("boom")
    assert db.query("SELECT id FROM room") == []
