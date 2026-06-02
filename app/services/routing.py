"""Pure routing: resolve which personas a message is addressed to.

Parses ``@handle`` and ``@everyone`` mentions (via the shared
:func:`app.services.mentions.find_mentions` helper) and maps them onto the
personas present in the room. No I/O.

Rules (FR-M2 / FR-M3)
---------------------
- No mentions -> ``[]``: the message is recorded as context only; no run starts.
- Unknown handles are ignored (not errors).
- ``@everyone`` expands to all room personas in **room order**.
- Specific mentions resolve in **mention order**.
- The result is de-duplicated, keeping each persona's first occurrence (so a
  persona named twice, or via both ``@handle`` and ``@everyone``, appears once).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.models import Persona
from app.services.mentions import find_mentions


def resolve_targets(text: str, room_personas: Sequence[Persona]) -> list[Persona]:
    """Resolve the personas addressed by ``text`` within ``room_personas``."""
    mentions = find_mentions(text)
    if not mentions:
        return []

    by_handle = {p.handle: p for p in room_personas}

    targets: list[Persona] = []
    seen: set[str] = set()

    def _add(persona: Persona) -> None:
        if persona.handle not in seen:
            seen.add(persona.handle)
            targets.append(persona)

    for mention in mentions:
        if mention == "everyone":
            for persona in room_personas:
                _add(persona)
        elif mention in by_handle:
            _add(by_handle[mention])
        # unknown handle: ignored

    return targets
