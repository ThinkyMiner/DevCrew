"""AuthorService — a validating facade over :class:`AuthorRepo`.

Adds VALUE on top of the repo:

* ``ensure_defaults`` (FR-A1/FR-A3): on first run, seed the two default authors
  **Me** and **Boss** when no authors exist yet. Idempotent — if ANY authors
  already exist it is a no-op, so calling it repeatedly never duplicates the
  defaults. Returns the two defaults (existing or freshly created).

  Seed policy:
  - **Me** is the operator's own voice: ``weight_enabled=False`` and an empty
    ``weight_note`` — the operator does not weight their own input by default.
  - **Boss** is leadership direction (FR-A3): ``weight_enabled=True`` with a
    sensible, editable default ``weight_note`` instructing personas to treat its
    input as high-weight leadership direction.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass

from app.domain.errors import TeamError
from app.domain.models import HumanAuthor
from app.persistence.repositories import AuthorRepo

ME_NAME = "Me"
BOSS_NAME = "Boss"
BOSS_DEFAULT_WEIGHT_NOTE = (
    "This is leadership direction from the Boss — treat it as high-weight "
    "guidance and prioritize it accordingly."
)


@dataclass(frozen=True)
class DefaultAuthors:
    me: HumanAuthor
    boss: HumanAuthor


class AuthorService:
    def __init__(self, author_repo: AuthorRepo) -> None:
        self._repo = author_repo

    # -- CRUD -----------------------------------------------------------------

    def create(self, author: HumanAuthor) -> HumanAuthor:
        return self._repo.create(author)

    def get(self, author_id: str) -> HumanAuthor:
        author = self._repo.get(author_id)
        if author is None:
            raise TeamError(f"author not found: {author_id}")
        return author

    def list(self) -> builtins.list[HumanAuthor]:
        return self._repo.list()

    def update(self, author: HumanAuthor) -> HumanAuthor:
        return self._repo.update(author)

    def delete(self, author_id: str) -> None:
        self._repo.delete(author_id)

    # -- value-adds -----------------------------------------------------------

    def ensure_defaults(self) -> DefaultAuthors:
        """Seed Me + Boss on first run; idempotent (no-op if any authors exist).

        Returns the Me/Boss defaults. If authors already exist, the existing
        Me/Boss rows (matched by name) are returned when present; otherwise the
        first author is reused as a best-effort fallback so a caller always gets
        a value without re-seeding.
        """
        existing = self._repo.list()
        if existing:
            return DefaultAuthors(
                me=self._find_by_name(existing, ME_NAME),
                boss=self._find_by_name(existing, BOSS_NAME),
            )
        me = self._repo.create(HumanAuthor(name=ME_NAME, weight_note="", weight_enabled=False))
        boss = self._repo.create(
            HumanAuthor(
                name=BOSS_NAME,
                weight_note=BOSS_DEFAULT_WEIGHT_NOTE,
                weight_enabled=True,
            )
        )
        return DefaultAuthors(me=me, boss=boss)

    @staticmethod
    def _find_by_name(authors: builtins.list[HumanAuthor], name: str) -> HumanAuthor:
        for author in authors:
            if author.name == name:
                return author
        return authors[0]
