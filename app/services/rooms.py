"""RoomService — a validating facade over :class:`RoomRepo` (+ ``PersonaRepo``).

Adds VALUE on top of the repos:

* ``rename`` / ``archive`` — convenience mutators that load, mutate one field,
  and persist (so callers don't reconstruct the whole :class:`Room`).
* ``list_member_personas`` — resolves the room's ordered member persona-IDs
  (``RoomRepo.list_members`` returns id strings) into full :class:`Persona`
  objects via ``PersonaRepo``, PRESERVING membership order and SKIPPING ids that
  no longer resolve (a dangling membership row is tolerated, not fatal).

FR-R3 (per-room separation) is inherent: sessions are keyed by ``(room,
persona)``, so there is no cross-room leakage to enforce here.
"""

from __future__ import annotations

import builtins

from app.domain.errors import NotFound
from app.domain.models import Persona, Room
from app.persistence.repositories import PersonaRepo, RoomRepo


class RoomService:
    def __init__(self, room_repo: RoomRepo, persona_repo: PersonaRepo) -> None:
        self._rooms = room_repo
        self._personas = persona_repo

    # -- CRUD -----------------------------------------------------------------

    def create(self, room: Room) -> Room:
        return self._rooms.create(room)

    def get(self, room_id: str) -> Room:
        room = self._rooms.get(room_id)
        if room is None:
            raise NotFound(f"room not found: {room_id}")
        return room

    def list(self) -> builtins.list[Room]:
        return self._rooms.list()

    def update(self, room: Room) -> Room:
        """Persist edits to an existing room row (name/topic/reply-mode/archived).

        Generic counterpart to :meth:`rename` / :meth:`archive` so a single PATCH
        can edit any combination of fields uniformly. Raises if the room is
        unknown (consistent with the other facades), so a PATCH on a missing id
        becomes a not-found rather than a silent no-op.
        """
        self.get(room.id)
        return self._rooms.update(room)

    def delete(self, room_id: str) -> None:
        self._rooms.delete(room_id)

    # -- value-adds -----------------------------------------------------------

    def rename(self, room_id: str, name: str) -> Room:
        room = self.get(room_id)
        room.name = name
        return self._rooms.update(room)

    def archive(self, room_id: str, *, archived: bool = True) -> Room:
        room = self.get(room_id)
        room.archived = archived
        return self._rooms.update(room)

    # -- membership -----------------------------------------------------------

    def add_member(self, room_id: str, persona_id: str) -> None:
        self._rooms.add_member(room_id, persona_id)

    def remove_member(self, room_id: str, persona_id: str) -> None:
        self._rooms.remove_member(room_id, persona_id)

    def list_member_personas(self, room_id: str) -> builtins.list[Persona]:
        """Resolve ordered member ids to :class:`Persona`, skipping missing ones."""
        personas: list[Persona] = []
        for pid in self._rooms.list_members(room_id):
            persona = self._personas.get(pid)
            if persona is not None:
                personas.append(persona)
        return personas
