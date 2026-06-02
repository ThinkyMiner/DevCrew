"""Opt-in REAL-CLI smoke test (Task 7.4) — DESELECTED BY DEFAULT.

Every test here is marked ``@pytest.mark.live`` and the default ``pytest``
invocation excludes it via ``addopts = "-m 'not live'"`` in ``pyproject.toml``,
so the normal suite NEVER spawns a real CLI or spends tokens (AGENTS §5).

Purpose
-------
Validate the real adapter + parser against a TRUE capture: spawn the real
``claude`` (and/or ``codex``) once with a trivial prompt through the real
``asyncio`` spawn, and assert the harness normalizes the live stream into our
:class:`StreamEvent` union — at minimum surfacing some text and a terminal
``RunDone`` carrying a ``session_id`` we could resume from.

Run it manually (spends a small number of tokens; requires the CLIs installed
and logged in)::

    pytest -m live                       # all live tests
    pytest -m live -k claude             # just the claude smoke
    pytest -m live -s                    # show the captured text

If a CLI is not on PATH the test SKIPS (so `pytest -m live` on a machine without
codex still runs the claude one). A nonzero exit / auth failure raises a typed
HarnessError — that is a real, located failure, not a silent pass.
"""

from __future__ import annotations

import shutil

import pytest

from app.domain.events import RunDone, TextDelta
from app.domain.models import Provider
from app.harness.base import RunSpec
from app.harness.claude import ClaudeHarness
from app.harness.codex import CodexHarness

pytestmark = pytest.mark.live

_TRIVIAL_PROMPT = "Reply with exactly the word: pong. No other text."


@pytest.mark.asyncio
async def test_claude_real_stream_normalizes_to_events() -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")

    harness = ClaudeHarness()
    spec = RunSpec(
        prompt=_TRIVIAL_PROMPT,
        provider=Provider.CLAUDE,
        model="claude-sonnet-4-5",
    )
    events = [event async for event in harness.run(spec)]

    # The stream must end with exactly one terminal RunDone carrying a resumable
    # session id (the durability/resume contract depends on capturing it).
    assert events, "claude produced no events"
    assert isinstance(events[-1], RunDone), f"last event must be RunDone, got {events[-1]!r}"
    assert events[-1].session_id, "RunDone must carry a session_id for resume"
    # And it must have surfaced at least one text delta (the parser mapped the
    # real assistant output into our normalized union).
    assert any(isinstance(e, TextDelta) and e.text for e in events), "expected a text delta"


@pytest.mark.asyncio
async def test_codex_real_stream_normalizes_to_events() -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH")

    harness = CodexHarness()
    spec = RunSpec(
        prompt=_TRIVIAL_PROMPT,
        provider=Provider.CODEX,
        model="gpt-5-codex",
    )
    events = [event async for event in harness.run(spec)]

    assert events, "codex produced no events"
    assert isinstance(events[-1], RunDone), f"last event must be RunDone, got {events[-1]!r}"
    assert events[-1].session_id, "RunDone must carry a session_id for resume"
    assert any(isinstance(e, TextDelta) and e.text for e in events), "expected a text delta"
