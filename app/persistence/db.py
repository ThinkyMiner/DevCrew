from __future__ import annotations

import sqlite3
import threading
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
        # Reentrant lock serializes all DB access on the shared
        # check_same_thread=False connection (correctness over throughput for
        # this local single-user app). Reentrancy lets execute()/executemany()
        # be called from inside a transaction() block without deadlocking.
        self._lock = threading.RLock()
        # Transaction nesting depth: writes commit only when this is 0.
        self._in_tx = 0

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)
        self._conn.commit()

    def query(self, sql: str, params: Sequence[SqlParam] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def execute(self, sql: str, params: Sequence[SqlParam] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            if self._in_tx == 0:
                self._conn.commit()

    def executemany(self, sql: str, rows: Sequence[Sequence[SqlParam]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            if self._in_tx == 0:
                self._conn.commit()

    def execute_returning_rowcount(self, sql: str, params: Sequence[SqlParam] = ()) -> int:
        """Like execute() but returns the statement's affected-row count.

        Lock-guarded and transaction-aware exactly like execute(): commits only
        at the outermost level. The cursor's rowcount is read while still under
        the lock so it cannot be clobbered by a concurrent statement.
        """
        with self._lock:
            cursor = self._conn.execute(sql, params)
            rowcount = cursor.rowcount
            if self._in_tx == 0:
                self._conn.commit()
            return rowcount

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements atomically: commit on success, roll back on any error.

        Nesting-aware: only the outermost block commits on success. Any
        exception rolls back the whole (possibly nested) transaction and
        re-raises. The reentrant lock serializes the entire block so
        execute()/executemany() called within it stay non-committing.
        """
        with self._lock:
            self._in_tx += 1
            try:
                yield self._conn
                if self._in_tx == 1:
                    self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._in_tx -= 1

    def close(self) -> None:
        """Close the underlying sqlite connection. Idempotent and lock-guarded.

        Safe to call more than once (e.g. an explicit shutdown after a context
        manager already closed it): sqlite's ``close()`` tolerates a re-close, and
        the lock serializes it against any in-flight query/execute.
        """
        with self._lock:
            self._conn.close()
