from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain.events import StreamEvent
from app.domain.models import PermissionMode, Provider


class RunSpec(BaseModel):
    """Everything a backend adapter needs to start (or resume) one persona run.

    This is the single typed boundary handed to the harness layer. Services build
    it from a persona's config plus the assembled prompt; the adapter translates it
    into the concrete CLI invocation.
    """

    prompt: str
    provider: Provider
    model: str
    system_prompt: str = ""
    effort: str | None = None
    resume_session_id: str | None = None
    mcp_servers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY


@runtime_checkable
class AgentBackend(Protocol):
    """A pluggable CLI harness backend.

    ``run`` and ``send_command`` are async generators yielding normalized
    ``StreamEvent``s; declared here as returning ``AsyncIterator[StreamEvent]``.
    """

    name: str
    supported_commands: set[str]

    def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]: ...

    def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]: ...
