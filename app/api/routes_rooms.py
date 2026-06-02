"""Room CRUD + membership (FR-R1..R4). Thin: delegates to RoomService."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_room_service, get_session_store
from app.api.schemas import MemberAdd, RoomCreate, RoomUpdate
from app.domain.models import Persona, PersonaSession, Room
from app.services.rooms import RoomService
from app.services.session_store import SessionStore

router = APIRouter(prefix="/rooms", tags=["rooms"])

ServiceDep = Annotated[RoomService, Depends(get_room_service)]
SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]


@router.post("", status_code=201)
def create_room(body: RoomCreate, service: ServiceDep) -> Room:
    return service.create(Room(**body.model_dump()))


@router.get("")
def list_rooms(service: ServiceDep) -> list[Room]:
    return service.list()


@router.get("/{room_id}")
def get_room(room_id: str, service: ServiceDep) -> Room:
    return service.get(room_id)


@router.patch("/{room_id}")
def update_room(room_id: str, body: RoomUpdate, service: ServiceDep) -> Room:
    existing = service.get(room_id)
    updated = existing.model_copy(update=body.model_dump(exclude_unset=True))
    return service.update(updated)


@router.delete("/{room_id}", status_code=204)
def delete_room(room_id: str, service: ServiceDep) -> None:
    service.get(room_id)  # 404 if missing
    service.delete(room_id)


# -- membership -----------------------------------------------------------


@router.get("/{room_id}/members")
def list_members(room_id: str, service: ServiceDep) -> list[Persona]:
    service.get(room_id)  # 404 if room missing
    return service.list_member_personas(room_id)


@router.post("/{room_id}/members", status_code=201)
def add_member(room_id: str, body: MemberAdd, service: ServiceDep) -> list[Persona]:
    service.get(room_id)  # 404 if room missing
    service.add_member(room_id, body.persona_id)
    return service.list_member_personas(room_id)


@router.delete("/{room_id}/members/{persona_id}", status_code=204)
def remove_member(room_id: str, persona_id: str, service: ServiceDep) -> None:
    service.get(room_id)  # 404 if room missing
    service.remove_member(room_id, persona_id)


# -- session reset (FR-C4) ------------------------------------------------


@router.post("/{room_id}/personas/{persona_id}/reset-session")
def reset_session(
    room_id: str,
    persona_id: str,
    service: ServiceDep,
    sessions: SessionStoreDep,
) -> PersonaSession:
    """Clear a persona's harness session in this room (FR-C4).

    Thin delegate to :meth:`SessionStore.reset_session`: the next turn finds no
    resume id and starts a fresh harness session. The UI owns the confirmation
    prompt before calling this.
    """
    service.get(room_id)  # 404 if room missing
    return sessions.reset_session(room_id, persona_id)
