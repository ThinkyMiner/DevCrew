from __future__ import annotations

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

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def wait(self) -> int:
        return self._returncode

    async def stderr_text(self) -> str:
        return self._stderr


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
        import asyncio

        await asyncio.sleep(3600)
        yield ""  # pragma: no cover

    async def wait(self) -> int:  # pragma: no cover
        return 0

    async def stderr_text(self) -> str:  # pragma: no cover
        return ""


def _spec(prompt: str = "hello", **kw: object) -> RunSpec:
    return RunSpec(prompt=prompt, provider=Provider.CLAUDE, model="claude-haiku-4-5", **kw)  # type: ignore[arg-type]


async def _collect(agen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in agen]


# -- argv ----------------------------------------------------------------------


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
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--append-system-prompt") + 1] == "you are helpful"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--add-dir") + 1] == "/work/dir"
    assert "--mcp-config" in argv
    # prompt is the final positional arg
    assert argv[-1] == "do the thing"
    # working_dir is also passed as cwd
    assert spawn.cwd == "/work/dir"


async def test_run_resume_adds_resume_flag() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = ClaudeHarness(spawn=spawn)
    await _collect(h.run(_spec(resume_session_id="prev-sess")))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--resume") + 1] == "prev-sess"


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
    # permission-mode always present (defaults to read-only)
    assert argv[argv.index("--permission-mode") + 1] == "read-only"
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
    assert argv[-1] == "/compact"
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
