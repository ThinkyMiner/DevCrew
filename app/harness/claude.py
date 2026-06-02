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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
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
# Order matters: more-specific patterns first so a general one cannot consume a
# substring that a specific one is meant to catch (M1).
_REDACT_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # Anthropic keys (specific)
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),  # other OpenAI-style keys (general)
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),  # GitHub tokens
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),  # long opaque blobs
)

# Raise the StreamReader line limit well above the default 64 KiB so a single
# large stream-json line (big tool_result / assistant message) does not trip
# LimitOverrunError mid-stream (I2). Lines longer than this still convert to a
# typed HarnessError rather than escaping as a raw ValueError.
_STDOUT_LINE_LIMIT = 8 * 1024 * 1024

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
        """Return accumulated stderr (decoded).

        Implementations MUST drain stderr concurrently with stdout (so a child
        writing a large volume to stderr cannot deadlock against a full pipe).
        This call awaits that drain finishing and returns the buffered text.
        """
        ...

    async def kill(self) -> None:
        """Terminate and reap the process; idempotent and safe after exit.

        Called from a ``finally`` on every exit path so a timeout, exception, or
        early generator close cannot leak the child or its pipes (C2).
        """
        ...


Spawn = Callable[[list[str], str | None], Awaitable[Proc]]


class _AsyncioProc:
    """Default :class:`Proc` backed by an ``asyncio`` subprocess.

    stderr is drained into an in-memory buffer by a background task started at
    construction time, so the child can never block writing stderr while we are
    still reading stdout (C1).
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stderr_buf = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
            # Constructed inside an async spawn, so a running loop is guaranteed.
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        """Read stderr to EOF into the in-memory buffer (runs concurrently)."""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                self._stderr_buf.extend(chunk)
        except ValueError:
            # A read-limit error on stderr we don't want to leak; whatever was
            # buffered so far is still returned by stderr_text().
            return

    async def stdout_lines(self) -> AsyncIterator[str]:
        stdout = self._process.stdout
        if stdout is None:
            raise HarnessError("claude subprocess has no stdout pipe")
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # An over-long line blows past the StreamReader limit; convert to
                # a typed, redacted error instead of leaking a raw ValueError (I2).
                raise HarnessError(
                    f"claude produced an over-long stdout line: {_redact(str(exc))}"
                ) from exc
            if not raw:
                break
            yield raw.decode("utf-8", errors="replace").rstrip("\n")

    async def wait(self) -> int:
        return await self._process.wait()

    async def stderr_text(self) -> str:
        if self._stderr_task is not None:
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        return self._stderr_buf.decode("utf-8", errors="replace")

    async def kill(self) -> None:
        # Terminate/kill the child if it is still running, then reap it so we
        # don't leak a zombie or its pipes. Idempotent (C2).
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass  # already gone
            # Drain any data still sitting in the stdout pipe before wait().
            # asyncio's subprocess wait() can deadlock while a PIPE remains full
            # (e.g. after a stdout limit overrun, the unread bytes block exit
            # reaping); reading to EOF releases it.
            stdout = self._process.stdout
            if stdout is not None:
                try:
                    while await stdout.read(65536):
                        pass
                except (ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                    pass
            try:
                await self._process.wait()
            except ProcessLookupError:
                pass
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass


async def _default_spawn(argv: list[str], cwd: str | None) -> Proc:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=_STDOUT_LINE_LIMIT,
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
            # `--effort <level>` is verified to exist in claude-cli 2.1.159
            # (`claude --help` lists it; accepted levels: low, medium, high,
            # xhigh, max). This is NOT an unverified open question — no hard
            # validation here, the CLI rejects unknown levels fail-loud.
            argv += ["--effort", spec.effort]
        argv += ["--permission-mode", spec.permission_mode.value]
        if spec.working_dir:
            argv += ["--add-dir", spec.working_dir]
        argv += self._mcp_args(spec.mcp_servers)
        # End-of-options separator (C1): the prompt is a trailing positional, so a
        # prompt beginning with "-" (e.g. "--version") would otherwise be parsed
        # as a flag. "--" forces everything after it to be treated as positional
        # prompt text (standard CLI semantics; verified: `claude --print -- "..."`
        # treats the text as the prompt).
        argv += ["--", prompt]
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
            # End-of-options separator (C1): guards the command positional so it
            # cannot be parsed as a flag, matching _build_argv.
            "--",
            command,
        ]

    # -- streaming core ---------------------------------------------------------

    async def _stream(
        self, argv: list[str], cwd: str | None, fallback_session_id: str | None
    ) -> AsyncGenerator[StreamEvent, None]:
        """Spawn ``argv`` and yield normalized events, ending with one RunDone.

        ``fallback_session_id`` seeds the terminal RunDone (e.g. the resumed id)
        in case the stream never surfaces one.
        """
        proc = await self._spawn(argv, cwd)
        session_id = fallback_session_id
        exit_code = -1  # defensive: never reference unbound if the loop aborts (I1)

        try:
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
        finally:
            # Kill/reap the child on EVERY exit path — normal completion, timeout,
            # exception, or the consumer closing the generator early
            # (GeneratorExit/cancellation). Idempotent and safe if already exited (C2).
            await proc.kill()

    # -- AgentBackend implementation -------------------------------------------

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
        """Stream normalized events for ``spec``, ending with one ``RunDone``.

        Error contract (I3/I4): on failure this adapter RAISES a typed error —
        :class:`HarnessTimeout`, :class:`HarnessAuthError`, or
        :class:`HarnessError`. It does NOT emit a terminal ``RunDone`` or any
        ``RunError`` event in that case. Catching those typed errors, translating
        them into a ``RunError`` event, and attaching the ``run_id`` is the
        orchestrator's responsibility (Unit 8). ``run_id`` and logging are
        threaded at the orchestrator layer, not here.
        """
        argv = self._build_argv(spec, spec.prompt)
        inner = self._stream(argv, spec.working_dir, spec.resume_session_id)
        # Explicitly close the inner generator on every exit (incl. the consumer
        # abandoning us via aclose/GeneratorExit) so its finally — and thus
        # proc.kill() — runs deterministically rather than only at GC (C2).
        try:
            async for event in inner:
                yield event
        finally:
            await inner.aclose()

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
        inner = self._stream(argv, None, session_id)
        try:
            async for event in inner:
                yield event
        finally:
            await inner.aclose()
