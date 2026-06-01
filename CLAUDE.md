# CLAUDE.md

**[`AGENTS.md`](AGENTS.md) is the single source of truth for this repository.**
Read it first and in full — it covers the architecture, the layering and dependency
rules, the non-negotiable engineering rules, the mandatory TDD workflow, the
commands, and the error-finding playbook. Everything there applies to you.

This file adds only the Claude-Code-specific notes that don't belong in the shared
guide.

## Orientation order

1. [`goal.md`](goal.md) — vision & success criteria.
2. [`docs/PRD.md`](docs/PRD.md) — requirements (FR-/NFR- IDs).
3. [`docs/plans/2026-06-01-team-chat-design.md`](docs/plans/2026-06-01-team-chat-design.md)
   — architecture & rationale.
4. [`AGENTS.md`](AGENTS.md) — how to work here.

## The one rule to never break

A persona is a **long-lived, resumable harness session — one per (room, persona)**.
We orchestrate sessions; we never reimplement the agent loop or rebuild persona
context ourselves. See AGENTS.md §2.

## Claude-Code-specific notes

- **Use skills.** Brainstorming produced the design; use **writing-plans** before
  implementing, **test-driven-development** while implementing, and
  **systematic-debugging** for any bug. These are not optional here.
- **TDD is enforced.** Write the failing test first (AGENTS.md §5). The `MockHarness`
  backend means you can build and test the entire app with **zero token spend** and
  no real CLI — default to it.
- **This app shells out to the `claude` CLI.** When editing `harness/claude_harness.py`,
  the relevant headless flags are `--print --output-format stream-json --resume
  --append-system-prompt --model --effort --mcp-config --add-dir --permission-mode`.
  Verify behavior against recorded fixtures, not live calls, in tests.
- **Don't confuse the two layers of "Claude."** This file governs *you, the agent
  editing the repo*. The code's use of the Claude CLI to power personas is a
  separate, product-level concern documented in the design doc.
- **Verify before claiming done.** Run `pytest`, `ruff`, and `mypy` and report real
  output. Never assert success without evidence (AGENTS.md §9).

## Quick commands

```bash
pytest                 # default to MockHarness; no token spend
ruff check . && mypy app
python -m app.main     # run locally on localhost
```

For anything not covered here, **AGENTS.md wins.**
