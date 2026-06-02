from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.persistence.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()
