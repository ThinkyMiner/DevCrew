"""Backend registry: maps a :class:`Provider` to a concrete :class:`AgentBackend`.

Kept deliberately small and explicit (AGENTS §4 fail-loud, no global mutable
surprises):

* :class:`BackendRegistry` is an isolated, instance-scoped registry. Tests
  construct their own and register a :class:`MockHarness` for
  :data:`Provider.MOCK`; nothing leaks between registries.
* :func:`default_registry` builds the composition-root registry with the real
  CLI adapters wired for ``CLAUDE`` and ``CODEX``. It returns a FRESH registry
  each call (no module-level singleton) so callers/tests cannot accidentally
  mutate shared state. The app composition root calls it once and injects the
  result.

Resolution of an unregistered provider raises :class:`ProviderUnavailable`
(typed, fail-loud) rather than returning ``None``.
"""

from __future__ import annotations

from app.domain.errors import ProviderUnavailable
from app.domain.models import Provider
from app.harness.base import AgentBackend
from app.harness.claude import ClaudeHarness
from app.harness.codex import CodexHarness


class BackendRegistry:
    """An isolated provider -> backend registry."""

    def __init__(self) -> None:
        self._backends: dict[Provider, AgentBackend] = {}

    def register(self, provider: Provider, backend: AgentBackend) -> None:
        """Register (or replace) the backend for ``provider``."""
        self._backends[provider] = backend

    def get_backend(self, provider: Provider) -> AgentBackend:
        """Return the backend for ``provider`` or raise ``ProviderUnavailable``."""
        try:
            return self._backends[provider]
        except KeyError as exc:
            raise ProviderUnavailable(
                f"no backend registered for provider {provider.value!r}"
            ) from exc

    def list_models(self) -> dict[str, list[str]]:
        """Provider value -> its backend's ``supported_models``, for every wired
        backend. This is the single source for the persona editor's model picker:
        the list follows whatever adapters are actually registered, so it can
        never drift from what the backends really accept."""
        return {
            provider.value: list(backend.supported_models)
            for provider, backend in self._backends.items()
        }


def build_default_registry(scratch_dir: str | None = None) -> BackendRegistry:
    """Build a fresh registry wired with the real CLI adapters.

    ``CLAUDE`` -> :class:`ClaudeHarness`, ``CODEX`` -> :class:`CodexHarness`.
    ``MOCK`` is intentionally left unregistered (tests inject it) so a mock
    backend can never be shipped by accident.

    ``scratch_dir`` is the neutral, empty directory used as the spawn cwd for
    personas with no bound ``working_dir`` (persona environment isolation). The
    composition root passes its ``settings.resolved_scratch_dir`` here so an
    advisory persona's claude/codex child runs in a doc-free directory instead of
    the server's project cwd. When ``None``, the adapters fall back to their
    pre-isolation behavior (back-compat).
    """
    registry = BackendRegistry()
    registry.register(Provider.CLAUDE, ClaudeHarness(scratch_dir=scratch_dir))
    registry.register(Provider.CODEX, CodexHarness(scratch_dir=scratch_dir))
    return registry


def default_registry() -> BackendRegistry:
    """Back-compat alias for :func:`build_default_registry` with no scratch dir."""
    return build_default_registry()
