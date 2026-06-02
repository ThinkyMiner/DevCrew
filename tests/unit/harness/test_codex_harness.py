from __future__ import annotations

import asyncio
import json
import sys
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
from app.harness.codex import CodexHarness, Proc, _default_spawn, _redact

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "codex_stream_basic.jsonl"


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
    return RunSpec(prompt=prompt, provider=Provider.CODEX, model="gpt-5-codex", **kw)  # type: ignore[arg-type]


async def _collect(agen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in agen]


# -- argv ----------------------------------------------------------------------


async def test_run_builds_expected_argv() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    spec = _spec(
        "do the thing",
        system_prompt="you are helpful",
        effort="high",
        permission_mode=PermissionMode.AUTO,
        working_dir="/work/dir",
    )
    await _collect(h.run(spec))

    argv = spawn.argv
    assert argv is not None
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5-codex"
    # AUTO -> workspace-write sandbox
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == "/work/dir"
    # reasoning effort via -c override
    assert "model_reasoning_effort=high" in argv
    # system prompt via -c model_instructions override (json-encoded value)
    assert any(a.startswith("model_instructions=") for a in argv)
    # prompt is the final positional arg
    assert argv[-1] == "do the thing"
    # working_dir is also passed as cwd
    assert spawn.cwd == "/work/dir"


async def test_run_read_only_maps_to_read_only_sandbox() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec(permission_mode=PermissionMode.READ_ONLY)))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--sandbox") + 1] == "read-only"


async def test_run_ask_maps_to_read_only_sandbox() -> None:
    # I1: codex exec is non-interactive (approval never) — there is no human to
    # approve writes, so ASK must NOT silently grant workspace-write. Conservative:
    # "can't ask -> don't allow writes". (Diverges from the Claude adapter, which
    # CAN pass --permission-mode ask through to an interactive prompt.)
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec(permission_mode=PermissionMode.ASK)))
    argv = spawn.argv
    assert argv is not None
    assert argv[argv.index("--sandbox") + 1] == "read-only"


async def test_run_sets_explicit_never_approval_policy() -> None:
    # I2: security posture is self-documenting — explicitly pin the non-interactive
    # approval policy rather than relying on the exec default.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    argv = spawn.argv
    assert argv is not None
    assert "approval_policy=never" in argv


async def test_run_prompt_preceded_by_end_of_options_separator() -> None:
    # C1: a prompt that looks like a flag must NOT be parsed as one. The prompt
    # must be the last element, immediately preceded by a literal "--".
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec("--version")))
    argv = spawn.argv
    assert argv is not None
    assert argv[-1] == "--version"
    assert argv[-2] == "--"
    # "--version" appears only after the separator, never as a parsed flag.
    sep = argv.index("--")
    assert "--version" not in argv[:sep]


async def test_run_resume_prompt_preceded_by_end_of_options_separator() -> None:
    # C1 (resume path): same guard for `codex exec ... resume <id> -- <prompt>`.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec("--version", resume_session_id="thread-xyz")))
    argv = spawn.argv
    assert argv is not None
    assert argv[-1] == "--version"
    assert argv[-2] == "--"
    ri = argv.index("resume")
    assert argv[ri + 1] == "thread-xyz"
    assert argv[ri + 2] == "--"
    assert argv[ri + 3] == "--version"


async def test_run_resume_builds_resume_subcommand() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec("again", resume_session_id="thread-xyz")))
    argv = spawn.argv
    assert argv is not None
    # `codex exec ... resume <id> -- <prompt>`
    assert "resume" in argv
    ri = argv.index("resume")
    assert argv[ri + 1] == "thread-xyz"
    assert argv[ri + 2] == "--"
    assert argv[ri + 3] == "again"
    assert argv[-1] == "again"


async def test_run_no_optional_flags_when_unset() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    argv = spawn.argv
    assert argv is not None
    assert "resume" not in argv
    assert not any(a.startswith("model_reasoning_effort=") for a in argv)
    assert not any(a.startswith("model_instructions=") for a in argv)
    # default permission mode read-only -> read-only sandbox
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--cd" not in argv
    assert spawn.cwd is None


# -- streaming -----------------------------------------------------------------


async def test_run_yields_normalized_events_ending_in_run_done() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    events = await _collect(h.run(_spec()))

    kinds = [type(e) for e in events]
    assert kinds == [
        ThinkingDelta,
        ToolUse,
        ToolResult,
        ToolUse,
        ToolResult,
        TextDelta,
        ToolResult,
        Usage,
        RunDone,
    ]
    done = events[-1]
    assert isinstance(done, RunDone)
    assert done.session_id == "019e848c-7d27-7b53-bc90-1230355e8f87"


async def test_structural_async_generator_and_protocol() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    result: list[StreamEvent] = []
    async for event in h.run(_spec()):
        result.append(event)
    assert result
    assert isinstance(result[-1], RunDone)
    assert isinstance(h, AgentBackend)


# -- failures ------------------------------------------------------------------


async def test_nonzero_exit_raises_harness_error() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines(), returncode=1, stderr="boom"))
    h = CodexHarness(spawn=spawn)
    with pytest.raises(HarnessError) as ei:
        await _collect(h.run(_spec()))
    assert "boom" in str(ei.value)


async def test_auth_failure_raises_auth_error() -> None:
    spawn = FakeSpawn(
        FakeProc(
            _fixture_lines(),
            returncode=1,
            stderr="Error: Not logged in. Please run codex login",
        )
    )
    h = CodexHarness(spawn=spawn)
    with pytest.raises(HarnessAuthError):
        await _collect(h.run(_spec()))


async def test_nonzero_exit_redacts_secrets_in_error() -> None:
    secret = "sk-ant-abcdef0123456789ABCDEF"
    spawn = FakeSpawn(FakeProc(_fixture_lines(), returncode=2, stderr=f"failed with key {secret}"))
    h = CodexHarness(spawn=spawn)
    with pytest.raises(HarnessError) as ei:
        await _collect(h.run(_spec()))
    assert secret not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


async def test_timeout_raises_harness_timeout() -> None:
    async def spawn(argv: list[str], cwd: str | None) -> Proc:
        return TimeoutProc()

    h = CodexHarness(spawn=spawn, timeout=0.05)
    with pytest.raises(HarnessTimeout):
        await _collect(h.run(_spec()))


# -- send_command --------------------------------------------------------------


async def test_send_command_unsupported_raises() -> None:
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    # codex advertises no supported commands today (OQ-1), so anything raises.
    with pytest.raises(HarnessError):
        await _collect(h.send_command("thread-1", "/compact"))


async def test_send_command_supported_streams_when_registered() -> None:
    # If a command is registered as supported, send_command resumes & streams.
    spawn = FakeSpawn(FakeProc(_fixture_lines()))
    h = CodexHarness(spawn=spawn)
    h.supported_commands = {"/compact"}
    events = await _collect(h.send_command("thread-1", "/compact"))
    argv = spawn.argv
    assert argv is not None
    assert "resume" in argv
    ri = argv.index("resume")
    assert argv[ri + 1] == "thread-1"
    # C1/M2: command (a positional) is guarded by the end-of-options separator.
    assert argv[ri + 2] == "--"
    assert argv[-1] == "/compact"
    assert argv[-2] == "--"
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "019e848c-7d27-7b53-bc90-1230355e8f87"


# -- helpers / capabilities ----------------------------------------------------


def test_capabilities() -> None:
    h = CodexHarness(spawn=FakeSpawn(FakeProc([])))
    assert h.name == "codex"
    # Conservative: no verified non-interactive compaction command (OQ-1).
    assert h.supported_commands == set()


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
    h = CodexHarness(spawn=spawn)
    await _collect(h.run(_spec()))
    assert proc.kill_calls == 1


async def test_proc_killed_on_error_exit() -> None:
    proc = FakeProc(_fixture_lines(), returncode=1, stderr="boom")
    spawn = FakeSpawn(proc)
    h = CodexHarness(spawn=spawn)
    with pytest.raises(HarnessError):
        await _collect(h.run(_spec()))
    assert proc.kill_calls == 1


async def test_proc_killed_on_early_generator_close() -> None:
    proc = FakeProc(_fixture_lines())
    spawn = FakeSpawn(proc)
    h = CodexHarness(spawn=spawn)
    agen = h.run(_spec())
    await agen.__anext__()  # consume one event, then abandon the generator
    await agen.aclose()
    assert proc.kill_calls == 1


# -- real-subprocess regression tests -----------------------------------------
#
# These drive the REAL `_default_spawn` against a trivial deterministic child
# (python3 -c "..."), NEVER the codex CLI. They reproduce the concurrency/kill/
# limit hazards the in-memory FakeProc cannot. Each is wrapped in
# asyncio.wait_for so a regression manifests as a fast failure rather than a hang.


def _child_spawn(child_src: str):  # type: ignore[no-untyped-def]
    """A Spawn that ignores the harness-built argv and runs our child script."""

    async def spawn(argv: list[str], cwd: str | None) -> Proc:
        return await _default_spawn([sys.executable, "-c", child_src], cwd)

    return spawn


async def test_real_subprocess_stderr_drain_does_not_deadlock() -> None:
    # Concurrent-drain: child emits a stdout line, floods 500 KB straight to
    # stderr (>> the OS pipe buffer) BEFORE more stdout, then exits nonzero. If
    # stderr were only read after stdout EOF, this would deadlock. Bounded by
    # wait_for so a regression fails fast instead of hanging.
    child = (
        "import sys, os\n"
        "sys.stdout.write('{}' + chr(10)); sys.stdout.flush()\n"
        "os.write(2, b'x' * 500000)\n"
        "sys.stdout.write('{}' + chr(10)); sys.stdout.flush()\n"
        "sys.exit(3)\n"
    )
    h = CodexHarness(spawn=_child_spawn(child), timeout=10.0)
    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(h.run(_spec())), timeout=5.0)
    assert not isinstance(ei.value, HarnessTimeout)
    assert "exited 3" in str(ei.value)


async def test_real_subprocess_stderr_secret_is_redacted() -> None:
    child = (
        "import sys\nsys.stderr.write('boom key=sk-ant-abcdef0123456789ABCDEF tail')\nsys.exit(1)\n"
    )
    h = CodexHarness(spawn=_child_spawn(child), timeout=10.0)
    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(h.run(_spec())), timeout=5.0)
    msg = str(ei.value)
    assert "sk-ant-abcdef0123456789ABCDEF" not in msg
    assert "[REDACTED]" in msg


async def test_real_subprocess_timeout_kills_child() -> None:
    # Short timeout; child sleeps far past it. We must raise HarnessTimeout
    # quickly AND the child must be killed/reaped (not left running).
    child = "import time\ntime.sleep(30)\n"
    spawn = _child_spawn(child)
    captured: dict[str, Proc] = {}

    async def capturing_spawn(argv: list[str], cwd: str | None) -> Proc:
        proc = await spawn(argv, cwd)
        captured["proc"] = proc
        return proc

    h = CodexHarness(spawn=capturing_spawn, timeout=1.0)
    with pytest.raises(HarnessTimeout):
        await asyncio.wait_for(_collect(h.run(_spec())), timeout=5.0)

    aproc = captured["proc"]
    underlying = aproc._process  # type: ignore[attr-defined]
    assert underlying.returncode is not None  # reaped
    import os

    with pytest.raises((ProcessLookupError, PermissionError)):
        os.kill(underlying.pid, 0)


async def test_real_subprocess_big_valid_json_line_parses() -> None:
    # A single valid JSONL line well over the default 64 KiB limit must parse,
    # proving we raised the StreamReader limit.
    big_text = "y" * 200000
    line = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": big_text}}
    )
    started = json.dumps({"type": "thread.started", "thread_id": "t-big"})
    completed = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}
    )
    child = (
        "import sys\n"
        f"sys.stdout.write({started!r} + '\\n')\n"
        f"sys.stdout.write({line!r} + '\\n')\n"
        f"sys.stdout.write({completed!r} + '\\n')\n"
        "sys.exit(0)\n"
    )
    h = CodexHarness(spawn=_child_spawn(child), timeout=10.0)
    events = await asyncio.wait_for(_collect(h.run(_spec())), timeout=5.0)
    text_events = [e for e in events if isinstance(e, TextDelta)]
    assert len(text_events) == 1
    assert text_events[0].text == big_text
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "t-big"


async def test_real_subprocess_oversized_line_becomes_harness_error() -> None:
    # A line beyond the raised limit converts to a typed HarnessError rather than
    # escaping as a raw ValueError/LimitOverrunError. Use a tiny limit so we
    # don't have to emit 8 MB.
    async def small_limit_spawn(argv: list[str], cwd: str | None) -> Proc:
        from app.harness.codex import _AsyncioProc

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys\nsys.stdout.write('z' * 100000 + '\\n')\nsys.exit(0)\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024,  # deliberately tiny so the 100 KB line overruns
        )
        return _AsyncioProc(proc)

    h = CodexHarness(spawn=small_limit_spawn, timeout=10.0)
    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(h.run(_spec())), timeout=5.0)
    assert not isinstance(ei.value, HarnessTimeout)
    assert "over-long" in str(ei.value)
