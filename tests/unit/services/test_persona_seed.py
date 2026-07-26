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


def test_teammate_prompts_never_encourage_tagging_to_agree() -> None:
    """D14: an @handle SCHEDULES a paid teammate turn. A seed prompt telling
    personas to '@handle when you agree' would turn every polite agreement
    into budget spend — the single highest-risk failure mode of deliberation."""
    for spec in DEFAULT_PERSONAS:
        assert "when you agree" not in spec.system_prompt.lower()


def test_teammate_prompts_teach_reference_vs_request_semantics() -> None:
    for spec in DEFAULT_PERSONAS:
        if spec.handle == DISPATCHER_HANDLE:
            continue
        assert "take a turn" in spec.system_prompt  # @ = request-a-turn is taught


def test_dispatcher_prompt_includes_wrapup_duty() -> None:
    disp = next(s for s in DEFAULT_PERSONAS if s.handle == DISPATCHER_HANDLE)
    assert "wrap up" in disp.system_prompt.lower()


def test_critic_prompt_mandates_dissent() -> None:
    critic = next(s for s in DEFAULT_PERSONAS if s.handle == "critic")
    assert "may not simply agree" in critic.system_prompt
