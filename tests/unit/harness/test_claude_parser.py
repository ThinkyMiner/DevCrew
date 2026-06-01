from __future__ import annotations

import json
from pathlib import Path

from app.domain.events import (
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    Usage,
)
from app.harness.claude_parser import parse_claude_line, session_id_of

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "claude_stream_basic.jsonl"


def _lines() -> list[dict[str, object]]:
    text = _FIXTURE.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_fixture_full_sequence() -> None:
    events: list[StreamEvent] = []
    for obj in _lines():
        events.extend(parse_claude_line(obj))

    kinds = [type(e) for e in events]
    assert kinds == [ThinkingDelta, ToolUse, ToolResult, TextDelta, Usage]

    thinking, tool_use, tool_result, text, usage = events
    assert isinstance(thinking, ThinkingDelta) and "weather tool" in thinking.text
    assert isinstance(tool_use, ToolUse)
    assert tool_use.name == "get_weather"
    assert tool_use.tool_id == "toolu_01"
    assert tool_use.input == {"city": "Paris"}
    assert isinstance(tool_result, ToolResult)
    assert tool_result.tool_id == "toolu_01"
    assert tool_result.content == "18C and sunny"
    assert tool_result.is_error is False
    assert isinstance(text, TextDelta) and text.text == "It is 18C and sunny in Paris."
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 10
    assert usage.output_tokens == 240
    # context_tokens derived: input + cache_creation + cache_read + output
    assert usage.context_tokens == 10 + 55356 + 0 + 240


def test_init_line_emits_nothing_but_carries_session_id() -> None:
    init = next(o for o in _lines() if o.get("subtype") == "init")
    assert parse_claude_line(init) == []
    assert session_id_of(init) == "sess-abc123"


def test_unknown_lines_parse_to_empty() -> None:
    for obj in _lines():
        if obj.get("type") == "stream_event" or obj.get("subtype") == "hook_started":
            assert parse_claude_line(obj) == []
    # explicit unknown type
    assert parse_claude_line({"type": "totally_unknown", "foo": 1}) == []
    assert parse_claude_line({}) == []


def test_tool_result_accepts_plain_string_content() -> None:
    obj = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t9",
                    "content": "plain text",
                    "is_error": True,
                }
            ]
        },
    }
    events = parse_claude_line(obj)
    assert len(events) == 1
    tr = events[0]
    assert isinstance(tr, ToolResult)
    assert tr.content == "plain text"
    assert tr.is_error is True


def test_result_without_cache_fields_uses_input_plus_output_proxy() -> None:
    # M3: non-cached turn still surfaces a context_tokens proxy (input+output).
    obj = {
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    events = parse_claude_line(obj)
    assert len(events) == 1
    usage = events[0]
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7
    assert usage.context_tokens == 12


def test_result_with_no_token_fields_has_no_context_tokens() -> None:
    obj = {"type": "result", "subtype": "success", "usage": {}}
    events = parse_claude_line(obj)
    assert len(events) == 1
    usage = events[0]
    assert isinstance(usage, Usage)
    assert usage.context_tokens is None


def test_session_id_of_returns_none_when_absent() -> None:
    assert session_id_of({"type": "result"}) is None
    assert session_id_of({"session_id": ""}) is None


def test_malformed_assistant_does_not_crash() -> None:
    assert parse_claude_line({"type": "assistant"}) == []
    assert parse_claude_line({"type": "assistant", "message": {"content": "nope"}}) == []
    assert parse_claude_line({"type": "assistant", "message": {"content": ["bad", 3]}}) == []
