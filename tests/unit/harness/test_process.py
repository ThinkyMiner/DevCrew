"""Tests for the shared subprocess runner (app.harness.process).

The runner is the deep module both CLI adapters delegate to. Its interface IS
the test surface: drive ``run_process`` with trivial provider callbacks and a
fake (or a real trivial child) ``spawn``. The subprocess-hardening regressions
(concurrent stderr drain, kill/reap, the over-long-line guard, the raised line
limit) live here ONCE — they used to be copy-pasted into both adapter test
files.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator

import pytest

from app.domain.errors import HarnessAuthError, HarnessError, HarnessTimeout
from app.domain.events import RunDone, StreamEvent, TextDelta
from app.harness.process import (
    AsyncioProc,
    Proc,
    make_default_spawn,
    redact,
    run_process,
)

_AUTH = ("not authenticated", "please run /login", "401")


# -- trivial provider callbacks ------------------------------------------------


def _parse_line(obj: dict[str, object]) -> list[StreamEvent]:
    if obj.get("type") == "text":
        return [TextDelta(text=str(obj.get("text", "")))]
    return []


def _session_id_of(obj: dict[str, object]) -> str | None:
    sid = obj.get("session_id")
    return sid if isinstance(sid, str) else None


# -- fakes ---------------------------------------------------------------------


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


def _spawn_for(proc: Proc):  # type: ignore[no-untyped-def]
    async def spawn(argv: list[str], cwd: str | None) -> Proc:
        return proc

    return spawn


def _run(proc: Proc, *, fallback: str | None = None, timeout: float = 10.0):  # type: ignore[no-untyped-def]
    return run_process(
        ["ignored"],
        None,
        spawn=_spawn_for(proc),
        provider="test",
        parse_line=_parse_line,
        session_id_of=_session_id_of,
        auth_patterns=_AUTH,
        fallback_session_id=fallback,
        timeout=timeout,
    )


async def _collect(agen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in agen]


# -- streaming -----------------------------------------------------------------


async def test_yields_parsed_events_then_run_done_with_captured_session_id() -> None:
    lines = [
        json.dumps({"type": "text", "text": "hi", "session_id": "sess-1"}),
        json.dumps({"type": "noise"}),  # parsed to nothing
    ]
    events = await _collect(_run(FakeProc(lines)))
    assert [type(e) for e in events] == [TextDelta, RunDone]
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "sess-1"


async def test_fallback_session_id_used_when_stream_surfaces_none() -> None:
    proc = FakeProc([json.dumps({"type": "text", "text": "x"})])
    events = await _collect(_run(proc, fallback="prev"))
    done = events[-1]
    assert isinstance(done, RunDone)
    assert done.session_id == "prev"


async def test_non_json_and_non_dict_lines_are_skipped() -> None:
    lines = [
        "not json at all",
        json.dumps([1, 2, 3]),
        "",
        json.dumps({"type": "text", "text": "ok"}),
    ]
    events = await _collect(_run(FakeProc(lines)))
    assert [type(e) for e in events] == [TextDelta, RunDone]


# -- failures ------------------------------------------------------------------


async def test_nonzero_exit_raises_harness_error_with_stderr() -> None:
    with pytest.raises(HarnessError) as ei:
        await _collect(_run(FakeProc([], returncode=1, stderr="boom")))
    assert "boom" in str(ei.value)
    assert "test exited 1" in str(ei.value)


async def test_auth_pattern_raises_auth_error() -> None:
    proc = FakeProc([], returncode=1, stderr="Error: Not authenticated. Please run /login")
    with pytest.raises(HarnessAuthError):
        await _collect(_run(proc))


async def test_nonzero_exit_redacts_secrets() -> None:
    secret = "sk-ant-abcdef0123456789ABCDEF"
    with pytest.raises(HarnessError) as ei:
        await _collect(_run(FakeProc([], returncode=2, stderr=f"failed key {secret}")))
    assert secret not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


async def test_timeout_raises_harness_timeout() -> None:
    class TimeoutProc:
        async def stdout_lines(self) -> AsyncIterator[str]:
            await asyncio.sleep(3600)
            yield ""  # pragma: no cover

        async def wait(self) -> int:  # pragma: no cover
            return 0

        async def stderr_text(self) -> str:  # pragma: no cover
            return ""

        async def kill(self) -> None:
            return None

    with pytest.raises(HarnessTimeout):
        await _collect(_run(TimeoutProc(), timeout=0.05))


# -- C2: child killed/reaped on every exit path (fake-level) -------------------


async def test_proc_killed_on_normal_completion() -> None:
    proc = FakeProc([json.dumps({"type": "text", "text": "x"})])
    await _collect(_run(proc))
    assert proc.kill_calls == 1


async def test_proc_killed_on_error_exit() -> None:
    proc = FakeProc([], returncode=1, stderr="boom")
    with pytest.raises(HarnessError):
        await _collect(_run(proc))
    assert proc.kill_calls == 1


async def test_proc_killed_on_early_generator_close() -> None:
    proc = FakeProc([json.dumps({"type": "text", "text": "x"})])
    agen = _run(proc)
    await agen.__anext__()  # consume one event, then abandon the generator
    await agen.aclose()
    assert proc.kill_calls == 1


# -- redaction -----------------------------------------------------------------


def test_redact_scrubs_tokens() -> None:
    assert "sk-ant-abcdefghijklmnop" not in redact("key=sk-ant-abcdefghijklmnop done")
    assert "[REDACTED]" in redact("Bearer abc.def.ghi")
    assert redact("nothing secret here") == "nothing secret here"


# -- real-subprocess regression tests -----------------------------------------
#
# These drive the REAL spawn against a trivial deterministic child
# (python3 -c "..."), NEVER a CLI. They reproduce the concurrency/kill/limit
# hazards the in-memory FakeProc cannot. Each is wrapped in asyncio.wait_for so a
# regression manifests as a fast failure rather than a hang.

_REAL_SPAWN = make_default_spawn(provider="test")


def _child_spawn(child_src: str):  # type: ignore[no-untyped-def]
    """A Spawn that ignores the built argv and runs our child script."""

    async def spawn(argv: list[str], cwd: str | None) -> Proc:
        return await _REAL_SPAWN([sys.executable, "-c", child_src], cwd)

    return spawn


def _run_real(spawn, *, timeout: float = 10.0):  # type: ignore[no-untyped-def]
    return run_process(
        ["ignored"],
        None,
        spawn=spawn,
        provider="test",
        parse_line=_parse_line,
        session_id_of=_session_id_of,
        auth_patterns=_AUTH,
        timeout=timeout,
    )


async def test_real_subprocess_stderr_drain_does_not_deadlock() -> None:
    # Child emits a stdout line, floods 500 KB straight to the stderr fd (>> the
    # OS pipe buffer) BEFORE more stdout, then exits nonzero. If stderr were only
    # read after stdout EOF, the child would block writing stderr while we block
    # reading stdout -> deadlock. Concurrent draining fixes it.
    child = (
        "import sys, os\n"
        "sys.stdout.write('{}' + chr(10)); sys.stdout.flush()\n"
        "os.write(2, b'x' * 500000)\n"
        "sys.stdout.write('{}' + chr(10)); sys.stdout.flush()\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(_run_real(_child_spawn(child))), timeout=5.0)
    assert not isinstance(ei.value, HarnessTimeout)
    assert "exited 3" in str(ei.value)


async def test_real_subprocess_stderr_secret_is_redacted() -> None:
    child = (
        "import sys\nsys.stderr.write('boom key=sk-ant-abcdef0123456789ABCDEF tail')\nsys.exit(1)\n"
    )
    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(_run_real(_child_spawn(child))), timeout=5.0)
    msg = str(ei.value)
    assert "sk-ant-abcdef0123456789ABCDEF" not in msg
    assert "[REDACTED]" in msg


async def test_real_subprocess_timeout_kills_child() -> None:
    # Child sleeps far past the short timeout. We must raise HarnessTimeout
    # quickly AND the child must be killed/reaped (not left running).
    child = "import time\ntime.sleep(30)\n"
    base = _child_spawn(child)
    captured: dict[str, Proc] = {}

    async def capturing_spawn(argv: list[str], cwd: str | None) -> Proc:
        proc: Proc = await base(argv, cwd)
        captured["proc"] = proc
        return proc

    with pytest.raises(HarnessTimeout):
        await asyncio.wait_for(_collect(_run_real(capturing_spawn, timeout=1.0)), timeout=5.0)

    aproc = captured["proc"]
    underlying = aproc._process  # type: ignore[attr-defined]
    assert underlying.returncode is not None  # reaped
    import os

    with pytest.raises((ProcessLookupError, PermissionError)):
        os.kill(underlying.pid, 0)


async def test_real_subprocess_big_valid_json_line_parses() -> None:
    # A single valid line well over the default 64 KiB limit must parse, proving
    # we raised the StreamReader limit.
    big_text = "y" * 200000
    line = json.dumps({"type": "text", "text": big_text, "session_id": "sess-big"})
    child = "import sys\n" f"sys.stdout.write({line!r} + '\\n')\n" "sys.exit(0)\n"
    events = await asyncio.wait_for(_collect(_run_real(_child_spawn(child))), timeout=5.0)
    text_events = [e for e in events if isinstance(e, TextDelta)]
    assert len(text_events) == 1
    assert text_events[0].text == big_text
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "sess-big"


async def test_real_subprocess_oversized_line_becomes_harness_error() -> None:
    # A line beyond the raised limit converts to a typed HarnessError rather than
    # escaping as a raw ValueError/LimitOverrunError. Use a tiny limit via a
    # dedicated spawn so we don't have to emit 8 MB.
    async def small_limit_spawn(argv: list[str], cwd: str | None) -> Proc:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys\nsys.stdout.write('z' * 100000 + '\\n')\nsys.exit(0)\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024,  # deliberately tiny so the 100 KB line overruns
        )
        return AsyncioProc(proc, "test")

    with pytest.raises(HarnessError) as ei:
        await asyncio.wait_for(_collect(_run_real(small_limit_spawn)), timeout=5.0)
    assert not isinstance(ei.value, HarnessTimeout)
    assert "over-long" in str(ei.value)
