from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.health as health_mod
from app.api.app_factory import create_app
from app.config.settings import Settings
from app.domain.models import Provider
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "team.db",
        logs_dir=data_dir / "logs",
    )


@pytest.fixture
def registry() -> BackendRegistry:
    """A registry with ONLY MockHarness — never the real claude/codex CLIs."""
    reg = BackendRegistry()
    mock = MockHarness()
    reg.register(Provider.MOCK, mock)
    reg.register(Provider.CLAUDE, mock)
    reg.register(Provider.CODEX, mock)
    return reg


@pytest.fixture
def client(test_settings, registry, monkeypatch) -> TestClient:
    # Health route resolves CLIs via shutil.which — fake them present so tests
    # never depend on a real claude/codex install.
    monkeypatch.setattr(health_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    # seed_personas=False: these tests build their own personas, so the seeded
    # team would collide on handles and add noise to membership/routing asserts.
    # The seeded path is covered explicitly in test_rest.py::test_seeded_team_*.
    app = create_app(test_settings, registry=registry, seed_personas=False)
    return TestClient(app)
