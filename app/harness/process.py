"""Shared subprocess runner for CLI harness adapters.

This is the one deep module that owns the subprocess machinery every CLI harness
adapter needs *identically*: spawning, bounded stdout line reading, a concurrent
stderr drain, secret redaction, kill/reap on every exit path, the run timeout,
the exit-code→typed-error mapping, and the adapter-owned terminal ``RunDone``
carrying the captured session id.

Everything provider-specific stays OUT of here and is injected at the call site:

* ``argv`` / ``cwd`` — the concrete CLI invocation (built by the adapter).
* ``parse_line`` — the pure wire-format parser (``parse_claude_line`` / ``parse_codex_line``).
* ``session_id_of`` — how this provider surfaces its session id.
* ``auth_patterns`` — stderr substrings that mean "re-login", not "retry".
* ``provider`` — used only in error text.

Two adapters (Claude, Codex) drive this one runner — a real seam, not
indirection. The hardening that used to be copy-pasted into both adapters
(C1 concurrent drain, C2 kill/reap, I2 over-long-line guard) now lives here once,
so a fix lands in one place. See ADR/DECISIONS D7–D9 for the hardening rationale.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.domain.errors import HarnessAuthError, HarnessError, HarnessTimeout
from app.domain.events import RunDone, StreamEvent

DEFAULT_TIMEOUT = 600.0

# Raise the StreamReader line limit well above the default 64 KiB so a single
# large stream-json line (big tool_result / assistant message) does not trip
# LimitOverrunError mid-stream (I2). Lines longer than this still convert to a
# typed HarnessError rather than escaping as a raw ValueError.
_STDOUT_LINE_LIMIT = 8 * 1024 * 1024

_REDACTED = "[REDACTED]"

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

# Provider-specific callbacks injected into run_process.
ParseLine = Callable[[dict[str, object]], list[StreamEvent]]
SessionIdOf = Callable[[dict[str, object]], str | None]


def redact(text: str) -> str:
    """Replace obvious secret-like substrings with ``[REDACTED]``."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


@runtime_checkable
class Proc(Protocol):
    """A spawned process the runner streams from.

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


class AsyncioProc:
    """Default :class:`Proc` backed by an ``asyncio`` subprocess.

    stderr is drained into an in-memory buffer by a background task started at
    construction time, so the child can never block writing stderr while we are
    still reading stdout (C1). ``provider`` only flavours error text.
    """

    def __init__(self, process: asyncio.subprocess.Process, provider: str = "process") -> None:
        self._process = process
        self._provider = provider
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
            raise HarnessError(f"{self._provider} subprocess has no stdout pipe")
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # An over-long line blows past the StreamReader limit; convert to
                # a typed, redacted error instead of leaking a raw ValueError (I2).
                raise HarnessError(
                    f"{self._provider} produced an over-long stdout line: {redact(str(exc))}"
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


def make_default_spawn(*, provider: str, stdin_devnull: bool = False) -> Spawn:
    """Build the production :class:`Spawn` for a provider.

    ``stdin_devnull`` attaches ``/dev/null`` to the child's stdin (codex wants
    this so it never blocks waiting on a tty); claude leaves stdin inherited.
    """

    async def _spawn(argv: list[str], cwd: str | None) -> Proc:
        if stdin_devnull:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                limit=_STDOUT_LINE_LIMIT,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=_STDOUT_LINE_LIMIT,
            )
        return AsyncioProc(process, provider)

    return _spawn


async def run_process(
    argv: list[str],
    cwd: str | None,
    *,
    spawn: Spawn,
    provider: str,
    parse_line: ParseLine,
    session_id_of: SessionIdOf,
    auth_patterns: tuple[str, ...],
    fallback_session_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 — threaded straight into asyncio.timeout() below
) -> AsyncGenerator[StreamEvent, None]:
    """Spawn ``argv`` and yield normalized events, ending with one ``RunDone``.

    ``fallback_session_id`` seeds the terminal ``RunDone`` (e.g. the resumed id)
    in case the stream never surfaces one.

    Error contract (I3/I4): on failure this RAISES a typed error —
    :class:`HarnessTimeout`, :class:`HarnessAuthError`, or :class:`HarnessError`.
    It does NOT emit a terminal ``RunDone`` or any ``RunError`` in that case;
    translating typed errors into a ``RunError`` (and attaching ``run_id``) is the
    orchestrator's responsibility.
    """
    proc = await spawn(argv, cwd)
    session_id = fallback_session_id
    exit_code = -1  # defensive: never reference unbound if the loop aborts (I1)

    try:
        try:
            async with asyncio.timeout(timeout):
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
                    for event in parse_line(obj):
                        yield event
                exit_code = await proc.wait()
        except TimeoutError as exc:
            raise HarnessTimeout(f"{provider} run exceeded {timeout}s timeout") from exc

        if exit_code != 0:
            stderr = redact(await proc.stderr_text())
            lowered = stderr.lower()
            if any(pat in lowered for pat in auth_patterns):
                raise HarnessAuthError(f"{provider} not authenticated (exit {exit_code}): {stderr}")
            raise HarnessError(f"{provider} exited {exit_code}: {stderr}")

        yield RunDone(session_id=session_id)
    finally:
        # Kill/reap the child on EVERY exit path — normal completion, timeout,
        # exception, or the consumer closing the generator early
        # (GeneratorExit/cancellation). Idempotent and safe if already exited (C2).
        await proc.kill()
