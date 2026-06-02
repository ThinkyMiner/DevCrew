"""Codex CLI harness adapter.

Translates a :class:`RunSpec` into a ``codex exec --json`` invocation, streams
stdout through the pure :mod:`app.harness.codex_parser`, and normalizes
everything to :class:`StreamEvent`s. This is the ONLY place that knows how to
*spawn* Codex; all wire-format knowledge stays in the parser module.

It MIRRORS :mod:`app.harness.claude` exactly — the same injectable ``spawn``, the
same ``Proc`` protocol, concurrent stderr drain, high stdout limit with a typed
over-long-line error, kill/reap in a ``finally`` on every exit path, secret
redaction, typed errors, and the adapter-owned terminal ``RunDone`` carrying the
captured session id.

Real interface (codex-cli 0.130.0, verified locally 2026-06):

* JSON event stream: ``codex exec --json "<prompt>"`` prints JSONL events to
  stdout. ``--skip-git-repo-check`` lets it run outside a git repo.
* Resume: ``codex exec resume <SESSION_ID> [PROMPT]`` where ``SESSION_ID`` is the
  ``thread_id`` surfaced on the ``thread.started`` line.
* Model: ``-m/--model``. Working root: ``-C/--cd`` (+ also passed as the spawn
  ``cwd``). Extra writable dirs: ``--add-dir``.
* Sandbox / permission: ``-s/--sandbox {read-only,workspace-write,
  danger-full-access}``. We map :class:`PermissionMode` -> sandbox mode (see
  ``_SANDBOX_FOR_MODE``: READ_ONLY and ASK both -> ``read-only``, AUTO ->
  ``workspace-write``). ``-a/--ask-for-approval`` exists ONLY on the top-level
  ``codex`` command, not on ``codex exec``; exec configures the approval policy
  via a ``-c`` override. We pin ``-c approval_policy=never`` explicitly so the
  non-interactive posture is self-documenting and robust to a default change
  (verified against codex-cli 0.130.0: the run banner reports
  ``approval: never``). Because exec cannot prompt a human, ASK maps to the
  conservative read-only sandbox rather than silently granting writes (I1).
* Reasoning effort: there is no ``--effort`` flag; Codex takes it via the config
  override ``-c model_reasoning_effort=<low|medium|high|xhigh>``.
* System prompt / instructions: Codex has no inline ``--append-system-prompt``.
  We pass it via the config override ``-c model_instructions=<text>`` (best
  effort — see TODO(OQ-3/instructions)).
* MCP servers: configured in ``~/.codex/config.toml``; there is no per-run
  ``--mcp-config``. We cannot express ad-hoc MCP servers per run (documented
  below).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.domain.errors import HarnessAuthError, HarnessError, HarnessTimeout
from app.domain.events import RunDone, StreamEvent
from app.domain.models import PermissionMode
from app.harness.base import RunSpec
from app.harness.codex_parser import parse_codex_line, session_id_of

_DEFAULT_TIMEOUT = 600.0

# Patterns that flag stderr/auth failures requiring re-login rather than a retry.
_AUTH_PATTERNS = (
    "not logged in",
    "please run codex login",
    "unauthorized",
    "authentication",
    "invalid api key",
    "401",
)

# Redaction: scrub obvious token-/key-like material before it lands in
# errors/logs (AGENTS §4.4 — no secrets in logs). Order matters: more-specific
# patterns first (mirrors the Claude adapter).
_REDACT_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # Anthropic keys (specific)
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),  # other OpenAI-style keys (general)
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),  # GitHub tokens
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),  # long opaque blobs
)

# Raise the StreamReader line limit well above the default 64 KiB so a single
# large JSONL line (big command output / agent message) does not trip
# LimitOverrunError mid-stream. Lines longer than this still convert to a typed
# HarnessError rather than escaping as a raw ValueError.
_STDOUT_LINE_LIMIT = 8 * 1024 * 1024

_REDACTED = "[REDACTED]"

# Map our permission model onto Codex's sandbox mode.
#
# I1 — ASK maps to read-only, NOT workspace-write. `codex exec` runs strictly
# non-interactively (we pin approval_policy=never below): there is no human in
# the loop to approve a write, so granting workspace-write under ASK would
# silently hand out write access nobody approved. Conservative rule: "can't ask
# -> don't allow writes." This is a DELIBERATE cross-backend divergence — the
# Claude CLI passes `--permission-mode ask` through to a CLI that CAN actually
# prompt, so ASK there is meaningfully different; `codex exec` cannot prompt.
_SANDBOX_FOR_MODE = {
    PermissionMode.READ_ONLY: "read-only",
    PermissionMode.ASK: "read-only",
    PermissionMode.AUTO: "workspace-write",
}


def _redact(text: str) -> str:
    """Replace obvious secret-like substrings with ``[REDACTED]``."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


@runtime_checkable
class Proc(Protocol):
    """A spawned process the adapter streams from (mirrors the Claude adapter)."""

    def stdout_lines(self) -> AsyncIterator[str]:
        """Async iterator over decoded stdout lines (newline-stripped)."""
        ...

    async def wait(self) -> int:
        """Await process exit and return the exit code."""
        ...

    async def stderr_text(self) -> str:
        """Return accumulated stderr (decoded), draining concurrently with stdout."""
        ...

    async def kill(self) -> None:
        """Terminate and reap the process; idempotent and safe after exit."""
        ...


Spawn = Callable[[list[str], str | None], Awaitable[Proc]]


class _AsyncioProc:
    """Default :class:`Proc` backed by an ``asyncio`` subprocess.

    stderr is drained into an in-memory buffer by a background task started at
    construction time, so the child can never block writing stderr while we are
    still reading stdout.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stderr_buf = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
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
            raise HarnessError("codex subprocess has no stdout pipe")
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # An over-long line blows past the StreamReader limit; convert to
                # a typed, redacted error instead of leaking a raw ValueError.
                raise HarnessError(
                    f"codex produced an over-long stdout line: {_redact(str(exc))}"
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
        # don't leak a zombie or its pipes. Idempotent.
        if self._process.returncode is None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass  # already gone
            # Drain any data still sitting in the stdout pipe before wait().
            # asyncio's subprocess wait() can deadlock while a PIPE remains full
            # (e.g. after a stdout limit overrun); reading to EOF releases it.
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
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        limit=_STDOUT_LINE_LIMIT,
    )
    return _AsyncioProc(process)


class CodexHarness:
    """:class:`AgentBackend` for the Codex CLI."""

    name = "codex"
    # TODO(OQ-1): codex exec has no verified non-interactive slash-command
    # mechanism for compaction/clear in 0.130.0 (compaction happens
    # automatically; there is no `/compact` over `codex exec`). We therefore
    # advertise NO supported commands conservatively; send_command will raise
    # HarnessError for any command until a live mechanism is confirmed.
    supported_commands: set[str] = set()

    def __init__(
        self,
        spawn: Spawn = _default_spawn,
        codex_bin: str = "codex",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._spawn = spawn
        self._codex_bin = codex_bin
        self._timeout = timeout

    # -- argv construction ------------------------------------------------------

    def _base_flags(self) -> list[str]:
        """Static, spec-independent flags applied to every codex exec invocation.

        Includes the non-interactive security pin so it is present even on argv
        builders that have no :class:`RunSpec` (e.g. ``send_command`` — M2).
        """
        # Pin the approval policy explicitly so the non-interactive security
        # posture is self-documenting and robust to a change in exec's default.
        # `-a/--ask-for-approval` is NOT accepted by `codex exec` (top-level
        # `codex` only); the supported route on exec is the `-c` config override
        # `approval_policy=never` (verified against codex-cli 0.130.0: the run
        # banner reports `approval: never`).
        return ["--json", "--skip-git-repo-check", "-c", "approval_policy=never"]

    def _common_flags(self, spec: RunSpec) -> list[str]:
        """Flags shared by initial-run and resume invocations."""
        flags: list[str] = self._base_flags()
        flags += ["--model", spec.model]
        flags += ["--sandbox", _SANDBOX_FOR_MODE.get(spec.permission_mode, "read-only")]
        if spec.working_dir:
            flags += ["--cd", spec.working_dir]
        if spec.effort:
            # No --effort flag; Codex takes reasoning effort via a config override.
            flags += ["-c", f"model_reasoning_effort={spec.effort}"]
        if spec.system_prompt:
            # No inline --append-system-prompt; pass instructions via -c override.
            # TODO(OQ-3/instructions): verify `model_instructions` is honored by
            # `codex exec` (vs requiring `model_instructions_file`).
            flags += ["-c", f"model_instructions={json.dumps(spec.system_prompt)}"]
        # NOTE: spec.mcp_servers cannot be expressed per-run on `codex exec`
        # (MCP servers live in ~/.codex/config.toml); they are intentionally not
        # mapped here. Documented limitation.
        return flags

    def _build_argv(self, spec: RunSpec, prompt: str) -> list[str]:
        if spec.resume_session_id:
            # `codex exec resume <SESSION_ID> -- [PROMPT]` — flags go before the
            # subcommand args. The literal "--" (C1) marks end-of-options so a
            # prompt beginning with "-" (e.g. "--version", "-c sandbox=...") is
            # treated as prompt text, never parsed as a flag/config override.
            # Verified against codex-cli 0.130.0: `codex exec ... -- "--version"`
            # runs the agent with that text as the prompt instead of printing
            # the version.
            return [
                self._codex_bin,
                "exec",
                *self._common_flags(spec),
                "resume",
                spec.resume_session_id,
                "--",
                prompt,
            ]
        return [self._codex_bin, "exec", *self._common_flags(spec), "--", prompt]

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
        exit_code = -1  # defensive: never reference unbound if the loop aborts

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
                        for event in parse_codex_line(obj):
                            yield event
                    exit_code = await proc.wait()
            except TimeoutError as exc:
                raise HarnessTimeout(f"codex run exceeded {self._timeout}s timeout") from exc

            if exit_code != 0:
                stderr = _redact(await proc.stderr_text())
                lowered = stderr.lower()
                if any(pat in lowered for pat in _AUTH_PATTERNS):
                    raise HarnessAuthError(f"codex not authenticated (exit {exit_code}): {stderr}")
                raise HarnessError(f"codex exited {exit_code}: {stderr}")

            yield RunDone(session_id=session_id)
        finally:
            # Kill/reap the child on EVERY exit path — normal completion, timeout,
            # exception, or the consumer closing the generator early. Idempotent.
            await proc.kill()

    # -- AgentBackend implementation -------------------------------------------

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
        """Stream normalized events for ``spec``, ending with one ``RunDone``.

        Error contract: on failure this adapter RAISES a typed error —
        :class:`HarnessTimeout`, :class:`HarnessAuthError`, or
        :class:`HarnessError`. It does NOT emit a terminal ``RunDone`` or any
        ``RunError`` event in that case; translating those typed errors into a
        ``RunError`` event is the orchestrator's responsibility (Unit 8).
        """
        argv = self._build_argv(spec, spec.prompt)
        inner = self._stream(argv, spec.working_dir, spec.resume_session_id)
        # Explicitly close the inner generator on every exit so its finally — and
        # thus proc.kill() — runs deterministically rather than only at GC.
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
        # Unreachable today (supported_commands is empty — TODO(OQ-1)); kept for
        # symmetry with the Claude adapter's contract. If/when Codex exposes a
        # non-interactive compaction command this activates.
        #
        # M2: build from the same _base_flags() helper as run() (no RunSpec is
        # available here, so spec-derived --sandbox/--model/--cd can't be set —
        # but the security pin and stream flags stay aligned) and reuse the same
        # resume + end-of-options "--" guard so the command positional can never
        # be parsed as a flag. TODO(OQ-1): this path goes live only once a
        # verified non-interactive command mechanism exists; revisit threading a
        # RunSpec through so --sandbox/--model/--cd match run() exactly.
        argv = [
            self._codex_bin,
            "exec",
            *self._base_flags(),
            "resume",
            session_id,
            "--",
            command,
        ]
        inner = self._stream(argv, None, session_id)
        try:
            async for event in inner:
                yield event
        finally:
            await inner.aclose()
