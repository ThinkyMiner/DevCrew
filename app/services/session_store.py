"""SessionStore — live-run tracking + the session-reset building block (FR-C4).

Two small, well-scoped responsibilities:

* ``reset_session`` (FR-C4): clear a persona's harness session in a room by
  upserting a FRESH :class:`PersonaSession` with ``harness_session_id=None``,
  ``last_seen_message_id=None``, and ``status="idle"``. The next turn then finds
  no resume id and starts a brand-new harness session — exactly the "reset"
  semantics. The provider is preserved (from the persona, falling back to any
  existing session row). This is the primitive; the UI layer is responsible for
  the confirmation prompt before calling it.

* In-flight run tracking: a minimal registry of active ``run_id``s per
  ``(room, persona)``, guarded by a lock so the later async layer can mark a run
  active/done and query what is in flight. Deliberately tiny — no cancellation
  machinery here (the orchestrator owns generator lifecycle); this only answers
  "is anything running for this persona right now?".
"""

from __future__ import annotations

import threading

from app.domain.errors import TeamError
from app.domain.models import PersonaSession, Provider
from app.persistence.repositories import PersonaRepo, SessionRepo


class SessionStore:
    def __init__(self, session_repo: SessionRepo, persona_repo: PersonaRepo) -> None:
        self._sessions = session_repo
        self._personas = persona_repo
        self._lock = threading.Lock()
        # (room_id, persona_id) -> set of active run_ids
        self._active: dict[tuple[str, str], set[str]] = {}

    # -- reset (FR-C4) --------------------------------------------------------

    def reset_session(self, room_id: str, persona_id: str) -> PersonaSession:
        """Clear the persona's harness session in this room; return the fresh row."""
        provider = self._resolve_provider(room_id, persona_id)
        fresh = PersonaSession(
            room_id=room_id,
            persona_id=persona_id,
            provider=provider,
            harness_session_id=None,
            last_seen_message_id=None,
            status="idle",
        )
        return self._sessions.upsert(fresh)

    def _resolve_provider(self, room_id: str, persona_id: str) -> Provider:
        persona = self._personas.get(persona_id)
        if persona is not None:
            return persona.provider
        existing = self._sessions.get(room_id, persona_id)
        if existing is not None:
            return existing.provider
        raise TeamError(f"cannot reset session: unknown persona {persona_id}")

    # -- in-flight run tracking (minimal, thread-safe) ------------------------

    def mark_active(self, room_id: str, persona_id: str, run_id: str) -> None:
        with self._lock:
            self._active.setdefault((room_id, persona_id), set()).add(run_id)

    def mark_done(self, room_id: str, persona_id: str, run_id: str) -> None:
        with self._lock:
            key = (room_id, persona_id)
            runs = self._active.get(key)
            if runs is not None:
                runs.discard(run_id)
                if not runs:
                    del self._active[key]

    def active_runs(self, room_id: str, persona_id: str) -> set[str]:
        with self._lock:
            return set(self._active.get((room_id, persona_id), set()))

    def is_busy(self, room_id: str, persona_id: str) -> bool:
        return bool(self.active_runs(room_id, persona_id))
