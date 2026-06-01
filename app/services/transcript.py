"""Pure transcript rendering: the attributed delta + explicit quote blocks.

This module turns persisted `Message` rows into the attributed text a persona
ingests. It performs **no I/O**: the caller injects a ``name_of`` resolver so
display-name lookup (which may touch the DB) stays outside this pure layer.

Attribution formats
--------------------
- Ordinary line:        ``[<author display>]: <content>``
- Directed at *this*
  persona:              ``[<author display> → @<handle>]: <content>``
- Quote block:          ``[<quoted author display>]:`` then each content line
                        prefixed with ``  > ``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.domain.models import AuthorKind, Message
from app.services.mentions import find_mentions

NameResolver = Callable[[AuthorKind, str], str]


def _is_directed_at(message: Message, persona_handle: str) -> bool:
    """True when ``message`` tags this persona's handle or ``@everyone``."""
    handle = persona_handle.lstrip("@").lower()
    mentions = find_mentions(message.content)
    return handle in mentions or "everyone" in mentions


def _attribute(message: Message, persona_handle: str, name_of: NameResolver) -> str:
    """Render a single message as one attributed transcript line."""
    display = name_of(message.author_kind, message.author_ref)
    if _is_directed_at(message, persona_handle):
        return f"[{display} → @{persona_handle.lstrip('@').lower()}]: {message.content}"
    return f"[{display}]: {message.content}"


def build_delta(
    messages: Sequence[Message],
    last_seen_id: str | None,
    *,
    persona_handle: str,
    name_of: NameResolver,
) -> str:
    """Render the attributed block of messages strictly after ``last_seen_id``.

    ``messages`` must be in chronological order. If ``last_seen_id`` is ``None``
    every message is included; otherwise only those *after* the message with
    that id (the pointer message itself is excluded). A message directed at this
    persona (via ``@<persona_handle>`` or ``@everyone``) is rendered with the
    ``→ @<handle>`` form; the persona's own past messages are still included,
    attributed by name. Returns ``""`` when nothing is new.
    """
    if last_seen_id is None:
        delta = list(messages)
    else:
        cut = -1
        for i, m in enumerate(messages):
            if m.id == last_seen_id:
                cut = i
                break
        delta = list(messages[cut + 1 :]) if cut >= 0 else list(messages)
    if not delta:
        return ""
    return "\n".join(_attribute(m, persona_handle, name_of) for m in delta)


def render_quotes(quoted_messages: Sequence[Message], name_of: NameResolver) -> str:
    """Render explicitly-quoted messages as attributed blockquotes.

    Each quoted message becomes a header line ``[<author display>]:`` followed
    by its content with every line prefixed ``  > ``. Multiple quotes are
    separated by a blank line. Quotes are shown regardless of seen-state.
    Returns ``""`` for an empty list.
    """
    blocks: list[str] = []
    for m in quoted_messages:
        display = name_of(m.author_kind, m.author_ref)
        quoted = "\n".join(f"  > {line}" for line in m.content.split("\n"))
        blocks.append(f"[{display}]:\n{quoted}")
    return "\n\n".join(blocks)
