from __future__ import annotations

import pytest

from app.persistence.db import Database
from app.persistence.repositories import PersonaRepo
from app.services.persona_seed import (
    DEFAULT_PERSONAS,
    DISPATCHER_HANDLE,
    ensure_default_personas,
)


@pytest.fixture
def repo(tmp_path) -> PersonaRepo:
    db = Database(tmp_path / "t.db")
    db.init_schema()
    return PersonaRepo(db)


def test_seeds_named_team_and_dispatcher(repo: PersonaRepo) -> None:
    ensure_default_personas(repo)
    by_handle = {p.handle: p for p in repo.list()}
    assert DISPATCHER_HANDLE in by_handle
    assert "architect" in by_handle
    # human first-name lives in `name`; the role label lives in `job`.
    ada = by_handle["architect"]
    assert ada.name == "Ada"
    assert ada.job
    # the seeded team is directly usable (addable to rooms), not templates.
    assert all(not p.is_template for p in by_handle.values())


def test_dispatcher_delegates_by_tagging_teammates(repo: PersonaRepo) -> None:
    ensure_default_personas(repo)
    disp = repo.get_by_handle(DISPATCHER_HANDLE)
    assert disp is not None
    # It must instruct itself to tag teammates by @handle (delegation is how the
    # orchestrator turns its reply into teammate turns).
    assert "@handle" in disp.system_prompt or "@" in disp.system_prompt


def test_idempotent_no_duplicate_handles(repo: PersonaRepo) -> None:
    ensure_default_personas(repo)
    n = len(repo.list())
    ensure_default_personas(repo)
    handles = [p.handle for p in repo.list()]
    assert len(handles) == len(set(handles))
    assert len(repo.list()) == n


def test_every_spec_is_complete() -> None:
    seen: set[str] = set()
    for spec in DEFAULT_PERSONAS:
        assert spec.handle and spec.name and spec.job and spec.system_prompt
        assert spec.handle not in seen, f"duplicate seed handle {spec.handle}"
        seen.add(spec.handle)
