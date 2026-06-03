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


def test_init_schema_migrates_legacy_persona_table_adding_job(tmp_path: Path) -> None:
    # Simulate a pre-`job` database: create the persona table WITHOUT the job
    # column and insert a row, then init_schema() must add the column (idempotent
    # migration) and leave the existing row intact with the default job ''.
    db = Database(tmp_path / "t.db")
    db.execute(
        "CREATE TABLE persona (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "handle TEXT NOT NULL UNIQUE, color TEXT NOT NULL, provider TEXT NOT NULL, "
        "model TEXT NOT NULL, effort TEXT, system_prompt TEXT NOT NULL DEFAULT '', "
        "mcp_servers TEXT NOT NULL DEFAULT '[]', allowed_tools TEXT NOT NULL DEFAULT '[]', "
        "working_dir TEXT, permission_mode TEXT NOT NULL, is_template INTEGER NOT NULL "
        "DEFAULT 0, created_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO persona (id, name, handle, color, provider, model, permission_mode, "
        "created_at) VALUES ('p1','Old','old','#fff','mock','m','read-only',"
        "'2026-01-01T00:00:00+00:00')"
    )
    cols_before = {r["name"] for r in db.query("PRAGMA table_info(persona)")}
    assert "job" not in cols_before

    db.init_schema()

    cols_after = {r["name"] for r in db.query("PRAGMA table_info(persona)")}
    assert "job" in cols_after
    row = db.query("SELECT job FROM persona WHERE id = 'p1'")[0]
    assert row["job"] == ""

    # Idempotent: a second init_schema() must not raise (column already present).
    db.init_schema()


def test_init_schema_migrates_legacy_room_adding_delegation(tmp_path: Path) -> None:
    # Pre-`delegation_enabled` room table: init_schema() must add the column with
    # a default of 1 (enabled) and leave existing rows intact.
    db = Database(tmp_path / "t.db")
    db.execute(
        "CREATE TABLE room (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "topic TEXT NOT NULL DEFAULT '', default_reply_mode TEXT NOT NULL, "
        "archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO room (id, name, default_reply_mode, created_at) "
        "VALUES ('r1','Old','sequential','2026-01-01T00:00:00+00:00')"
    )
    assert "delegation_enabled" not in {r["name"] for r in db.query("PRAGMA table_info(room)")}

    db.init_schema()

    assert "delegation_enabled" in {r["name"] for r in db.query("PRAGMA table_info(room)")}
    assert (
        db.query("SELECT delegation_enabled FROM room WHERE id = 'r1'")[0]["delegation_enabled"]
        == 1
    )


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
