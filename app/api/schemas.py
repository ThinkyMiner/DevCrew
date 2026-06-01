"""Request bodies for the REST API.

Responses reuse the domain models directly (Persona/HumanAuthor/Room/Message/
RunRecord) — they are already Pydantic and safe to serialize. For *writes* we
define narrow Create/Update bodies so clients cannot set server-generated fields
(``id``, ``created_at``). Updates use all-optional fields for PATCH semantics
(only provided fields are applied).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.models import PermissionMode, Provider, ReplyMode


class PersonaCreate(BaseModel):
    name: str
    handle: str
    provider: Provider
    model: str
    color: str = "#6aa0ff"
    effort: str | None = None
    system_prompt: str = ""
    mcp_servers: list[str] = []
    allowed_tools: list[str] = []
    working_dir: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    is_template: bool = False


class PersonaUpdate(BaseModel):
    name: str | None = None
    handle: str | None = None
    provider: Provider | None = None
    model: str | None = None
    color: str | None = None
    effort: str | None = None
    system_prompt: str | None = None
    mcp_servers: list[str] | None = None
    allowed_tools: list[str] | None = None
    working_dir: str | None = None
    permission_mode: PermissionMode | None = None
    is_template: bool | None = None


class AuthorCreate(BaseModel):
    name: str
    color: str = "#9ee37d"
    weight_note: str = ""
    weight_enabled: bool = True


class AuthorUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    weight_note: str | None = None
    weight_enabled: bool | None = None


class RoomCreate(BaseModel):
    name: str
    topic: str = ""
    default_reply_mode: ReplyMode = ReplyMode.SEQUENTIAL


class RoomUpdate(BaseModel):
    name: str | None = None
    topic: str | None = None
    default_reply_mode: ReplyMode | None = None
    archived: bool | None = None


class MemberAdd(BaseModel):
    persona_id: str
