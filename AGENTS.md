# AGENTS.md — engineering guide for Team

This is the **single source of truth** for how to work in this repository. It
applies to every agent and human contributor (Claude Code, Codex, or otherwise).
`CLAUDE.md` defers to this file and adds only Claude-Code-specific notes.

Read this fully before changing code.

---

## 1. What this project is

Team is a locally-run Python web app: a Slack-style group chat between one human
operator and configurable AI personas, each backed by a real CLI harness (Claude
Code or Codex) run as a subprocess. Start with these, in order:

1. [`goal.md`](goal.md) — the vision and success criteria.
2. [`docs/PRD.md`](docs/PRD.md) — the requirements (FR-/NFR- IDs).
3. [`docs/plans/2026-06-01-team-chat-design.md`](docs/plans/2026-06-01-team-chat-design.md)
   — the architecture and the rationale behind it.

Do not contradict those documents. If the code needs to diverge, update the
document in the same change and say why.

## 2. The one idea you must hold in your head

**A persona is a long-lived, resumable harness session — one per (room, persona).**
We never reimplement the agent loop. The harness owns the persona's context,
thinking, tools, and MCP. We orchestrate sessions into a shared transcript using
**delta injection** (attributed unseen messages) plus **explicit quote injection**.
If a change blurs this boundary — e.g. rebuilding context ourselves, or losing the
ability to resume — it is almost certainly wrong.

## 3. Architecture & layering

```
app/
├── domain/        Pure data: Pydantic models + the normalized StreamEvent union.
│                    No I/O. No subprocess. No FastAPI. No sqlite.
├── harness/       AgentBackend protocol + ClaudeHarness / CodexHarness / MockHarness.
│                    The ONLY layer that spawns subprocesses or parses CLI output.
├── services/      Orchestration & business logic. Depends on domain + harness +
│                    persistence via interfaces. No FastAPI imports.
├── persistence/   SQLite repositories (stdlib sqlite3) + on-disk run-log store.
│                    The ONLY layer that touches the DB or the filesystem store.
├── api/           FastAPI routes + WebSocket. Thin. Translates HTTP/WS <-> services.
├── web/           Buildless frontend: ES-module JS + WebSocket + CSS. No bundler.
├── config/        pydantic-settings, paths, startup health checks.
└── main.py        Composition root: wires concrete implementations together.
```

**Dependency rule (enforced by review):** dependencies point inward.
`domain` depends on nothing. `api` and `persistence` may depend on `services` and
`domain`. `services` never imports `api`. Cross-layer access goes through the
protocols/interfaces declared in the inner layer, so `MockHarness` can replace the
real one in tests.

## 4. Non-negotiable engineering rules

These exist because the operator's top priorities are **code quality** and
**errors being trivial to locate**.

1. **Fail loud, fail located.** No bare `except:`. No swallowed exceptions. No
   silent fallbacks. Every failure either raises a typed error or emits a structured
   error event — always carrying the `run_id`.
2. **Typed boundaries.** All data crossing a layer boundary is a Pydantic model from
   `domain/`, validated at the edge. No passing raw dicts between layers.
3. **One `run_id` everywhere.** Generated when a run starts; threaded through logs,
   the DB `run_record`, the error card, and the log filename. Any symptom must lead
   to its run in one hop.
4. **No secrets in logs.** Commands are stored redacted. Never log tokens, env
   secrets, or full auth headers.
5. **Errors are typed.** Use the taxonomy: `HarnessError`, `HarnessTimeout`,
   `HarnessAuthError`, `SessionNotFound`, `ProviderUnavailable`. Add to it rather
   than raising bare `Exception`.
6. **Small functions, clear names.** Match the surrounding style. A reader should
   locate the cause of a bug from names and structure alone.
7. **YAGNI.** Build what the PRD asks for. No speculative abstraction.
8. **Update docs with code.** If behavior changes, the PRD/design change in the same
   commit.

## 5. Test-driven development is mandatory

- **Write the failing test first**, watch it fail, then implement. This is required,
  not optional.
- The **`MockHarness`** backend is the backbone of testing: it produces deterministic
  scripted `StreamEvent` streams so the full app — routing, streaming, persistence,
  restart — runs with **no real CLI and zero token spend**. Default all tests to it.
- **Never call the real `claude`/`codex` CLIs in unit or CI tests.** Real-CLI checks
  are separate, opt-in, and clearly marked.
- Layers to cover: orchestrator routing (`@tag`, `@everyone`, untagged → no run),
  delta builder, quote injection, Boss weighting, stream parsers (recorded
  fixtures), and the full message→reply flow including sequential cross-talk,
  parallel snapshotting, and restart/resume.
- When you fix a bug, first write a test that reproduces it.

## 6. Commands

> The repo is being built; if a command below is missing, add it as part of your
> change rather than working around it. Keep this list current.

```bash
# Setup (Python 3.12+; local env is 3.14)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # deps from pyproject.toml

# Run the app (binds localhost only)
python -m app.main               # then open the printed http://127.0.0.1:PORT

# Tests (must default to MockHarness — no token spend)
pytest                           # all
pytest -q tests/unit             # fast unit layer
pytest -k orchestrator           # focused

# Quality gates (run before declaring work done)
ruff check . && ruff format --check .
mypy app
```

## 7. The error-finding playbook

When something misbehaves, in this order:

1. **Find the `run_id`** from the error card in the UI or the structured log line.
2. **Open the run log** at `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl` — it is
   the full harness stream for that exact run.
3. **Read `run_record`** in SQLite for the redacted command, exit code, and timing.
4. **Reproduce with `MockHarness`** by scripting the offending stream into a test.
5. Only then change code — with the failing test in hand (rule §5).

The architecture is designed so this path always works. If you find a failure that
*can't* be traced this way, that gap is itself a bug — fix the observability.

## 8. Working with the harnesses

- Treat each backend's quirks as isolated to its adapter in `harness/`. Normalize
  everything into the shared `StreamEvent` union before it leaves the layer.
- Capture and persist the `session_id` the harness emits; resume with it next turn.
- Honor `supported_commands`: `/compact`, `/clear`, raw commands must degrade
  gracefully and visibly when a backend lacks them (see PRD OQ-1).
- See `docs/plans/...-design.md` "Backend abstraction" for the exact flag mapping.

## 9. Definition of done

A change is done only when: the new/changed behavior has tests (written first and
passing), `ruff` and `mypy` are clean, no rule in §4 is violated, affected docs are
updated, and — if you claimed something works — you ran it and observed the result.
Never assert success without evidence.
