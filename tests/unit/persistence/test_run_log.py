from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.domain.events import RunDone, TextDelta, Usage, parse_event
from app.persistence.run_log import RunLogStore


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 1, 12, 30, 45, tzinfo=UTC)


def test_writer_path_uses_clock_room_persona_run(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path, clock=_fixed_clock)
    writer = store.open("room1", "persona1", "run-abc")
    expected = tmp_path / "room1" / "persona1" / f"{_fixed_clock().isoformat()}-run-abc.jsonl"
    assert writer.path == expected


def test_writes_three_events_as_jsonl_lines(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path, clock=_fixed_clock)
    writer = store.open("room1", "persona1", "run-abc")
    events = [
        TextDelta(text="hello"),
        Usage(input_tokens=1, output_tokens=2),
        RunDone(session_id="sess-9"),
    ]
    for e in events:
        writer.write_event(e)

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [parse_event(json.loads(line)) for line in lines]
    assert parsed == events


def test_open_creates_parent_dirs(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path, clock=_fixed_clock)
    writer = store.open("r", "p", "run-1")
    writer.write_event(TextDelta(text="x"))
    assert writer.path.exists()
    assert writer.path.parent == tmp_path / "r" / "p"
