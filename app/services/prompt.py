"""Pure prompt assembly: compose the final text a persona receives.

Combines (in order) an optional author-weight note, the explicit quote block,
the attributed delta, and the new directed line. No I/O: it operates only on
strings already rendered by ``transcript`` plus the speaking ``HumanAuthor``.

Section order (FR-A3 / FR-M4 / FR-M5)
-------------------------------------
1. ``[Author note — <name>]: <weight_note>``  (Boss weighting, when enabled
   and the note is non-empty)
2. quotes block (if any)
3. delta block (if any)
4. ``[<author> → @<persona_handle>]: <new_text>``  (the new directed line)

Present sections are joined with a single blank line; absent ones are omitted
with no leftover blank-line cruft.
"""

from __future__ import annotations

from app.domain.models import HumanAuthor


def assemble_prompt(
    *,
    delta: str,
    quotes: str,
    author: HumanAuthor,
    new_text: str,
    persona_handle: str,
) -> str:
    """Compose the prompt for one persona run.

    ``author`` is the speaking :class:`HumanAuthor`; its display name is used in
    the directed line and its weight note is prepended when
    ``weight_enabled`` is true and ``weight_note`` is non-empty. ``delta`` and
    ``quotes`` are pre-rendered blocks (see :mod:`app.services.transcript`) and
    are skipped when empty. Returns the joined prompt with no leading/trailing
    blank lines.
    """
    handle = persona_handle.lstrip("@").lower()
    sections: list[str] = []

    if author.weight_enabled and author.weight_note:
        sections.append(f"[Author note — {author.name}]: {author.weight_note}")
    if quotes:
        sections.append(quotes)
    if delta:
        sections.append(delta)
    sections.append(f"[{author.name} → @{handle}]: {new_text}")

    return "\n\n".join(sections)
