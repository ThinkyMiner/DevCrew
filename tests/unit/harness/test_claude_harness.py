from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.domain.errors import HarnessAuthError, HarnessError, HarnessTimeout
from app.domain.events import (
    RunDone,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    Usage,
)
from app.domain.models import PermissionMode, Provider
from app.harness.base import AgentBackend, RunSpec
from app.harness.claude import ClaudeHarness, Proc, _redact

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "claude_stream_basic.jsonl"


def _fixture_lines() -> list[str]:
    return [ln for ln in _FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


class FakeProc:
    """In-memory :class:`Proc` that replays canned stdout lines."""

    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self._lines = lines
        self._returncode = returncode
        self._stderr = stderr
        self.kill_calls = 0

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def wait(self) -> int:
        return self._returncode

    async def stderr_text(self) -> str:
        return self._stderr

    async def kill(self) -> None:
        self.kill_calls += 1


class FakeSpawn:
    """Records the argv/cwd it was called with and returns a fixed FakeProc."""

    def __init__(self, proc: FakeProc) -> None:
        self._proc = proc
        self.argv: list[str] | None = None
        self.cwd: str | None = None
        self.calls = 0

    async def __call__(self, argv: list[str], cwd: str | None) -> Proc:
        self.argv = argv
        self.cwd = cwd
        self.calls += 1
        return self._proc


class TimeoutProc:
    """A Proc whose stdout iterator never yields and blocks forever."""

    async def stdout_lines(self) -> AsyncIterator[str]:
        await asyncio.sleep(3600)
        yield ""  # pragma: no cover

    async def wait(self) -> int:  # pragma: no cover
        return 0

    async def stderr_text(self) -> str:  # pragma: no cover
        return ""

    async def kill(self) -> None:
        return None


def _spec(prompt: str = "hello", **kw: object) -> RunSpec:
    model = kw.pop("model", "claude-haiku-4-5")
    return RunSpec(prompt=prompt, provider=Provider.CLAUDE, model=model, **kw)  # type: ignore[arg-type]


async def _collect(agen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in agen]


# -- argv ----------------------------------------------------------------------


async def test_model_label_with_version_is_normalized_to_alias() -> None:
    # Defensive: a human label like "opus 4.8" is NOT a valid --model value; the
    # adapter collapses a "<alias> <version…>" string to the bare alias so the CLI
    # never sees it (covers legacy/seeded data regardless of how it was created).
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec(model="opus 4.8")))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--model") + 1] == "opus"


async def test_valid_model_strings_pass_through_unchanged() -> None:
    for valid in ["opus", "sonnet", "haiku", "claude-opus-4-8"]:
        spawn = FakeSpawn(FakeProc(_fixture_lines()))
        h = ClaudeHarness(spawn=spawn)
        await _collect(h.run(_spec(model=valid)))
        argv = spawn.argv
        assert argv is not None
        assert argv[argv.index("--model") + 1] == valid, valid


async def test_run_builds_expected_argv() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    spec = _spec(
        "do the thing",
        system_prompt="you are helpful",
        effort="high",
        permission_mode=PermissionMode.AUTO,
        working_dir="/work/dir",
        mcp_servers=["filesystem"],
    )
    await _collect(h.run(spec))

    argv = spawn.argv
    assert argv is not None
    assert argv[0] == "claude"
    assert "--print" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # Persona isolation is via a neutral spawn cwd, NOT --bare: --bare breaks
    # auth ("Not logged in"), so it must never be passed.
    assert "--bare" not in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--append-system-prompt") + 1] == "you are helpful"
    assert argv[argv.index("--effort") + 1] == "high"
    # AUTO maps to Claude's "acceptEdits" (domain PermissionMode -> claude vocab)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--add-dir") + 1] == "/work/dir"
    assert "--mcp-config" in argv
    # prompt is the final positional arg
    assert argv[-1] == "do the thing"
    # working_dir is also passed as cwd
    assert spawn.cwd == "/work/dir"


async def test_allowed_tools_passed_as_comma_separated_allowedtools() -> None:
    # The bug: a persona's allowed_tools (e.g. WebSearch) never reached the CLI, so
    # headless `claude --print` (which cannot prompt the operator) blocked the tool.
    # Fix: emit `--allowedTools` so the listed tools are pre-approved without a
    # prompt. Comma-separated single value so a scoped entry like "Bash(git *)"
    # (which contains a space) is not split into separate args.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec(allowed_tools=["WebSearch", "Bash(git *)"])))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,Bash(git *)"


async def test_no_allowed_tools_omits_allowedtools_flag() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    assert spawn.argv is not None
    assert "--allowedTools" not in spawn.argv


async def test_run_resume_adds_resume_flag() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec(resume_session_id="prev-sess")))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--resume") + 1] == "prev-sess"


async def test_run_prompt_preceded_by_end_of_options_separator() -> None:
    # C1: a prompt that looks like a flag must NOT be parsed as one. The prompt
    # must be the last element, immediately preceded by a literal "--".
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec("--version")))
    argv = spawn.argv
    assert argv is not None
    assert argv[-1] == "--version"
    assert argv[-2] == "--"
    sep = argv.index("--")
    assert "--version" not in argv[:sep]


async def test_run_resume_prompt_preceded_by_end_of_options_separator() -> None:
    # C1 (resume path): the prompt positional is guarded on resume runs too.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec("--version", resume_session_id="prev-sess")))
    argv = spawn.argv
    assert argv is not None
    assert argv[-1] == "--version"
    assert argv[-2] == "--"


async def test_run_no_optional_flags_when_unset() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    argv = spawn.argv
    assert argv is not None
    assert "--resume" not in argv
    assert "--append-system-prompt" not in argv
    assert "--effort" not in argv
    assert "--mcp-config" not in argv
    assert "--add-dir" not in argv
    assert "--allowedTools" not in argv
    # permission-mode always present; default READ_ONLY maps to Claude's "plan"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert spawn.cwd is None


async def test_permission_modes_map_to_valid_claude_choices() -> None:
    """Every domain PermissionMode must map to a value the claude CLI accepts.

    Regression for a bug found by a live smoke: the CLI's --permission-mode
    rejects our domain values (read-only/ask/auto); allowed choices are
    acceptEdits, auto, bypassPermissions, default, dontAsk, plan.
    """
    valid = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
    expected = {
        PermissionMode.READ_ONLY: "plan",
        PermissionMode.ASK: "default",
        PermissionMode.AUTO: "acceptEdits",
    }
    for mode, want in expected.items():
        spawn = FakeSpawn(FakeProc(_fixture_lines()))
        h = ClaudeHarness(spawn=spawn)
        await _collect(h.run(_spec(permission_mode=mode)))
        assert spawn.argv is not None
        got = spawn.argv[spawn.argv.index("--permission-mode") + 1]
        assert got == want and got in valid, f"{mode} -> {got}"


async def test_bare_flag_never_passed_on_any_path() -> None:
    # --bare breaks auth ("Not logged in"); it must NOT appear on run, resume,
    # or send_command paths. Isolation is via the neutral spawn cwd instead.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec(resume_session_id="prev-sess")))
    assert spawn.argv is not None and "--bare" not in spawn.argv

    spawn2 = FakeSpawn(FakeProc(_fixture_lines()))
    h2 = ClaudeHarness(spawn=spawn2)
    await _collect(h2.send_command("sess-1", "/compact"))
    assert spawn2.argv is not None and "--bare" not in spawn2.argv


# -- persona environment isolation (scratch cwd) -------------------------------


async def test_scratch_cwd_used_when_no_working_dir() -> None:
    # With a scratch_dir configured and no spec.working_dir, the persona's child
    # must spawn in the neutral scratch dir (NOT the server's project cwd).
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn, scratch_dir="/neutral/scratch")
    await _collect(h.run(_spec()))
    assert spawn.cwd == "/neutral/scratch"
    # Scratch must NOT be added as a writable --add-dir.
    assert spawn.argv is not None
    assert "/neutral/scratch" not in spawn.argv


async def test_working_dir_overrides_scratch_cwd() -> None:
    # A persona WITH a bound repo runs there (intentional repo context); scratch
    # is ignored and the repo is still passed as --add-dir.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn, scratch_dir="/neutral/scratch")
    await _collect(h.run(_spec(working_dir="/work/dir")))
    assert spawn.cwd == "/work/dir"
    assert spawn.argv is not None
    assert spawn.argv[spawn.argv.index("--add-dir") + 1] == "/work/dir"


async def test_send_command_uses_scratch_cwd() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn, scratch_dir="/neutral/scratch")
    await _collect(h.send_command("sess-1", "/compact"))
    assert spawn.cwd == "/neutral/scratch"


async def test_no_scratch_dir_preserves_legacy_cwd_behavior() -> None:
    # Back-compat: with no scratch_dir, an unbound persona still spawns with
    # cwd=None (pre-isolation behavior) so existing tests/usage are unaffected.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    assert spawn.cwd is None


# -- streaming -----------------------------------------------------------------


async def test_run_yields_normalized_events_ending_in_run_done() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    events = await _collect(h.run(_spec()))

    kinds = [type(e) for e in events]
    assert kinds == [ThinkingDelta, ToolUse, ToolResult, TextDelta, Usage, RunDone]
    done = events[-1]
    assert isinstance(done, RunDone)
    assert done.session_id == "sess-abc123"


async def test_structural_async_generator_and_protocol() -> None:
    # Drives `async for` to a real list (proves run() is a working async gen),
    # and asserts the instance satisfies AgentBackend (runtime_checkable only
    # checks attribute presence, hence the explicit async-gen drive above).
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    result: list[StreamEvent] = []
    async for event in h.run(_spec()):
        result.append(event)
    assert result
    assert isinstance(result[-1], RunDone)
    assert isinstance(h, AgentBackend)


# -- failures ------------------------------------------------------------------


async def test_nonzero_exit_raises_harness_error() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines(), returncode=1, stderr="boom"))
    h = ClaudeHarness(spawn=spawn)
    with pytest.raises(HarnessError) as ei:
        await _collect(h.run(_spec()))
    assert "boom" in str(ei.value)


async def test_auth_failure_raises_auth_error() -> None:
    spawn = FakeSpawn(
        FakeProc(
            _fixture_lines(),
            returncode=1,
            stderr="Error: Not authenticated. Please run /login",
        )
    )
    h = ClaudeHarness(spawn=spawn)
    with pytest.raises(HarnessAuthError):
        await _collect(h.run(_spec()))


async def test_nonzero_exit_redacts_secrets_in_error() -> None:
    secret = "sk-ant-abcdef0123456789ABCDEF"
    spawn = FakeSpawn(FakeProc(_fixture_lines(), returncode=2, stderr=f"failed with key {secret}"))
    h = ClaudeHarness(spawn=spawn)
    with pytest.raises(HarnessError) as ei:
        await _collect(h.run(_spec()))
    assert secret not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


async def test_timeout_raises_harness_timeout() -> None:
    async def spawn(argv: list[str], cwd: str | None) -> Proc:
        return TimeoutProc()

    h = ClaudeHarness(spawn=spawn, timeout=0.05)
    with pytest.raises(HarnessTimeout):
        await _collect(h.run(_spec()))


# -- send_command --------------------------------------------------------------


async def test_send_command_unsupported_raises() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    with pytest.raises(HarnessError):
        await _collect(h.send_command("sess-abc123", "/bogus"))


async def test_send_command_supported_builds_resume_argv_and_streams() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    events = await _collect(h.send_command("sess-abc123", "/compact"))

    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--resume") + 1] == "sess-abc123"
    # C1: the slash command (a positional) is guarded by the separator too.
    assert argv[-1] == "/compact"
    assert argv[-2] == "--"
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "sess-abc123"


# -- helpers / capabilities ----------------------------------------------------


def test_capabilities() -> None:
    h = ClaudeHarness(spawn=FakeSpawn(FakeProc([])))
    assert h.name == "claude"
    assert h.supported_commands == {"/compact", "/clear"}


def test_redact_scrubs_tokens() -> None:
    assert "sk-ant-abcdefghijklmnop" not in _redact("key=sk-ant-abcdefghijklmnop done")
    assert "[REDACTED]" in _redact("Bearer abc.def.ghi")
    assert _redact("nothing secret here") == "nothing secret here"


def test_fixture_is_valid_jsonl() -> None:
    for line in _fixture_lines():
        assert isinstance(json.loads(line), dict)


# -- C2: child killed/reaped on every exit path (fake-level) -------------------


async def test_proc_killed_on_normal_completion() -> None:
    proc = FakeProc(_fixture_lines())
    spawn = FakeSpawn(proc)
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    assert proc.kill_calls == 1


async def test_proc_killed_on_error_exit() -> None:
    proc = FakeProc(_fixture_lines(), returncode=1, stderr="boom")
    spawn = FakeSpawn(proc)
    h = ClaudeHarness(spawn=spawn)
    with pytest.raises(HarnessError):
        await _collect(h.run(_spec()))
    assert proc.kill_calls == 1


async def test_proc_killed_on_early_generator_close() -> None:
    proc = FakeProc(_fixture_lines())
    spawn = FakeSpawn(proc)
    h = ClaudeHarness(spawn=spawn)
    agen = h.run(_spec())
    await agen.__anext__()  # consume one event, then abandon the generator
    await agen.aclose()
    assert proc.kill_calls == 1
