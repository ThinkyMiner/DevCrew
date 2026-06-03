from __future__ import annotations

import pytest

from app.domain.errors import ProviderUnavailable
from app.domain.models import Provider
from app.harness.base import AgentBackend
from app.harness.claude import ClaudeHarness
from app.harness.codex import CodexHarness
from app.harness.mock import MockHarness
from app.harness.registry import (
    BackendRegistry,
    build_default_registry,
    default_registry,
)


def test_register_and_get_mock() -> None:
    reg = BackendRegistry()
    mock = MockHarness()
    reg.register(Provider.MOCK, mock)
    got = reg.get_backend(Provider.MOCK)
    assert got is mock
    assert isinstance(got, AgentBackend)


def test_unregistered_provider_raises_provider_unavailable() -> None:
    reg = BackendRegistry()
    with pytest.raises(ProviderUnavailable):
        reg.get_backend(Provider.CLAUDE)


def test_register_overwrites() -> None:
    reg = BackendRegistry()
    first = MockHarness()
    second = MockHarness()
    reg.register(Provider.MOCK, first)
    reg.register(Provider.MOCK, second)
    assert reg.get_backend(Provider.MOCK) is second


def test_isolated_registries_do_not_share_state() -> None:
    a = BackendRegistry()
    b = BackendRegistry()
    a.register(Provider.MOCK, MockHarness())
    with pytest.raises(ProviderUnavailable):
        b.get_backend(Provider.MOCK)


def test_default_registry_resolves_claude_and_codex() -> None:
    reg = default_registry()
    claude = reg.get_backend(Provider.CLAUDE)
    codex = reg.get_backend(Provider.CODEX)
    assert isinstance(claude, ClaudeHarness)
    assert isinstance(codex, CodexHarness)
    assert isinstance(claude, AgentBackend)
    assert isinstance(codex, AgentBackend)


def test_default_registry_mock_unregistered_until_injected() -> None:
    # The default (composition-root) registry wires real CLIs; MOCK is for tests
    # to inject explicitly so we never accidentally ship a mock backend.
    reg = default_registry()
    with pytest.raises(ProviderUnavailable):
        reg.get_backend(Provider.MOCK)
    reg.register(Provider.MOCK, MockHarness())
    assert isinstance(reg.get_backend(Provider.MOCK), MockHarness)


def test_default_registry_returns_independent_instances() -> None:
    # Each call builds a fresh registry so tests cannot leak registrations into
    # one another (avoid global mutable surprises).
    assert default_registry() is not default_registry()


def test_build_default_registry_threads_scratch_dir_into_adapters() -> None:
    # Persona isolation: the scratch dir must reach BOTH real adapters so an
    # unbound persona spawns its child in the neutral dir.
    reg = build_default_registry("/neutral/scratch")
    claude = reg.get_backend(Provider.CLAUDE)
    codex = reg.get_backend(Provider.CODEX)
    assert isinstance(claude, ClaudeHarness)
    assert isinstance(codex, CodexHarness)
    assert claude._scratch_dir == "/neutral/scratch"
    assert codex._scratch_dir == "/neutral/scratch"


def test_build_default_registry_defaults_to_no_scratch() -> None:
    reg = build_default_registry()
    claude = reg.get_backend(Provider.CLAUDE)
    assert isinstance(claude, ClaudeHarness)
    assert claude._scratch_dir is None
