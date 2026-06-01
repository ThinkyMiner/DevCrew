"""Room message listing (chronological, paginated).

Pagination approach: the repo exposes only ``list_for_room`` (full chronological
list), so we page in the route with ``offset``/``limit`` query params applied to
that ordered list. This is simple and correct for a local single-user app's
transcript sizes; if transcripts grow large this is the seam to push paging into
the repo/SQL. ``limit`` omitted -> return all from ``offset`` onward.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_message_repo, get_room_service
from app.domain.models import Message
from app.persistence.repositories import MessageRepo
from app.services.rooms import RoomService

router = APIRouter(prefix="/rooms", tags=["messages"])

RepoDep = Annotated[MessageRepo, Depends(get_message_repo)]
RoomDep = Annotated[RoomService, Depends(get_room_service)]


@router.get("/{room_id}/messages")
def list_messages(
    room_id: str,
    repo: RepoDep,
    rooms: RoomDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> list[Message]:
    rooms.get(room_id)  # 404 if room missing
    messages = repo.list_for_room(room_id)
    if limit is None:
        return messages[offset:]
    return messages[offset : offset + limit]
