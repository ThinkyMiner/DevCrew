"""Single source of truth for the ``codex exec --json`` event schema.

ALL knowledge of the Codex CLI JSONL wire format lives here, so schema drift only
has to be fixed in one place (AGENTS §8: keep harness quirks isolated to the
adapter layer). Mirrors :mod:`app.harness.claude_parser`'s design exactly:

* :func:`parse_codex_line` is a *pure* function — one raw decoded JSON object in,
  zero-or-more normalized :class:`StreamEvent`s out. Unknown / irrelevant objects
  return ``[]`` (never raise).
* It deliberately does NOT own the terminal :class:`RunDone` nor session-id
  capture. The adapter (:mod:`app.harness.codex`) tracks the thread id seen on the
  ``thread.started`` line via :func:`session_id_of` and emits the single terminal
  ``RunDone(session_id=...)`` after the stream ends.

Reality notes (grounded in a live ``codex exec --json`` capture + the schema
embedded in the codex-cli 0.130.0 binary, 2026-06):

Top-level events (the ``type`` field):

* ``thread.started`` — carries ``thread_id`` (the resume/session id). -> []
* ``turn.started`` — no payload of interest. -> []
* ``item.started`` / ``item.updated`` — interim item state; ignored so we don't
  double-emit (the matching ``item.completed`` carries the final output). -> []
* ``item.completed`` — carries ``item`` whose ``type`` is one of:
  ``agent_message`` (-> TextDelta), ``reasoning`` (-> ThinkingDelta),
  ``command_execution`` (-> ToolUse + ToolResult), ``mcp_tool_call``
  (-> ToolUse + ToolResult), ``error`` (-> errored ToolResult). Other item types
  (``file_change``, ``web_search``, ``todo_list``, ...) -> [].
* ``turn.completed`` — carries ``usage`` (-> Usage).
* ``turn.failed`` — carries an ``error`` (-> errored ToolResult so the failure
  text is visible; the adapter still surfaces the typed error from the nonzero
  exit / stderr, keeping "one terminal event owned by the adapter").

Token usage fields (from ``turn.completed.usage`` / ``TokenUsage``):
``input_tokens``, ``cached_input_tokens``, ``output_tokens``,
``reasoning_output_tokens`` (and sometimes ``total_tokens``). There is no single
"context window" field, so ``context_tokens`` is derived as
input + cached + output + reasoning (a cache-inclusive proxy, matching the Claude
parser's approach), falling back to input + output, then ``None``.

TODO(OQ-3): the ``reasoning`` item's exact text field (``text`` vs
``summary_text``), the ``error`` item / ``turn.failed`` error shape, and the
``mcp_tool_call`` result field are best-effort from the live capture + binary
string inspection and should be re-verified against a fuller live capture
(reasoning summaries + an MCP tool call + a forced turn failure).
"""

from __future__ import annotations

from app.domain.events import (
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    Usage,
)


def session_id_of(obj: dict[str, object]) -> str | None:
    """Return the Codex thread/session id carried by a raw line, if any.

    Pure helper used by the adapter to track the session across lines without
    duplicating schema knowledge outside this module. Codex surfaces the id once,
    on the ``thread.started`` line, as ``thread_id``.
    """
    if obj.get("type") == "thread.started":
        tid = obj.get("thread_id")
        return tid if isinstance(tid, str) and tid else None
    return None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reasoning_text(item: dict[str, object]) -> str:
    """Pull reasoning text, tolerating ``text`` or ``summary_text`` (OQ-3)."""
    for key in ("text", "summary_text"):
        text = item.get(key)
        if isinstance(text, str) and text:
            return text
    return ""


def _parse_command_execution(item: dict[str, object]) -> list[StreamEvent]:
    tool_id = item.get("id")
    tid = tool_id if isinstance(tool_id, str) else None
    command = _str(item.get("command"))
    output = _str(item.get("aggregated_output"))
    status = _str(item.get("status"))
    exit_code = item.get("exit_code")
    is_error = status == "failed" or (isinstance(exit_code, int) and exit_code != 0)
    tool_input: dict[str, object] = {"command": command}
    if isinstance(exit_code, int):
        tool_input["exit_code"] = exit_code
    return [
        ToolUse(name="command_execution", input=tool_input, tool_id=tid),
        ToolResult(tool_id=tid, content=output, is_error=is_error),
    ]


def _parse_mcp_tool_call(item: dict[str, object]) -> list[StreamEvent]:
    tool_id = item.get("id")
    tid = tool_id if isinstance(tool_id, str) else None
    server = _str(item.get("server"))
    tool = _str(item.get("tool"))
    name = f"{server}.{tool}" if server and tool else (tool or server or "mcp_tool_call")
    status = _str(item.get("status"))
    is_error = status == "failed"
    # The result field name varies; surface whichever is present as text.
    result = item.get("result")
    if result is None:
        result = item.get("structured_content")
    content = result if isinstance(result, str) else ("" if result is None else str(result))
    args = item.get("arguments")
    tool_input: dict[str, object] = {"arguments": args} if isinstance(args, dict) else {}
    return [
        ToolUse(name=name, input=tool_input, tool_id=tid),
        ToolResult(tool_id=tid, content=content, is_error=is_error),
    ]


def _parse_item(item: dict[str, object]) -> list[StreamEvent]:
    itype = item.get("type")
    if itype == "agent_message":
        return [TextDelta(text=_str(item.get("text")))]
    if itype == "reasoning":
        text = _reasoning_text(item)
        return [ThinkingDelta(text=text)] if text else []
    if itype == "command_execution":
        return _parse_command_execution(item)
    if itype == "mcp_tool_call":
        return _parse_mcp_tool_call(item)
    if itype == "error":
        return [ToolResult(content=_str(item.get("message")), is_error=True)]
    # file_change, web_search, todo_list, image_generation, ... -> ignore.
    return []


def _parse_usage(obj: dict[str, object]) -> list[StreamEvent]:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return [Usage()]
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cached = usage.get("cached_input_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    context_tokens: int | None
    if cached is not None or reasoning is not None:
        # Cache-inclusive proxy for "tokens in the context window".
        context_tokens = input_tokens + _int(cached) + output_tokens + _int(reasoning)
    elif input_tokens or output_tokens:
        context_tokens = input_tokens + output_tokens
    else:
        context_tokens = None
    return [
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_tokens=context_tokens,
        )
    ]


def parse_codex_line(obj: dict[str, object]) -> list[StreamEvent]:
    """Map ONE raw ``codex exec --json`` object to zero-or-more StreamEvents.

    Unknown or irrelevant objects (``thread.started``, ``turn.started``,
    ``item.started``/``item.updated``, unrecognized item/top-level types,
    malformed shapes) return ``[]`` — never raise.
    """
    mtype = obj.get("type")
    if mtype == "item.completed":
        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        return _parse_item(item)
    if mtype == "turn.completed":
        return _parse_usage(obj)
    if mtype == "turn.failed":
        error = obj.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return [ToolResult(content=_str(message) or "turn failed", is_error=True)]
    # thread.started, turn.started, item.started/updated, anything unknown -> [].
    return []
