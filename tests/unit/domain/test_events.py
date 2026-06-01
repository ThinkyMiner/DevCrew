from app.domain.events import (
    RunDone,
    RunError,
    TextDelta,
    ThinkingDelta,
    ToolUse,
    Usage,
    parse_event,
)


def test_text_delta_roundtrips():
    e = TextDelta(text="hi")
    assert e.kind == "text"
    assert parse_event(e.model_dump()) == e


def test_parse_dispatches_on_kind():
    assert isinstance(parse_event({"kind": "thinking", "text": "..."}), ThinkingDelta)
    assert isinstance(parse_event({"kind": "tool_use", "name": "grep", "input": {}}), ToolUse)
    assert isinstance(parse_event({"kind": "usage", "input_tokens": 1, "output_tokens": 2}), Usage)
    assert isinstance(
        parse_event({"kind": "error", "error_kind": "HarnessError", "message": "x"}), RunError
    )
    assert isinstance(parse_event({"kind": "done", "session_id": "s1"}), RunDone)
