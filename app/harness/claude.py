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

import json
from collections.abc import AsyncGenerator, AsyncIterator

from app.domain.errors import HarnessError
from app.domain.events import StreamEvent
from app.domain.models import PermissionMode
from app.harness.base import RunSpec
from app.harness.claude_parser import parse_claude_line, session_id_of
from app.harness.process import (
    DEFAULT_TIMEOUT,
    AsyncioProc,
    Spawn,
    make_default_spawn,
    redact,
    run_process,
)
from app.harness.process import (
    Proc as Proc,  # re-exported: the harness tests import Proc from this module
)

# Back-compat re-exports: the existing harness tests import these names from the
# adapter module. The implementations now live in app.harness.process.
_AsyncioProc = AsyncioProc
_redact = redact
_default_spawn: Spawn = make_default_spawn(provider="claude")

# Persona environment isolation flags (verified live against claude-cli 2.1.159).
# Applied to EVERY invocation (initial run + resume) so a persona behaves ONLY per
# its configured --append-system-prompt, never as the operator's "Team coding
# agent":
# Persona environment isolation is achieved via a NEUTRAL spawn cwd OUTSIDE the
# project tree (see ClaudeHarness._spawn_cwd + Settings.resolved_scratch_dir): a
# persona with no bound working_dir runs in a clean temp dir, so its claude child
# does not walk up and load this repo's CLAUDE.md/AGENTS.md (which was making
# personas behave like a "Team coding agent").
#
# We deliberately do NOT pass isolation FLAGS — both options that looked apt were
# verified live to break auth:
#   --bare                 → "Not logged in · Please run /login" (minimal mode
#                            skips the credential bootstrap).
#   --setting-sources ""   → same; it drops the user settings that carry the
#                            OAuth/subscription credentials.
# KNOWN LIMITATION (TODO/OQ): the operator's USER-GLOBAL Claude Code plugins/
# hooks (e.g. a SessionStart hook) still load for persona children and can add
# minor behavioral flavor. There is no flag that disables them without also
# dropping auth; a future fix is to point personas at a clean CLAUDE_CONFIG_DIR
# that has the credentials copied in but no plugins/hooks.
_ISOLATION_FLAGS: list[str] = []

# The domain PermissionMode is provider-agnostic (read-only/ask/auto); the Claude
# CLI's `--permission-mode` accepts a DIFFERENT vocabulary (verified against
# claude-cli 2.1.159: acceptEdits, auto, bypassPermissions, default, dontAsk,
# plan). Map our intent onto Claude's real choices, mirroring how CodexHarness
# maps PermissionMode onto codex's --sandbox values:
#   READ_ONLY -> "plan"        (Claude's only mode that cannot edit/execute; it
#                               reads/analyzes and answers without making changes)
#   ASK       -> "default"     (prompts before acting on tools)
#   AUTO      -> "acceptEdits"  (proceeds without prompting for edits)
_PERMISSION_MODE_FOR_CLAUDE: dict[PermissionMode, str] = {
    PermissionMode.READ_ONLY: "plan",
    PermissionMode.ASK: "default",
    PermissionMode.AUTO: "acceptEdits",
}

# Patterns that flag stderr/auth failures requiring re-login rather than a retry.
_AUTH_PATTERNS = (
    "not authenticated",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "401",
)

class ClaudeHarness:
    """:class:`AgentBackend` for the Claude Code CLI."""

    name = "claude"
    supported_commands: set[str] = {"/compact", "/clear"}

    def __init__(
        self,
        spawn: Spawn = _default_spawn,
        claude_bin: str = "claude",
        timeout: float = DEFAULT_TIMEOUT,
        scratch_dir: str | None = None,
    ) -> None:
        self._spawn = spawn
        self._claude_bin = claude_bin
        self._timeout = timeout
        # Persona environment isolation: a NEUTRAL, empty directory (no
        # CLAUDE.md/AGENTS.md) used as the spawn cwd when a persona has no bound
        # working_dir. Without it, the claude child inherits the server's cwd
        # (this repo) and loads our project docs, behaving like a "Team coding
        # agent" instead of its configured advisor persona. A persona WITH a
        # working_dir still runs there (the user wants that repo's context).
        # When None, current behavior is preserved (back-compat for tests); the
        # composition root passes a real scratch dir.
        self._scratch_dir = scratch_dir

    def _spawn_cwd(self, spec: RunSpec) -> str | None:
        """Resolve the spawn cwd: the bound repo if set, else the neutral scratch.

        Persona isolation (verified live): with no working_dir we must NOT run in
        the server's project cwd (it would load this repo's CLAUDE.md/AGENTS.md).
        """
        if spec.working_dir:
            return spec.working_dir
        return self._scratch_dir

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

    @staticmethod
    def _allowed_tools_args(allowed_tools: list[str]) -> list[str]:
        """Build ``--allowedTools`` from a persona's ``allowed_tools``.

        Headless ``claude --print`` cannot prompt the operator for a tool
        permission (there is no terminal to answer on the webpage), so any tool
        that would otherwise require approval — WebSearch, WebFetch, Bash, … — is
        blocked unless pre-approved here. ``--allowedTools`` grants exactly the
        tools the operator chose in the persona editor, no prompt required.

        Emitted as ONE comma-separated value (the CLI accepts "comma or
        space-separated"): a scoped entry like ``Bash(git *)`` contains a space,
        so passing the list as separate argv items would split it — and a
        variadic ``<tools...>`` flag could also greedily swallow the flags that
        follow. A single token sidesteps both. Empty list -> no flag.
        """
        cleaned = [t.strip() for t in allowed_tools if t and t.strip()]
        if not cleaned:
            return []
        return ["--allowedTools", ",".join(cleaned)]

    @staticmethod
    def _normalize_model(model: str) -> str:
        """Collapse a human label like ``"opus 4.8"`` to the CLI alias ``"opus"``.

        The Claude CLI's ``--model`` accepts aliases (``opus``/``sonnet``/``haiku``)
        or full ids (``claude-opus-4-8``) — NOT a spaced label like ``"opus 4.8"``,
        which it rejects ("model … may not exist"). This is the last line of
        defence: whatever a persona's stored model is (UI, API, or legacy seed), a
        ``"<alias> <version…>"`` string is reduced to the bare alias here so the
        CLI never chokes. Anything else (a bare alias, a full id) passes through.
        """
        stripped = model.strip()
        head = stripped.split()[0].lower() if stripped.split() else stripped
        if " " in stripped and head in {"opus", "sonnet", "haiku"}:
            return head
        return stripped

    def _build_argv(self, spec: RunSpec, prompt: str) -> list[str]:
        argv = [
            self._claude_bin,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            # Persona isolation (verified live bug fix) — see _ISOLATION_FLAGS.
            *_ISOLATION_FLAGS,
            "--model",
            self._normalize_model(spec.model),
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
        argv += ["--permission-mode", _PERMISSION_MODE_FOR_CLAUDE[spec.permission_mode]]
        # Pre-approve the persona's chosen tools (WebSearch, Bash, …): headless
        # --print mode cannot prompt the operator, so without this they are blocked.
        argv += self._allowed_tools_args(spec.allowed_tools)
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
            # Persona isolation on resume too (so a /compact or /clear over an
            # existing session stays uncontaminated) — see _ISOLATION_FLAGS.
            *_ISOLATION_FLAGS,
            "--resume",
            session_id,
            # End-of-options separator (C1): guards the command positional so it
            # cannot be parsed as a flag, matching _build_argv.
            "--",
            command,
        ]

    # -- streaming core ---------------------------------------------------------

    def _stream(
        self, argv: list[str], cwd: str | None, fallback_session_id: str | None
    ) -> AsyncGenerator[StreamEvent, None]:
        """Drive the shared :func:`run_process` runner with Claude's callbacks.

        All the subprocess hardening (concurrent stderr drain, kill/reap, the
        over-long-line guard, timeout, exit-code→error mapping) lives in
        :mod:`app.harness.process`; this adapter only supplies the Claude argv,
        parser, session-id extractor, and auth patterns. ``fallback_session_id``
        seeds the terminal RunDone (e.g. the resumed id).
        """
        return run_process(
            argv,
            cwd,
            spawn=self._spawn,
            provider="claude",
            parse_line=parse_claude_line,
            session_id_of=session_id_of,
            auth_patterns=_AUTH_PATTERNS,
            fallback_session_id=fallback_session_id,
            timeout=self._timeout,
        )

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
        inner = self._stream(argv, self._spawn_cwd(spec), spec.resume_session_id)
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
        # No RunSpec here (no working_dir), so use the neutral scratch dir as cwd
        # rather than the server's project cwd — same persona-isolation rationale
        # as run() (avoids loading this repo's CLAUDE.md/AGENTS.md).
        inner = self._stream(argv, self._scratch_dir, session_id)
        try:
            async for event in inner:
                yield event
        finally:
            await inner.aclose()
