# Team

Team is a locally-run, Slack-style group chat where one human collaborates with
configurable AI personas — each backed by a real **Claude Code** or **Codex** CLI
harness running as a managed subprocess. You drive the conversation: post
messages, tag the personas who should respond, speak as different authors (Me /
Boss) to weight input, and let personas debate each other (sequential cross-talk
or parallel fan-out), quote earlier messages, and resume exactly where they left
off after a restart. Every harness run streams live and is logged to disk so any
error points straight to its cause. See [`goal.md`](goal.md) for the vision and
the seven success criteria, and [`docs/PRD.md`](docs/PRD.md) for the detailed
requirements.

## Prerequisites

- **Python 3.12+** (developed on 3.14).
- The **`claude`** and **`codex`** CLIs installed, on your `PATH`, and **logged
  in** — Team uses your existing CLI subscriptions; there is no API-key mode.
  (You only need a given CLI if you create personas on that provider. The
  mock-only dev mode below needs neither.)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run (real personas)

```bash
python -m app.main
# then open the printed http://127.0.0.1:8000
```

This wires the real `claude`/`codex` adapters and binds loopback only (local
use). Host/port and storage locations are configurable via `TEAM_`-prefixed env
vars, e.g. `TEAM_PORT=9000`, `TEAM_DATA_DIR=./mydata` (the db and run logs
default under `data_dir`). A startup health banner in the UI warns if either CLI
is missing or unverified.

## Run token-free (mock personas)

To click around the entire UI — streaming, transcript, quote chips, reply modes,
the session console, error cards — with **zero token cost** and no real CLI:

```bash
python -m scripts.dev_mock
# then open http://127.0.0.1:8000
```

Every provider resolves to a deterministic `MockHarness` that echoes prompts.
It seeds a demo room with two mock personas (`@arch`, `@critic`) on first run and
stores its state under `./data-dev` so it never touches real data.

## Tests & quality gates

```bash
pytest                       # all Python tests (MockHarness — no real CLI, no tokens)
node --test tests/js/        # buildless frontend JS unit tests

# Quality gates (run before declaring work done — AGENTS.md §9)
ruff check . && ruff format --check .
mypy app
```

The Python suite **defaults to `MockHarness`** and never spawns a real CLI. The
keystone end-to-end test (`tests/integration/test_e2e.py`) boots the full app on
a temp db and drives a complete conversation that proves all seven success
criteria.

### Opt-in real-CLI smoke test

`tests/live/test_real_cli_smoke.py` spawns the **real** `claude`/`codex` once to
validate the adapters/parsers against a true capture. It is marked `live` and
**deselected by default** (`pytest` runs nothing live and spends no tokens). Run
it explicitly when you want to verify against the real CLIs:

```bash
pytest -m live               # requires the CLIs installed + logged in (spends a few tokens)
```

## Architecture (brief)

Inward-pointing layers (dependencies point inward; see
[`AGENTS.md`](AGENTS.md) §3 for the full rules):

```
app/
├── domain/        Pure Pydantic models + the normalized StreamEvent union (no I/O).
├── harness/       AgentBackend protocol + Claude/Codex/Mock backends (the ONLY
│                    subprocess layer; normalizes CLI output into StreamEvents).
├── services/      Orchestration: routing, delta/quote/weight prompt assembly,
│                    sequential/parallel turns, session lifecycle.
├── persistence/   stdlib sqlite3 repositories + on-disk JSONL run-log store.
├── api/           FastAPI REST + WebSocket (thin transport over services).
├── web/           Buildless ES-module JS + CSS frontend (no bundler).
├── config/        pydantic-settings, structured logging, startup health check.
└── main.py        Composition root: `python -m app.main`.
```

**The one idea:** a persona is a long-lived, resumable harness session — one per
(room, persona). The harness owns context, thinking, tools, and MCP; Team
orchestrates these sessions into a shared transcript via delta injection plus
explicit quote injection. We never reimplement the agent loop.

## Error-finding playbook

When a run misbehaves, every failure is traceable in one hop (AGENTS.md §7):

1. Find the **`run_id`** on the error card in the UI (or a structured log line).
2. Open the run log at `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl` — the
   full normalized harness stream for that exact run. The error card's
   "log" link (`GET /runs/<run_id>/log`) serves this file directly.
3. Read the `run_record` row in SQLite for the redacted command, exit code, and
   timing.
4. Reproduce with `MockHarness` by scripting the offending stream into a test.

See [`AGENTS.md`](AGENTS.md) for the full engineering guide (TDD, the typed-error
taxonomy, "fail loud, fail located", and the definition of done).
