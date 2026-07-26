"""One-time persona cleanup for an existing ``data/team.db``.

WHAT it does (all inside one transaction, after a file backup):

1. **Dedupe / merge the ``-copy`` personas.** The old flow (before the team was
   seeded as non-templates) forced operators to *duplicate* a template to get a
   usable, addable persona, so databases accumulated ``architect-copy-copy-...``
   rows. For each ``*-copy*`` persona we find its original (the base handle with
   every ``-copy`` suffix stripped) and RE-POINT every reference to it —
   messages, room membership, sessions, run records — then delete the copy. A
   copy with no messages is simply removed; one with real history is *merged*, so
   no transcript is orphaned.

2. **Rename the team to human names** (``Architect`` -> ``Ada``, …) and clear
   ``is_template`` so they show up in the room "add member" picker. The existing
   ``system_prompt``, ``job``, ``color``, ``provider`` and ``model`` are
   PRESERVED — only the display name and the template flag change.

3. **Ensure the seeded team exists**, including the ``@systemd`` orchestrator.
   Any default persona whose handle is missing is inserted from
   :data:`app.services.persona_seed.DEFAULT_PERSONAS`.

Idempotent: re-running is a no-op (copies already gone, names already human,
handles already present). Run it with the app STOPPED:

    python -m scripts.migrate_personas          # migrate ./data/team.db
    python -m scripts.migrate_personas --dry-run
    python -m scripts.migrate_personas --db path/to/team.db
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.persistence.db import Database
from app.services.persona_seed import DEFAULT_PERSONAS

# Human-name map derived from the canonical seed, keyed by handle.
_HUMAN_NAME = {spec.handle: spec.name for spec in DEFAULT_PERSONAS}


@dataclass
class MigrationReport:
    removed_unused: int = 0  # copies deleted that had no messages
    merged: int = 0  # copies whose history was re-pointed to an original
    renamed: int = 0  # team personas given a human name
    seeded: int = 0  # default personas inserted (incl. @systemd)
    orphan_copies: list[str] = field(default_factory=list)  # copies with no original

    def summary(self) -> str:
        return (
            f"removed_unused={self.removed_unused} merged={self.merged} "
            f"renamed={self.renamed} seeded={self.seeded} "
            f"orphan_copies={self.orphan_copies}"
        )


def _base_handle(handle: str) -> str:
    """Strip every trailing ``-copy`` so ``architect-copy-copy`` -> ``architect``."""
    base = handle
    while base.endswith("-copy"):
        base = base[: -len("-copy")]
    return base


def migrate(db: Database) -> MigrationReport:
    report = MigrationReport()
    with db.transaction():
        _dedupe_copies(db, report)
        _rename_team(db, report)
        _seed_missing(db, report)
    return report


def _dedupe_copies(db: Database, report: MigrationReport) -> None:
    rows = db.query("SELECT id, handle FROM persona WHERE handle LIKE '%-copy%'")
    id_by_handle = {r["handle"]: r["id"] for r in db.query("SELECT id, handle FROM persona")}
    for row in rows:
        copy_id, handle = row["id"], row["handle"]
        original_id = id_by_handle.get(_base_handle(handle))
        if original_id is None or original_id == copy_id:
            # No surviving original to fold into — leave it alone and report it,
            # rather than silently deleting a persona with nowhere to merge.
            report.orphan_copies.append(handle)
            continue
        had_messages = bool(
            db.query(
                "SELECT 1 FROM message WHERE author_ref = ? AND author_kind = 'persona' LIMIT 1",
                (copy_id,),
            )
        )
        _repoint(db, copy_id, original_id)
        db.execute("DELETE FROM persona WHERE id = ?", (copy_id,))
        if had_messages:
            report.merged += 1
        else:
            report.removed_unused += 1


def _repoint(db: Database, copy_id: str, original_id: str) -> None:
    """Move every reference from ``copy_id`` to ``original_id`` before deletion."""
    # Messages + audit run records: straight re-point.
    db.execute(
        "UPDATE message SET author_ref = ? WHERE author_ref = ? AND author_kind = 'persona'",
        (original_id, copy_id),
    )
    db.execute(
        "UPDATE run_record SET persona_id = ? WHERE persona_id = ?",
        (original_id, copy_id),
    )
    # Membership + sessions are keyed by (room, persona): if the original is
    # already present in that room, drop the copy's row; otherwise re-point it.
    for table in ("room_persona", "persona_session"):
        for r in db.query(f"SELECT room_id FROM {table} WHERE persona_id = ?", (copy_id,)):
            room_id = r["room_id"]
            exists = db.query(
                f"SELECT 1 FROM {table} WHERE room_id = ? AND persona_id = ?",
                (room_id, original_id),
            )
            if exists:
                db.execute(
                    f"DELETE FROM {table} WHERE room_id = ? AND persona_id = ?",
                    (room_id, copy_id),
                )
            else:
                db.execute(
                    f"UPDATE {table} SET persona_id = ? WHERE room_id = ? AND persona_id = ?",
                    (original_id, room_id, copy_id),
                )


def _rename_team(db: Database, report: MigrationReport) -> None:
    for handle, human in _HUMAN_NAME.items():
        rows = db.query("SELECT name, is_template FROM persona WHERE handle = ?", (handle,))
        if not rows:
            continue
        current = rows[0]
        if current["name"] == human and not current["is_template"]:
            continue  # already migrated
        # Only touch the display name + template flag; preserve the rich prompt,
        # job, color, provider, model the operator already has.
        db.execute(
            "UPDATE persona SET name = ?, is_template = 0 WHERE handle = ?",
            (human, handle),
        )
        report.renamed += 1


def _seed_missing(db: Database, report: MigrationReport) -> None:
    present = {r["handle"] for r in db.query("SELECT handle FROM persona")}
    now = datetime.now(UTC).isoformat()
    for spec in DEFAULT_PERSONAS:
        if spec.handle in present:
            continue
        db.execute(
            "INSERT INTO persona (id, name, handle, color, job, provider, model, effort, "
            "system_prompt, mcp_servers, allowed_tools, working_dir, permission_mode, "
            "is_template, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                spec.name,
                spec.handle,
                spec.color,
                spec.job,
                spec.provider.value,
                spec.model,
                None,
                spec.system_prompt,
                "[]",
                "[]",
                None,
                "read-only",
                0,
                now,
            ),
        )
        report.seeded += 1


def refresh_seed_prompts(db: Database) -> int:
    """Re-stamp SEEDED handles' system prompts with the current canonical specs.

    Opt-in (``--refresh-prompts``): the regular migration deliberately preserves
    operator edits, but D14 changed the seeded prompts' @-mention semantics
    ("@ = request a turn", anti-rubber-stamp rules, dispatcher wrap-up duty) and
    an old prompt actively fights the deliberation scheduler. Only handles that
    exist in ``DEFAULT_PERSONAS`` are touched; operator-created personas never
    are. Returns the number of personas updated.
    """
    updated = 0
    with db.transaction():
        for spec in DEFAULT_PERSONAS:
            rows = db.query("SELECT system_prompt FROM persona WHERE handle = ?", (spec.handle,))
            if not rows or rows[0]["system_prompt"] == spec.system_prompt:
                continue
            db.execute(
                "UPDATE persona SET system_prompt = ? WHERE handle = ?",
                (spec.system_prompt, spec.handle),
            )
            updated += 1
    return updated


def _backup(db_path: Path) -> Path:
    """Copy the db (and any WAL/SHM sidecars) next to it with a timestamp suffix."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_suffix(db_path.suffix + f".bak.{stamp}")
    shutil.copy2(db_path, backup)
    for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
        p = Path(sidecar)
        if p.exists():
            shutil.copy2(p, f"{backup}{sidecar[len(str(db_path)) :]}")
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up personas in an existing team.db")
    parser.add_argument("--db", default="data/team.db", help="path to team.db")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, then roll back"
    )
    parser.add_argument(
        "--refresh-prompts",
        action="store_true",
        help="also overwrite SEEDED personas' system prompts with the current "
        "canonical specs (D14 @-semantics); operator-created personas untouched",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")

    if not args.dry_run:
        backup = _backup(db_path)
        print(f"[migrate] backed up {db_path} -> {backup}")

    db = Database(db_path)
    try:
        db.init_schema()
        if args.dry_run:
            # Run inside a transaction we deliberately abort so nothing persists.
            report = MigrationReport()
            try:
                with db.transaction():
                    _dedupe_copies(db, report)
                    _rename_team(db, report)
                    _seed_missing(db, report)
                    raise _Rollback()
            except _Rollback:
                pass
            print(f"[migrate] DRY-RUN (no changes written): {report.summary()}")
        else:
            report = migrate(db)
            print(f"[migrate] done: {report.summary()}")
            if args.refresh_prompts:
                n = refresh_seed_prompts(db)
                print(f"[migrate] refreshed seed prompts on {n} persona(s)")
    finally:
        db.close()


class _Rollback(Exception):
    """Internal sentinel to abort the dry-run transaction."""


if __name__ == "__main__":
    main()
