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

import json
from collections.abc import AsyncGenerator, AsyncIterator

from app.domain.errors import HarnessError
from app.domain.events import StreamEvent
from app.domain.models import PermissionMode
from app.harness.base import RunSpec
from app.harness.codex_parser import parse_codex_line, session_id_of
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
# codex attaches /dev/null to stdin so the child never blocks on a tty.
_default_spawn: Spawn = make_default_spawn(provider="codex", stdin_devnull=True)

# Patterns that flag stderr/auth failures requiring re-login rather than a retry.
_AUTH_PATTERNS = (
    "not logged in",
    "please run codex login",
    "unauthorized",
    "authentication",
    "invalid api key",
    "401",
)

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


class CodexHarness:
    """:class:`AgentBackend` for the Codex CLI."""

    name = "codex"
    # TODO(OQ-1): codex exec has no verified non-interactive slash-command
    # mechanism for compaction/clear in 0.130.0 (compaction happens
    # automatically; there is no `/compact` over `codex exec`). We therefore
    # advertise NO supported commands conservatively; send_command will raise
    # HarnessError for any command until a live mechanism is confirmed.
    supported_commands: set[str] = set()
    # Models the codex CLI's ``-m/--model`` accepts (suggestions, not an
    # allowlist — a persona may type any id the installed codex build supports).
    supported_models: tuple[str, ...] = ("gpt-5.5-codex", "gpt-5.5")

    def __init__(
        self,
        spawn: Spawn = _default_spawn,
        codex_bin: str = "codex",
        timeout: float = DEFAULT_TIMEOUT,
        scratch_dir: str | None = None,
    ) -> None:
        self._spawn = spawn
        self._codex_bin = codex_bin
        self._timeout = timeout
        # Persona environment isolation: a NEUTRAL, empty directory (no
        # AGENTS.md) used as the working root / spawn cwd when a persona has no
        # bound working_dir. Without it the codex child inherits the server's cwd
        # (this repo) and loads the cwd AGENTS.md, contaminating the persona.
        # A persona WITH a working_dir still runs there (user wants that context).
        # When None, current behavior is preserved (back-compat for tests).
        #
        # TODO(OQ/codex-global-agents): neutral cwd stops *cwd* AGENTS.md from
        # leaking, but `codex exec` ALSO reads the GLOBAL ~/.codex/AGENTS.md and
        # the operator's ~/.codex/config.toml (skills/plugins/hooks). There is no
        # clean exec flag that drops the global AGENTS.md while keeping ChatGPT
        # auth working: `--ignore-user-config` skips config.toml but its docs say
        # it does NOT cover AGENTS.md, and it also resets the model default
        # (breaking the run). So global ~/.codex/AGENTS.md contamination of codex
        # personas is a known residual limitation surfaced to the operator.
        self._scratch_dir = scratch_dir

    def _spawn_cwd(self, spec: RunSpec) -> str | None:
        """Resolve the spawn cwd: the bound repo if set, else the neutral scratch."""
        if spec.working_dir:
            return spec.working_dir
        return self._scratch_dir

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
        # Persona isolation: --cd the bound repo if set, else the neutral scratch
        # dir (NOT the server's project cwd, which would load that repo's
        # AGENTS.md). See __init__ for the residual global-AGENTS.md limitation.
        cwd = self._spawn_cwd(spec)
        if cwd:
            flags += ["--cd", cwd]
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

    @staticmethod
    def _wants_web_search(spec: RunSpec) -> bool:
        """True if the persona's allowed_tools request web search.

        Accepts the common spellings the allowed-tools picker may produce
        (``web_search``, ``WebSearch``, ``web search``, ``web-search``) by
        normalising to alphanumerics. This is the one allowed_tools entry the
        codex adapter acts on — it maps to codex's native web_search tool.
        """
        return any(
            "".join(ch for ch in tool.lower() if ch.isalnum()) == "websearch"
            for tool in spec.allowed_tools
        )

    def _global_flags(self, spec: RunSpec) -> list[str]:
        """Top-level codex flags that must precede the ``exec`` subcommand.

        ``--search`` enables codex's native web_search tool and is a GLOBAL flag:
        ``codex --search exec …`` parses, ``codex exec --search`` does not
        (verified against codex-cli 0.130.0).
        """
        return ["--search"] if self._wants_web_search(spec) else []

    def _build_argv(self, spec: RunSpec, prompt: str) -> list[str]:
        glob = self._global_flags(spec)
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
                *glob,
                "exec",
                *self._common_flags(spec),
                "resume",
                spec.resume_session_id,
                "--",
                prompt,
            ]
        return [self._codex_bin, *glob, "exec", *self._common_flags(spec), "--", prompt]

    # -- streaming core ---------------------------------------------------------

    def _stream(
        self, argv: list[str], cwd: str | None, fallback_session_id: str | None
    ) -> AsyncGenerator[StreamEvent, None]:
        """Drive the shared :func:`run_process` runner with Codex's callbacks.

        All the subprocess hardening (concurrent stderr drain, kill/reap, the
        over-long-line guard, timeout, exit-code→error mapping) lives in
        :mod:`app.harness.process`; this adapter only supplies the Codex argv,
        parser, session-id extractor, and auth patterns. ``fallback_session_id``
        seeds the terminal RunDone (e.g. the resumed id).
        """
        return run_process(
            argv,
            cwd,
            spawn=self._spawn,
            provider="codex",
            parse_line=parse_codex_line,
            session_id_of=session_id_of,
            auth_patterns=_AUTH_PATTERNS,
            fallback_session_id=fallback_session_id,
            timeout=self._timeout,
        )

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
        inner = self._stream(argv, self._spawn_cwd(spec), spec.resume_session_id)
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
        # Neutral scratch cwd (no RunSpec/working_dir here) — persona isolation,
        # same rationale as run().
        inner = self._stream(argv, self._scratch_dir, session_id)
        try:
            async for event in inner:
                yield event
        finally:
            await inner.aclose()
