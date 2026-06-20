# Keep repository CRUD explicit; no metaprogrammed BaseRepo

**Status:** accepted

The 6 repositories in `app/persistence/repositories.py` (Persona/Author/Room/Message/Session/Run) hand-roll the same CRUD shape, and `get`/`list`/`delete` are byte-identical across several of them. The obvious refactor is a generic `BaseRepo[T]` driven by declarative `{column: serializer}` maps so every method — including `create`/`update`/`_row_to_model` — becomes generic. We deliberately reject that.

We optimize this codebase for **AI-navigability** over raw line count: an agent should be able to read one repository end-to-end and see the actual `INSERT`/`UPDATE` SQL, the `json.dumps`/enum-`.value`/`_dt` serialization, and the field mapping in one place — not reconstruct them from a metaprogramming layer. Full consolidation trades that visibility for locality we don't need (the schema is low-churn).

## Decision

- **Allowed:** a light `BaseRepo[T]` that owns only the byte-identical mechanical methods — `get`, `list`, `delete` — parameterized by `table`, `order_by`, and a per-repo `_row_to_model`.
- **Kept explicit per repo:** `create`, `update`, and `_row_to_model` — the verbose-but-obvious SQL and serialization stay visible in each repository.

## Considered options

- **Full `BaseRepo[T]` with declarative column maps** — rejected: hides the INSERT/UPDATE logic behind reflection; a single repo can no longer be read end-to-end.
- **Leave all 6 fully hand-rolled** — viable, maximally explicit, but the `get`/`list`/`delete` triplet is mindless duplication with no readability value, so the light base is a net win.

## Why this is recorded

A future architecture review will see the repetition and re-suggest the full base class. This ADR records that the duplication in `create`/`update` is intentional — explicitness there is the feature, not an oversight.
