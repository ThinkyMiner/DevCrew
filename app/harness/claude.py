"""Claude Code CLI harness adapter.

Translates a :class:`RunSpec` into a ``claude --print --output-format
stream-json --verbose`` invocation, streams stdout through the pure
:mod:`app.harness.claude_parser`, and normalizes everything to
:class:`StreamEvent`s. This is the ONLY place that knows how to *spawn* Claude;
all wire-format knowledge stays in the parser module.

Subprocess access goes through an injectable ``spawn`` callable so tests never
touch a real process (AGENTS §5). The default spawn uses ``asyncio``.

Terminal-event design (see claude_parser): the parser never emits ``RunDone``.
This adapter tracks the session id across lines and emits exactly one terminal
``RunDone(session_id=...)`` after the stream ends.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.domain.errors import HarnessAuthError, HarnessError, HarnessTimeout
from app.domain.events import RunDone, StreamEvent
from app.harness.base import RunSpec
from app.harness.claude_parser import parse_claude_line, session_id_of

_DEFAULT_TIMEOUT = 600.0

# Patterns that flag stderr/auth failures requiring re-login rather than a retry.
_AUTH_PATTERNS = (
    "not authenticated",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "401",
)

# Redaction: scrub obvious token-/key-like material before it lands in errors/logs
# (AGENTS §4.4 — no secrets in logs). Best-effort, intentionally conservative.
_REDACT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),  # OpenAI/Anthropic-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),  # GitHub tokens
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),  # long opaque blobs
)

_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    """Replace obvious secret-like substrings with ``[REDACTED]``."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


@runtime_checkable
class Proc(Protocol):
    """A spawned process the adapter streams from.

    A fake implementation in tests yields the fixture lines from
    :attr:`stdout_lines`, then reports :attr:`returncode`/:attr:`stderr_text`.
    """

    def stdout_lines(self) -> AsyncIterator[str]:
        """Async iterator over decoded stdout lines (newline-stripped)."""
        ...

    async def wait(self) -> int:
        """Await process exit and return the exit code."""
        ...

    async def stderr_text(self) -> str:
        """Return accumulated stderr (decoded) once the stream has ended."""
        ...


Spawn = Callable[[list[str], str | None], Awaitable[Proc]]


class _AsyncioProc:
    """Default :class:`Proc` backed by an ``asyncio`` subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def stdout_lines(self) -> AsyncIterator[str]:
        assert self._process.stdout is not None
        async for raw in self._process.stdout:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")

    async def wait(self) -> int:
        return await self._process.wait()

    async def stderr_text(self) -> str:
        if self._process.stderr is None:
            return ""
        data = await self._process.stderr.read()
        return data.decode("utf-8", errors="replace")


async def _default_spawn(argv: list[str], cwd: str | None) -> Proc:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    return _AsyncioProc(process)


class ClaudeHarness:
    """:class:`AgentBackend` for the Claude Code CLI."""

    name = "claude"
    supported_commands: set[str] = {"/compact", "/clear"}

    def __init__(
        self,
        spawn: Spawn = _default_spawn,
        claude_bin: str = "claude",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._spawn = spawn
        self._claude_bin = claude_bin
        self._timeout = timeout

    # -- argv construction ------------------------------------------------------

    def _mcp_args(self, mcp_servers: list[str]) -> list[str]:
        """Build ``--mcp-config`` args for the requested MCP servers.

        Approach (kept simple, documented per the plan): ``mcp_servers`` holds
        names of servers already configured in the user's Claude config, so we
        pass them through with a single ``--mcp-config`` whose value is a JSON
        object naming them. Claude merges this with its own config. If/when
        richer per-server wiring is needed this is the one place to extend.

        TODO(OQ-1/MCP): the exact ``--mcp-config`` payload Claude expects in
        headless mode is unverified against a live MCP run; this passes a
        minimal name-list config and must be validated end-to-end.
        """
        if not mcp_servers:
            return []
        config: dict[str, object] = {"mcpServers": {name: {} for name in mcp_servers}}
        return ["--mcp-config", json.dumps(config)]

    def _build_argv(self, spec: RunSpec, prompt: str) -> list[str]:
        argv = [
            self._claude_bin,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            spec.model,
        ]
        if spec.resume_session_id:
            argv += ["--resume", spec.resume_session_id]
        if spec.system_prompt:
            argv += ["--append-system-prompt", spec.system_prompt]
        if spec.effort:
            argv += ["--effort", spec.effort]
        argv += ["--permission-mode", spec.permission_mode.value]
        if spec.working_dir:
            argv += ["--add-dir", spec.working_dir]
        argv += self._mcp_args(spec.mcp_servers)
        argv.append(prompt)
        return argv

    def _resume_argv(self, session_id: str, command: str) -> list[str]:
        return [
            self._claude_bin,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--resume",
            session_id,
            command,
        ]

    # -- streaming core ---------------------------------------------------------

    async def _stream(
        self, argv: list[str], cwd: str | None, fallback_session_id: str | None
    ) -> AsyncIterator[StreamEvent]:
        """Spawn ``argv`` and yield normalized events, ending with one RunDone.

        ``fallback_session_id`` seeds the terminal RunDone (e.g. the resumed id)
        in case the stream never surfaces one.
        """
        proc = await self._spawn(argv, cwd)
        session_id = fallback_session_id

        try:
            async with asyncio.timeout(self._timeout):
                async for line in proc.stdout_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # non-JSON noise — ignore, never crash
                    if not isinstance(obj, dict):
                        continue
                    sid = session_id_of(obj)
                    if sid is not None:
                        session_id = sid
                    for event in parse_claude_line(obj):
                        yield event
                exit_code = await proc.wait()
        except TimeoutError as exc:
            raise HarnessTimeout(f"claude run exceeded {self._timeout}s timeout") from exc

        if exit_code != 0:
            stderr = _redact(await proc.stderr_text())
            lowered = stderr.lower()
            if any(pat in lowered for pat in _AUTH_PATTERNS):
                raise HarnessAuthError(f"claude not authenticated (exit {exit_code}): {stderr}")
            raise HarnessError(f"claude exited {exit_code}: {stderr}")

        yield RunDone(session_id=session_id)

    # -- AgentBackend implementation -------------------------------------------

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
        argv = self._build_argv(spec, spec.prompt)
        async for event in self._stream(argv, spec.working_dir, spec.resume_session_id):
            yield event

    async def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]:
        if command not in self.supported_commands:
            raise HarnessError(
                f"command {_redact(command)!r} not supported by {self.name} "
                f"(supported: {sorted(self.supported_commands)})"
            )
        # TODO(OQ-1): exact /compact (& /clear) mechanics in headless --print
        # mode are unverified. We resume the session and pass the slash command
        # as the prompt; if Claude does not honor it this must degrade
        # gracefully (it will still stream/terminate normally rather than hang).
        argv = self._resume_argv(session_id, command)
        async for event in self._stream(argv, None, session_id):
            yield event
