"""Tests for run_scope — the RunRecord lifecycle context manager.

The interface is the test surface: drive ``run_scope`` with fake RunRepo /
RunLogStore and assert the record is created on entry and finalized on every
exit path (success, error_kind set, exception), and that the run_id ContextVar
is bound during the block and reset afterwards.
"""

from __future__ import annotations

import pytest

from app.config.logging import run_id_var
from app.domain.events import StreamEvent
from app.domain.models import RunRecord
from app.services.run_scope import run_scope


class FakeWriter:
    def __init__(self, path: str) -> None:
        self.path = path
        self.events: list[StreamEvent] = []

    def write_event(self, event: StreamEvent) -> None:
        self.events.append(event)


class FakeRunLog:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str, str]] = []

    def open(self, room_id: str, persona_id: str, run_id: str) -> FakeWriter:
        self.opened.append((room_id, persona_id, run_id))
        return FakeWriter(f"/logs/{room_id}/{persona_id}/{run_id}.jsonl")


class FakeRunRepo:
    def __init__(self) -> None:
        self.created: list[RunRecord] = []
        self.finalized: list[RunRecord] = []

    def create(self, run: RunRecord) -> RunRecord:
        self.created.append(run)
        return run

    def finalize(self, run: RunRecord) -> RunRecord:
        self.finalized.append(run)
        return run


def _scope(runs: FakeRunRepo, log: FakeRunLog):  # type: ignore[no-untyped-def]
    return run_scope(
        runs,  # type: ignore[arg-type]
        log,  # type: ignore[arg-type]
        room_id="r1",
        persona_id="p1",
        command_redacted="claude run resume=False",
    )


def test_creates_record_and_opens_writer_on_entry() -> None:
    runs, log = FakeRunRepo(), FakeRunLog()
    with _scope(runs, log) as (run, writer):
        assert runs.created == [run]
        assert log.opened == [("r1", "p1", run.run_id)]
        assert run.log_path == writer.path
        assert run.finished_at is None  # not finalized yet


def test_finalizes_success_with_exit_code_zero() -> None:
    runs, log = FakeRunRepo(), FakeRunLog()
    with _scope(runs, log) as (run, _writer):
        pass
    assert runs.finalized == [run]
    assert run.exit_code == 0
    assert run.error_kind is None
    assert run.finished_at is not None


def test_error_kind_set_by_body_yields_exit_code_one() -> None:
    runs, log = FakeRunRepo(), FakeRunLog()
    with _scope(runs, log) as (run, _writer):
        run.error_kind = "HarnessError"
    assert run.exit_code == 1
    assert run.error_kind == "HarnessError"
    assert runs.finalized == [run]


def test_finalizes_even_when_body_raises() -> None:
    runs, log = FakeRunRepo(), FakeRunLog()
    with pytest.raises(ValueError):
        with _scope(runs, log) as (run, _writer):
            raise ValueError("boom")
    # The finally guarantee: the record is still finalized on an exception path.
    assert runs.finalized == [run]
    assert run.finished_at is not None


def test_run_id_var_bound_during_block_and_reset_after() -> None:
    assert run_id_var.get() is None
    runs, log = FakeRunRepo(), FakeRunLog()
    with _scope(runs, log) as (run, _writer):
        assert run_id_var.get() == run.run_id
    assert run_id_var.get() is None
