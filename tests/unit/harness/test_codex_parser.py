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
from app.harness.codex_parser import parse_codex_line, session_id_of

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "codex_stream_basic.jsonl"


def _fixture_objs() -> list[dict[str, object]]:
    objs: list[dict[str, object]] = []
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            assert isinstance(obj, dict)
            objs.append(obj)
    return objs


def _flatten(objs: list[dict[str, object]]) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    for obj in objs:
        out.extend(parse_codex_line(obj))
    return out


def test_fixture_is_valid_jsonl() -> None:
    for obj in _fixture_objs():
        assert isinstance(obj, dict)


def test_normalized_sequence_from_fixture() -> None:
    events = _flatten(_fixture_objs())
    kinds = [type(e) for e in events]
    # thread.started + turn.started -> [] ; in_progress command -> [] ;
    # todo_list -> [] (unknown/irrelevant). The completed items map in order.
    assert kinds == [
        ThinkingDelta,  # reasoning
        ToolUse,  # completed command_execution
        ToolResult,  # completed command_execution
        ToolUse,  # completed mcp_tool_call
        ToolResult,  # completed mcp_tool_call
        TextDelta,  # agent_message
        ToolResult,  # error item (mapped to an errored ToolResult, not a crash)
        Usage,  # turn.completed
    ]


def test_reasoning_maps_to_thinking() -> None:
    events = parse_codex_line(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}}
    )
    assert events == [ThinkingDelta(text="hmm")]


def test_reasoning_summary_text_fallback() -> None:
    events = parse_codex_line(
        {"type": "item.completed", "item": {"type": "reasoning", "summary_text": "sum"}}
    )
    assert events == [ThinkingDelta(text="sum")]


def test_agent_message_maps_to_text() -> None:
    events = parse_codex_line(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi there"}}
    )
    assert events == [TextDelta(text="hi there")]


def test_command_execution_completed_maps_to_tool_use_and_result() -> None:
    events = parse_codex_line(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "echo hi",
                "aggregated_output": "hi\n",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert len(events) == 2
    tool_use, tool_result = events
    assert isinstance(tool_use, ToolUse)
    assert tool_use.name == "command_execution"
    assert tool_use.input["command"] == "echo hi"
    assert tool_use.tool_id == "item_1"
    assert isinstance(tool_result, ToolResult)
    assert tool_result.tool_id == "item_1"
    assert tool_result.content == "hi\n"
    assert tool_result.is_error is False


def test_command_execution_failed_marks_error() -> None:
    events = parse_codex_line(
        {
            "type": "item.completed",
            "item": {
                "id": "c2",
                "type": "command_execution",
                "command": "false",
                "aggregated_output": "boom",
                "exit_code": 127,
                "status": "failed",
            },
        }
    )
    result = events[1]
    assert isinstance(result, ToolResult)
    assert result.is_error is True


def test_command_execution_in_progress_yields_nothing() -> None:
    # Only completed items carry final output; started/in_progress are ignored so
    # we don't double-emit when item.completed arrives.
    events = parse_codex_line(
        {
            "type": "item.started",
            "item": {
                "id": "c3",
                "type": "command_execution",
                "command": "sleep 1",
                "status": "in_progress",
            },
        }
    )
    assert events == []


def test_mcp_tool_call_maps_to_tool_result() -> None:
    events = parse_codex_line(
        {
            "type": "item.completed",
            "item": {
                "id": "m1",
                "type": "mcp_tool_call",
                "server": "fs",
                "tool": "read",
                "status": "completed",
                "result": "data",
            },
        }
    )
    assert len(events) == 2
    use, result = events
    assert isinstance(use, ToolUse)
    assert use.name == "fs.read"
    assert isinstance(result, ToolResult)
    assert result.is_error is False


def test_error_item_maps_to_errored_tool_result() -> None:
    events = parse_codex_line(
        {"type": "item.completed", "item": {"type": "error", "message": "kaboom"}}
    )
    assert len(events) == 1
    err = events[0]
    assert isinstance(err, ToolResult)
    assert err.is_error is True
    assert "kaboom" in err.content


def test_turn_completed_maps_usage_with_cache_proxy() -> None:
    events = parse_codex_line(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
            },
        }
    )
    assert len(events) == 1
    usage = events[0]
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 7
    # context proxy: input + cached + output + reasoning
    assert usage.context_tokens == 100 + 40 + 7 + 3


def test_turn_failed_maps_to_run_error_message() -> None:
    # turn.failed surfaces an error; the parser stays pure and reports it as an
    # errored ToolResult so the adapter/orchestrator can see the failure text
    # without the parser owning terminal RunError emission.
    events = parse_codex_line(
        {"type": "turn.failed", "error": {"message": "context_window_exceeded"}}
    )
    assert len(events) == 1
    err = events[0]
    assert isinstance(err, ToolResult)
    assert err.is_error is True
    assert "context_window_exceeded" in err.content


def test_unknown_line_returns_empty() -> None:
    assert parse_codex_line({"type": "thread.started", "thread_id": "x"}) == []
    assert parse_codex_line({"type": "turn.started"}) == []
    assert parse_codex_line({"type": "totally.unknown"}) == []
    assert parse_codex_line({"no": "type"}) == []
    assert (
        parse_codex_line({"type": "item.completed", "item": {"type": "todo_list", "items": []}})
        == []
    )


def test_malformed_item_does_not_crash() -> None:
    assert parse_codex_line({"type": "item.completed", "item": "not-a-dict"}) == []
    assert parse_codex_line({"type": "item.completed"}) == []


def test_session_id_of_reads_thread_id() -> None:
    assert session_id_of({"type": "thread.started", "thread_id": "abc"}) == "abc"
    assert session_id_of({"type": "turn.started"}) is None
    assert session_id_of({"type": "thread.started", "thread_id": ""}) is None
