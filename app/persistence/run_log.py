from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.domain.events import StreamEvent


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RunLogWriter:
    """Appends StreamEvents as one JSON line each to a single run's log file.

    The full harness stream for a run lives here (FR-D3); each line round-trips
    back through `app.domain.events.parse_event`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write_event(self, event: StreamEvent) -> None:
        line = json.dumps(event.model_dump(), default=str)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class RunLogStore:
    """Filesystem JSONL run-log store.

    Layout: ``base_dir/<room>/<persona>/<ISO-ts>-<run_id>.jsonl``. The timestamp
    comes from an injected clock so tests are deterministic.
    """

    def __init__(
        self, base_dir: str | Path, clock: Callable[[], datetime] = _default_clock
    ) -> None:
        self._base_dir = Path(base_dir)
        self._clock = clock

    def open(self, room_id: str, persona_id: str, run_id: str) -> RunLogWriter:
        directory = self._base_dir / room_id / persona_id
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = self._clock().isoformat()
        path = directory / f"{timestamp}-{run_id}.jsonl"
        return RunLogWriter(path)
