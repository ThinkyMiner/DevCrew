from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# A SQLite-bindable parameter value.
SqlParam = str | int | float | bytes | None


class Database:
    """Thin typed wrapper around stdlib sqlite3.

    Opens a single connection with `row_factory=sqlite3.Row`, foreign-key
    enforcement on, and WAL journaling. This is the only place the rest of the
    persistence layer talks to sqlite.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)
        self._conn.commit()

    def query(self, sql: str, params: Sequence[SqlParam] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

    def execute(self, sql: str, params: Sequence[SqlParam] = ()) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def executemany(self, sql: str, rows: Sequence[Sequence[SqlParam]]) -> None:
        self._conn.executemany(sql, rows)
        self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements atomically: commit on success, roll back on any error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()
