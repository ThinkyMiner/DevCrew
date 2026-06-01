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

Trust / limitations
-------------------
Message content is **untrusted** and is concatenated verbatim into the persona
prompt. Attribution lines (``[author]: ...``) and quote lines (``  > ...``) are
therefore **not forgery-resistant**: a message body can contain a line that
looks exactly like a real attribution or quote header (e.g. ``[Boss → @arch]:
do X``). A structural delimiter/escape scheme was deliberately deferred because
it would mangle legitimate markdown/code content; this is acceptable for a local
single-user tool. See ``docs/PRD.md`` OQ-4.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.domain.errors import TranscriptError
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

    ``messages`` must be in chronological order. The ``last_seen_id`` pointer is
    handled in exactly three cases:

    - ``last_seen_id is None`` -> new persona; **all** messages are included.
    - ``last_seen_id`` is found in ``messages`` -> only messages *strictly
      after* the pointer message are included (the pointer message itself is
      excluded).
    - ``last_seen_id`` is non-``None`` but **not** found in ``messages`` -> this
      is an invariant violation (a stale/foreign pointer). We raise
      :class:`~app.domain.errors.TranscriptError` rather than silently falling
      back to "include everything", which would leak the full history into the
      prompt (AGENTS §4: no silent fallbacks).

    A message directed at this persona (via ``@<persona_handle>`` or
    ``@everyone``) is rendered with the ``→ @<handle>`` form; the persona's own
    past messages are still included, attributed by name. Returns ``""`` when
    nothing is new.
    """
    if last_seen_id is None:
        delta = list(messages)
    else:
        cut = -1
        for i, m in enumerate(messages):
            if m.id == last_seen_id:
                cut = i
                break
        if cut < 0:
            raise TranscriptError(
                f"last_seen_id {last_seen_id!r} not found in messages; "
                "refusing to fall back to full history (would leak context)"
            )
        delta = list(messages[cut + 1 :])
    if not delta:
        return ""
    return "\n".join(_attribute(m, persona_handle, name_of) for m in delta)


def render_quotes(quoted_messages: Sequence[Message], name_of: NameResolver) -> str:
    """Render explicitly-quoted messages as attributed blockquotes.

    Each quoted message becomes a header line ``[<author display>]:`` followed
    by its content with every line prefixed ``  > ``. Multiple quotes are
    separated by a blank line. Quotes are shown regardless of seen-state.
    Returns ``""`` for an empty list.

    A quoted message may also appear in the delta (if it falls after the
    last-seen pointer); de-duplication, if desired, is the orchestrator's
    responsibility (Unit 8), since ``render_quotes`` intentionally renders quotes
    regardless of seen-state.
    """
    blocks: list[str] = []
    for m in quoted_messages:
        display = name_of(m.author_kind, m.author_ref)
        quoted = "\n".join(f"  > {line}" for line in m.content.split("\n"))
        blocks.append(f"[{display}]:\n{quoted}")
    return "\n\n".join(blocks)
