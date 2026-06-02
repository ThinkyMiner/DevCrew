from __future__ import annotations

import sqlite3
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


def test_close_is_idempotent_and_releases_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.close()
    # Second close must not raise (safe to call twice, e.g. shutdown hook).
    db.close()
    # Connection is actually closed: a query now raises ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        db.query("SELECT 1")


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


def test_execute_inside_transaction_does_not_prematurely_commit(tmp_path: Path) -> None:
    # Regression (I1): a db.execute() issued while a transaction() block is open
    # must NOT commit the in-flight transactional work. If the block then raises,
    # the rollback must discard BOTH the transactional write and the execute().
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError):  # noqa: PT012
        with db.transaction() as conn:
            conn.execute("INSERT INTO t (id) VALUES (?)", ("r1",))
            db.execute("INSERT INTO t (id) VALUES (?)", ("r2",))
            raise RuntimeError("boom")

    # Neither r1 nor r2 should have survived: both rolled back.
    assert db.query("SELECT COUNT(*) AS n FROM t")[0]["n"] == 0


def test_committed_transaction_persists_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")

    with db.transaction() as conn:
        conn.execute("INSERT INTO t (id) VALUES (?)", ("r1",))
        db.execute("INSERT INTO t (id) VALUES (?)", ("r2",))

    assert db.query("SELECT COUNT(*) AS n FROM t")[0]["n"] == 2
