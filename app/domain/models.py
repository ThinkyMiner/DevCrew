from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    MOCK = "mock"


class PermissionMode(StrEnum):
    READ_ONLY = "read-only"
    ASK = "ask"
    AUTO = "auto"


class ReplyMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class AuthorKind(StrEnum):
    HUMAN = "human"
    PERSONA = "persona"


_HANDLE_RE = re.compile(r"^[a-z0-9_-]+$")


class Persona(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=_id)
    name: str
    handle: str
    color: str = "#6aa0ff"
    # Short, human-facing role label (e.g. "System architect"). Shown next to the
    # persona name in chat and the manager so you still know what a persona does
    # after renaming it to a real person's name. Distinct from the system prompt.
    job: str = ""
    provider: Provider
    model: str
    # maps to claude --effort / codex reasoning effort. The claude CLI flag is
    # verified to exist (`claude --effort <level>`, accepts low/medium/high/xhigh/max)
    # as of claude-cli 2.1.159 — see app/harness/claude.py near the --effort argv.
    effort: str | None = None
    system_prompt: str = ""
    mcp_servers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    is_template: bool = False
    created_at: datetime = Field(default_factory=_now)

    @field_validator("handle")
    @classmethod
    def _norm_handle(cls, v: str) -> str:
        v = v.lstrip("@").strip().lower()
        if not _HANDLE_RE.match(v):
            raise ValueError("handle must match [a-z0-9_-]+ (no spaces)")
        return v


class HumanAuthor(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=_id)
    name: str
    color: str = "#9ee37d"
    weight_note: str = ""
    weight_enabled: bool = True


class Room(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=_id)
    name: str
    topic: str = ""
    default_reply_mode: ReplyMode = ReplyMode.SEQUENTIAL
    # When true, a persona's reply that @-mentions another persona delegates a turn
    # to that persona (auto-adding them to the room if needed), bounded by the
    # orchestrator's depth/run caps. The per-room off switch.
    delegation_enabled: bool = True
    archived: bool = False
    created_at: datetime = Field(default_factory=_now)


class Message(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=_id)
    room_id: str
    author_kind: AuthorKind
    author_ref: str  # human_author.id or persona.id
    content: str
    quoted_message_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_now)

    @field_validator("quoted_message_ids")
    @classmethod
    def _dedupe_quoted(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class PersonaSession(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    room_id: str
    persona_id: str
    provider: Provider
    harness_session_id: str | None = None
    last_seen_message_id: str | None = None
    status: str = "idle"


class RunRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    run_id: str = Field(default_factory=_id)
    room_id: str
    persona_id: str
    command_redacted: str
    exit_code: int | None = None
    usage: dict[str, object] = Field(default_factory=dict)
    log_path: str | None = None
    error_kind: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
