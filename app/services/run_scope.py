"""The run-record lifecycle as one context manager.

A persona turn and a session control command both need the exact same
bookkeeping around their stream: create a :class:`RunRecord`, bind the
``run_id`` ContextVar (so structured logs carry it), open the run-log writer,
and — on every exit path, success or failure — finalize the record (exit code,
finish time, persist) and reset the ContextVar.

That bookkeeping used to be copy-pasted verbatim into both
``ChatOrchestrator._run_turn`` and ``ChatOrchestrator.send_control``. It now
lives here once, so a finalize rule changes in one place and no run can skip the
``finally``.

Scope discipline (deliberately narrow): this owns only the record lifecycle. The
caller's body still does the streaming AND the error *emission* (yielding a
``RunError`` event, persisting the ``[error:]`` marker) — a context manager can't
yield stream events. The body simply sets ``run.error_kind`` (and ``run.usage``)
on the yielded record; the scope finalizes whatever is set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from app.config.logging import run_id_var
from app.domain.models import RunRecord
from app.persistence.repositories import RunRepo
from app.persistence.run_log import RunLogStore, RunLogWriter


@contextmanager
def run_scope(
    runs: RunRepo,
    run_log: RunLogStore,
    *,
    room_id: str,
    persona_id: str,
    command_redacted: str,
) -> Iterator[tuple[RunRecord, RunLogWriter]]:
    """Open a RunRecord + run-log writer, finalize both on exit.

    Yields ``(run, writer)``. The body sets ``run.error_kind`` on failure and
    ``run.usage`` as usage events arrive; on exit the scope derives
    ``exit_code`` from ``error_kind``, stamps ``finished_at``, persists the
    record, and resets the ``run_id`` ContextVar — even if the body raises,
    returns, or the consuming generator is closed early.
    """
    run = RunRecord(
        room_id=room_id,
        persona_id=persona_id,
        command_redacted=command_redacted,
    )
    token = run_id_var.set(run.run_id)
    writer = run_log.open(room_id, persona_id, run.run_id)
    run.log_path = str(writer.path)
    runs.create(run)
    try:
        yield run, writer
    finally:
        run.exit_code = 0 if run.error_kind is None else 1
        run.finished_at = datetime.now(UTC)
        runs.finalize(run)
        run_id_var.reset(token)
