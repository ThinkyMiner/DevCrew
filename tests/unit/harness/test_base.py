from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.events import StreamEvent
from app.domain.models import PermissionMode, Provider
from app.harness.base import AgentBackend, RunSpec


def test_runspec_holds_persona_config() -> None:
    spec = RunSpec(
        prompt="hi",
        model="opus",
        provider=Provider.CLAUDE,
        resume_session_id=None,
        system_prompt="be terse",
        effort="high",
        mcp_servers=[],
        allowed_tools=[],
        working_dir=None,
        permission_mode=PermissionMode.READ_ONLY,
    )
    assert spec.prompt == "hi"
    assert spec.provider is Provider.CLAUDE
    assert spec.model == "opus"
    assert spec.system_prompt == "be terse"
    assert spec.effort == "high"
    assert spec.permission_mode is PermissionMode.READ_ONLY


def test_runspec_defaults() -> None:
    spec = RunSpec(prompt="hi", provider=Provider.MOCK, model="m")
    assert spec.system_prompt == ""
    assert spec.effort is None
    assert spec.resume_session_id is None
    assert spec.mcp_servers == []
    assert spec.allowed_tools == []
    assert spec.working_dir is None
    assert spec.permission_mode is PermissionMode.READ_ONLY


def test_protocol_is_runtime_checkable() -> None:
    class Dummy:
        name = "dummy"
        supported_commands: set[str] = set()

        async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
            if False:  # pragma: no cover
                yield  # type: ignore[unreachable]

        async def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]:
            if False:  # pragma: no cover
                yield  # type: ignore[unreachable]

    assert isinstance(Dummy(), AgentBackend)


def test_non_conforming_class_is_not_a_backend() -> None:
    class NotABackend:
        name = "nope"

    assert not isinstance(NotABackend(), AgentBackend)
