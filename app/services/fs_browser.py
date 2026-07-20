"""Read-only OS directory browser backing the room working-dir folder picker.

This lists *directory names* on the machine running the server so the operator
can click to choose a room's shared ``working_dir`` instead of typing a path. It
is deliberately minimal and read-only: it never reads file contents, never
writes, and returns only sub-directories (plus the parent) of the requested
path. The app binds to loopback for a single local operator (PRD non-goals), so
exposing directory names over the local API is acceptable.

This is *not* the persistence store (that is the DB + run-log under data_dir);
it touches the filesystem only to enumerate folders for the picker. A missing or
non-directory path fails loud with :class:`~app.domain.errors.NotFound`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.domain.errors import NotFound


class DirEntry(BaseModel):
    name: str
    path: str


class DirListing(BaseModel):
    path: str  # the resolved absolute path being listed
    parent: str | None  # absolute parent path, or None at the filesystem root
    entries: list[DirEntry]  # immediate sub-directories, sorted case-insensitively


def list_dirs(path: str | None) -> DirListing:
    """List the immediate sub-directories of ``path`` (home dir when None).

    Expands ``~``, resolves the path, and confines results to directories. Raises
    :class:`NotFound` if the path does not exist or is not a directory.
    """
    base = (Path(path).expanduser() if path else Path.home()).resolve()
    if not base.is_dir():
        raise NotFound(f"not a directory: {base}")

    entries: list[DirEntry] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:  # unreadable directory — fail loud, located.
        raise NotFound(f"cannot read directory: {base} ({exc})") from exc
    for child in children:
        try:
            if child.is_dir():
                entries.append(DirEntry(name=child.name, path=str(child)))
        except OSError:
            # A child we can't stat (broken symlink, permission) is simply skipped;
            # the browser stays usable rather than erroring on one bad entry.
            continue

    parent = str(base.parent) if base.parent != base else None
    return DirListing(path=str(base), parent=parent, entries=entries)
