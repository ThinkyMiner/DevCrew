"""Single source of truth for the ``claude --output-format stream-json`` schema.

ALL knowledge of the Claude Code stream-json wire format lives here, so schema
drift only has to be fixed in one place (AGENTS §8: keep harness quirks isolated
to the adapter layer).

Design decision — session_id + RunDone
--------------------------------------
``parse_claude_line`` is a *pure* function: one raw decoded JSON object in,
zero-or-more normalized :class:`StreamEvent`s out. It deliberately does NOT own
the terminal :class:`RunDone` nor session-id capture, because a single-line
parser cannot both stay pure and track cross-line state cleanly.

Instead:

* the parser surfaces the session id when a line carries one, via
  :func:`session_id_of` (a separate pure helper), and
* the *adapter* (:mod:`app.harness.claude`) tracks the session id seen across
  ``init``/``assistant``/``result`` lines and emits the single terminal
  ``RunDone(session_id=...)`` after the stream ends.

The ``result`` line maps to a :class:`Usage` event only — never ``RunDone`` —
keeping the "one terminal event, owned by the adapter" rule unambiguous.

Reality notes (from a live ``claude --print --output-format stream-json
--verbose`` capture, 2026-06):

* ``system`` lines come in several subtypes (``init``, ``hook_started``,
  ``hook_response`` …). Only ``init`` is interesting; every other ``system``
  line — and any unknown ``type`` — maps to ``[]`` (ignored, never crashes).
* The ``result`` line's ``usage`` has ``input_tokens`` and ``output_tokens``
  plus ``cache_creation_input_tokens`` / ``cache_read_input_tokens``. There is
  no single cumulative "context window" field, so ``context_tokens`` is derived
  as the sum of input + cache-read + cache-creation + output when any cache
  field is present (a reasonable proxy for "tokens in the context window"),
  else ``None``.
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
    """Return the ``session_id`` carried by a raw line, if any.

    Pure helper used by the adapter to track the session across lines without
    duplicating schema knowledge outside this module.
    """
    sid = obj.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _as_text(value: object) -> str:
    """Coerce a ``content`` value (string or list of text blocks) to plain text.

    ``tool_result`` content may be a bare string or a list of
    ``{"type": "text", "text": "..."}`` blocks; join the text of the latter.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _parse_assistant(obj: dict[str, object]) -> list[StreamEvent]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    events: list[StreamEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                events.append(TextDelta(text=text))
        elif btype == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                events.append(ThinkingDelta(text=thinking))
        elif btype == "tool_use":
            name = block.get("name")
            tool_input = block.get("input")
            tool_id = block.get("id")
            events.append(
                ToolUse(
                    name=name if isinstance(name, str) else "",
                    input=tool_input if isinstance(tool_input, dict) else {},
                    tool_id=tool_id if isinstance(tool_id, str) else None,
                )
            )
    return events


def _parse_user(obj: dict[str, object]) -> list[StreamEvent]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    events: list[StreamEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_id = block.get("tool_use_id")
        events.append(
            ToolResult(
                tool_id=tool_id if isinstance(tool_id, str) else None,
                content=_as_text(block.get("content")),
                is_error=bool(block.get("is_error", False)),
            )
        )
    return events


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_result(obj: dict[str, object]) -> list[StreamEvent]:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return [Usage()]
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    context_tokens: int | None = None
    if cache_read is not None or cache_creation is not None:
        context_tokens = input_tokens + _int(cache_read) + _int(cache_creation) + output_tokens
    return [
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_tokens=context_tokens,
        )
    ]


def parse_claude_line(obj: dict[str, object]) -> list[StreamEvent]:
    """Map ONE raw stream-json object to zero-or-more normalized StreamEvents.

    Unknown or irrelevant objects (other ``system`` subtypes, ``stream_event``,
    unrecognized ``type`` values, malformed shapes) return ``[]`` — never raise.
    """
    msg_type = obj.get("type")
    if msg_type == "assistant":
        return _parse_assistant(obj)
    if msg_type == "user":
        return _parse_user(obj)
    if msg_type == "result":
        return _parse_result(obj)
    # ``system`` (init and others), ``stream_event``, anything unknown -> ignore.
    return []
