"""Mention parsing — the single source of truth for `@handle` detection.

Shared by the transcript builder (to decide if a message is *directed at* a
persona) and by routing (to resolve which personas should run). Keeping one
implementation avoids the two ever drifting apart.
"""

from __future__ import annotations

import re

# A mention is an `@` that is not glued to the end of a preceding word (so
# emails like ``kartik@example.com`` are not mistaken for mentions), followed
# by a handle in the same charset the domain `Persona.handle` validator allows:
# ``[a-z0-9_-]+``. ``@everyone`` is included as an ordinary handle token.
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)")


def find_mentions(text: str) -> list[str]:
    """Return lowercased handles mentioned in ``text``, in order of appearance.

    Includes the literal ``"everyone"`` when ``@everyone`` is present. Raw
    occurrences are returned (duplicates preserved); de-duplication is left to
    the caller, which may need ordering rules of its own.
    """
    return [m.lower() for m in _MENTION_RE.findall(text)]
