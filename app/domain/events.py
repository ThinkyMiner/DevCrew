from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class TextDelta(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class ThinkingDelta(BaseModel):
    kind: Literal["thinking"] = "thinking"
    text: str


class ToolUse(BaseModel):
    kind: Literal["tool_use"] = "tool_use"
    name: str
    input: dict[str, object] = Field(default_factory=dict)
    tool_id: str | None = None


class ToolResult(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    tool_id: str | None = None
    content: str = ""
    is_error: bool = False


class Usage(BaseModel):
    kind: Literal["usage"] = "usage"
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int | None = None  # powers the per-persona context indicator (FR-C3)


class RunError(BaseModel):
    kind: Literal["error"] = "error"
    error_kind: str
    message: str
    # Additive (Phase 5b): let a transport build an error card + log link without
    # changing post_message's (persona_id, event) tuple contract. Populated by the
    # orchestrator when it constructs the RunError; never carries secrets.
    run_id: str | None = None
    log_path: str | None = None


class RunDone(BaseModel):
    kind: Literal["done"] = "done"
    session_id: str | None = None  # harness session id to persist for resume
    # Additive (Phase 5b): the run this terminal event belongs to, so a transport
    # can correlate the end-of-turn with its RunRecord/log.
    run_id: str | None = None


StreamEvent = Annotated[
    TextDelta | ThinkingDelta | ToolUse | ToolResult | Usage | RunError | RunDone,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


def parse_event(data: dict[str, object]) -> StreamEvent:
    return _ADAPTER.validate_python(data)
