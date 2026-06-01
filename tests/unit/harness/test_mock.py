from __future__ import annotations

import pytest

from app.domain.errors import HarnessError
from app.domain.events import RunDone, StreamEvent, TextDelta, Usage
from app.domain.models import Provider
from app.harness.base import RunSpec
from app.harness.mock import MockHarness


def _spec(prompt: str, **kw: object) -> RunSpec:
    return RunSpec(prompt=prompt, provider=Provider.MOCK, model="m", **kw)  # type: ignore[arg-type]


async def _collect(agen: object) -> list[StreamEvent]:
    return [e async for e in agen]  # type: ignore[attr-defined,union-attr]


async def test_mock_emits_text_and_done_and_records_spec() -> None:
    h = MockHarness()
    spec = _spec("hello world")
    events = await _collect(h.run(spec))
    assert any(getattr(e, "text", "").strip() for e in events)
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id
    assert any(isinstance(e, Usage) for e in events)
    assert h.calls[0].prompt == "hello world"


async def test_mock_default_text_references_prompt() -> None:
    h = MockHarness()
    events = await _collect(h.run(_spec("SPECIAL-TOKEN-42")))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "SPECIAL-TOKEN-42" in text


async def test_mock_session_ids_are_deterministic_and_increment() -> None:
    h = MockHarness()
    s1 = (await _collect(h.run(_spec("a"))))[-1]
    s2 = (await _collect(h.run(_spec("b"))))[-1]
    assert isinstance(s1, RunDone) and isinstance(s2, RunDone)
    assert s1.session_id == "mock-1"
    assert s2.session_id == "mock-2"


async def test_mock_resumes_same_session() -> None:
    h = MockHarness()
    first = await _collect(h.run(_spec("a")))
    s1 = first[-1].session_id  # type: ignore[union-attr]
    assert s1
    second = await _collect(h.run(_spec("b", resume_session_id=s1)))
    s2 = second[-1].session_id  # type: ignore[union-attr]
    assert s2 == s1


async def test_script_reply_emits_scripted_text() -> None:
    h = MockHarness()
    h.script_reply("discuss", "ALPHA-SAYS-X")
    events = await _collect(h.run(_spec("please discuss the topic")))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "ALPHA-SAYS-X" in text
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id


async def test_constructor_script_with_explicit_events() -> None:
    scripted: list[StreamEvent] = [TextDelta(text="scripted-A"), RunDone(session_id="custom-1")]
    h = MockHarness(script={"trigger": scripted})
    events = await _collect(h.run(_spec("contains trigger here")))
    assert isinstance(events[-1], RunDone)
    # explicit RunDone in the script is respected (not appended a second time)
    assert sum(isinstance(e, RunDone) for e in events) == 1
    assert events[-1].session_id == "custom-1"


async def test_last_prompt_helpers() -> None:
    h = MockHarness()
    await _collect(h.run(_spec("first prompt")))
    await _collect(h.run(_spec("second prompt")))
    assert h.last_prompt == "second prompt"
    # last_prompt_for filters by resume session id
    s1 = "mock-1"
    await _collect(h.run(_spec("resumed prompt", resume_session_id=s1)))
    assert h.last_prompt_for(s1) == "resumed prompt"


async def test_last_prompt_for_unknown_session_returns_none() -> None:
    h = MockHarness()
    assert h.last_prompt_for("nope") is None


async def test_last_prompt_when_no_calls_is_none() -> None:
    h = MockHarness()
    assert h.last_prompt is None


async def test_send_command_supported_emits_events() -> None:
    h = MockHarness()
    events = await _collect(h.send_command("mock-1", "/compact"))
    assert any(isinstance(e, TextDelta) and "[compacted]" in e.text for e in events)
    assert isinstance(events[-1], RunDone)
    assert events[-1].session_id == "mock-1"


async def test_send_command_unsupported_raises() -> None:
    h = MockHarness()
    with pytest.raises(HarnessError):
        await _collect(h.send_command("mock-1", "/bogus"))


def test_mock_capabilities() -> None:
    h = MockHarness()
    assert h.name == "mock"
    assert h.supported_commands == {"/compact", "/clear"}


def test_mock_is_agent_backend() -> None:
    from app.harness.base import AgentBackend

    assert isinstance(MockHarness(), AgentBackend)
