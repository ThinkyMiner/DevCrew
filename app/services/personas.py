"""PersonaService — a thin, validating facade over :class:`PersonaRepo`.

Adds VALUE on top of the repo (it does NOT re-implement CRUD storage):

* ``duplicate`` (FR-P1): clone a persona with a fresh id and a non-colliding
  handle/name so the new row does not shadow the original. Handle policy:
  append ``-copy`` to the source handle, and if that already exists keep
  appending ``-copy`` (``alpha`` -> ``alpha-copy`` -> ``alpha-copy-copy``) until
  the handle is unique among all existing personas. The name gets a
  ``" (copy)"`` suffix. Sessions are NEVER copied — a clone is a brand-new
  persona that starts its own per-room sessions on first use.
* ``update`` only updates the persona row. Per FR-P1, editing a persona must NOT
  destroy its sessions: sessions live in ``SessionRepo`` keyed by
  ``(room, persona)`` and are completely untouched here.
* Templates: ``list_templates`` and ``create_from_template`` (clone a template
  into a normal, usable persona — i.e. ``is_template=False``).
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.errors import TeamError
from app.domain.models import Persona
from app.persistence.repositories import PersonaRepo


class PersonaService:
    def __init__(self, persona_repo: PersonaRepo) -> None:
        self._repo = persona_repo

    # -- plain CRUD passthrough (typed) ---------------------------------------

    def create(self, persona: Persona) -> Persona:
        return self._repo.create(persona)

    def get(self, persona_id: str) -> Persona:
        """Return the persona or raise — callers want a value, not ``None``."""
        persona = self._repo.get(persona_id)
        if persona is None:
            raise TeamError(f"persona not found: {persona_id}")
        return persona

    def list(self) -> builtins.list[Persona]:
        return self._repo.list()

    def update(self, persona: Persona) -> Persona:
        """Update the persona row only. Per-room sessions are left untouched."""
        return self._repo.update(persona)

    def delete(self, persona_id: str) -> None:
        self._repo.delete(persona_id)

    # -- value-adds -----------------------------------------------------------

    def list_templates(self) -> builtins.list[Persona]:
        return [p for p in self._repo.list() if p.is_template]

    def duplicate(self, persona_id: str) -> Persona:
        """Clone ``persona_id`` into a new persona with a unique handle/name.

        The clone is always a normal persona (``is_template=False``) regardless
        of the source; use a template directly to keep it a template.
        """
        source = self.get(persona_id)
        return self._clone(source, is_template=False)

    def create_from_template(self, template_id: str) -> Persona:
        """Instantiate a normal persona from a template (FR-P1 templates)."""
        template = self.get(template_id)
        if not template.is_template:
            raise TeamError(f"persona {template_id} is not a template")
        return self._clone(template, is_template=False)

    # -- internals ------------------------------------------------------------

    def _clone(self, source: Persona, *, is_template: bool) -> Persona:
        handle = self._unique_handle(source.handle)
        clone = source.model_copy(
            update={
                "id": uuid4().hex,
                "name": f"{source.name} (copy)",
                "handle": handle,
                "is_template": is_template,
                "created_at": datetime.now(UTC),
            }
        )
        return self._repo.create(clone)

    def _unique_handle(self, base: str) -> str:
        existing = {p.handle for p in self._repo.list()}
        candidate = f"{base}-copy"
        while candidate in existing:
            candidate = f"{candidate}-copy"
        return candidate
