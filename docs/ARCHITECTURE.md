# Team — Architecture & Code Map

> For a new contributor: read this top-to-bottom once, then keep it open while you
> work. It maps every module, traces the end-to-end message flow, and lists the
> invariants you must not break. Pair it with [`DECISIONS.md`](DECISIONS.md) (the
> *why*) and [`HANDOFF.md`](HANDOFF.md) (current status + backlog).
>
> Companion rules of the road: [`../AGENTS.md`](../AGENTS.md) (engineering bar:
> layering, TDD, fail-loud, definition of done).

## 1. The one idea

**A persona is a long-lived, resumable CLI-harness session — one per `(room, persona)`.**
When you create `@architect` and add it to a room, Team starts a `claude` (or
`codex`) session for that pair. Each time the persona speaks again, Team
**resumes that same session** (`claude --resume <id>` / `codex exec resume <id>`),
so the harness — not Team — owns that persona's context window, thinking, tool
use, and MCP servers. Team never reimplements the agent loop.

The "shared transcript" the personas appear to share is reconstructed per turn by
**delta injection** (the attributed messages a persona hasn't seen since it last
spoke) plus **explicit quote injection** (messages you deliberately quote). See
§4 for the exact assembly.

If a change blurs this boundary — rebuilding context ourselves, losing the ability
to resume, or coupling layers — it is almost certainly wrong.

## 2. Layers (dependencies point inward)

```
app/
├── domain/        Pure data. Pydantic models + the normalized StreamEvent union.
│                    NO I/O, no subprocess, no FastAPI, no sqlite. Imported by all.
├── harness/       The ONLY layer that spawns subprocesses / parses CLI output.
│                    AgentBackend protocol + Claude/Codex/Mock backends + registry.
├── persistence/   The ONLY layer that touches the DB / filesystem run-log store.
│                    stdlib sqlite3 repositories + JSONL run logs.
├── services/      Orchestration & business logic. Depends on domain + harness +
│                    persistence (via their classes/protocols). NO FastAPI import.
├── api/           FastAPI REST + WebSocket. Thin: translates HTTP/WS <-> services.
│                    The composition root (app_factory) is the ONLY api file that
│                    may import harness adapters (to wire them).
├── web/           Buildless ES-module JS + CSS. No bundler, no npm deps, no CDN.
├── config/        pydantic-settings, structured logging, startup health check.
└── main.py        Entry point: `python -m app.main` -> uvicorn over create_app().
```

**Dependency rule (enforced in review):** `domain` depends on nothing internal;
`api`/`persistence` may depend on `services`/`domain`; `services` never imports
`api`; cross-layer access goes through the inner layer's protocols so `MockHarness`
can replace the real backends in tests. `mypy --strict` + ruff run on `app/`.

## 3. Module map

Line counts are a rough size signal (as of this writing). Start with the **bold**
files — they carry the load.

### domain/ — pure data (~240 lines)
- `models.py` — `Persona`, `HumanAuthor`, `Room`, `Message`, `PersonaSession`,
  `RunRecord` + enums `Provider` (claude/codex/mock), `PermissionMode`
  (read-only/ask/auto), `ReplyMode` (sequential/parallel), `AuthorKind`
  (human/persona). `validate_assignment=True` on entities; `Persona.handle` is
  normalized + regex-validated; `Message.quoted_message_ids` is deduped.
- `events.py` — the **`StreamEvent`** discriminated union (`kind`): `TextDelta`,
  `ThinkingDelta`, `ToolUse`, `ToolResult`, `Usage` (+optional `context_tokens`),
  `RunError` (+`run_id`/`log_path`), `RunDone` (+`session_id`/`run_id`).
  `parse_event(dict)` validates via a TypeAdapter. This is the lingua franca
  across every layer.
- `errors.py` — `TeamError` base (+`.kind`) and subclasses `HarnessError`,
  `HarnessTimeout`, `HarnessAuthError`, `SessionNotFound`, `ProviderUnavailable`,
  `TranscriptError`, `NotFound`.

### harness/ — the only subprocess layer (~1500 lines)
- `base.py` — `RunSpec` (everything a backend needs for one run) + the
  `AgentBackend` Protocol (`name`, `supported_commands`, `run`, `send_command`).
- **`claude.py`** / `claude_parser.py` — `ClaudeHarness` + the pure stream-json
  parser. The parser holds ALL Claude wire-format knowledge; the adapter spawns
  the process, streams events, captures the `session_id`, and emits the terminal
  `RunDone`. Real-subprocess hardening lives here: concurrent stderr drain,
  kill/reap on every exit path, bounded stdout lines, secret redaction, typed
  errors, the `--` end-of-options guard, the PermissionMode→claude-vocab map, and
  the neutral-cwd isolation. See DECISIONS.md D7–D9.
- **`codex.py`** / `codex_parser.py` — `CodexHarness` + its parser, mirroring the
  hardened Claude pattern for `codex exec` (resume is a subcommand:
  `codex exec resume <thread_id>`). Maps PermissionMode→`--sandbox`.
- `mock.py` — **`MockHarness`**: deterministic, scriptable backend. The backbone of
  every default test (no real CLI, no tokens). Fails loud on ambiguous script
  matches. Read this before writing orchestrator/api tests.
- `registry.py` — `BackendRegistry` + `build_default_registry(scratch_dir)`. The
  composition root builds the real registry; tests inject a Mock-only one.

### persistence/ — the only DB/FS layer (~580 lines)
- `db.py` — thin `Database` wrapper over stdlib `sqlite3`: `row_factory`,
  `foreign_keys=ON`, WAL, an **RLock + transaction-nesting** guard (so an
  interleaved write can't prematurely commit an open transaction), `query`/
  `execute`/`executemany`/`transaction`/`close`.
- `schema.sql` — 8 tables (PRD §7). `run_record` deliberately has **no** FK to
  room/persona (audit rows survive deletion — see the comment in the file).
- `repositories.py` — one repo per aggregate (Persona/Author/Room/Message/Session/
  Run). Row⇄model via `model_construct` (trusted store; no re-validation on read).
- `run_log.py` — `RunLogStore`: per-run JSONL at
  `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl`.

### services/ — orchestration (~1300 lines)
- **`orchestrator.py`** — `ChatOrchestrator`, the keystone. `post_message(...)` is
  an async generator yielding `(persona_id, StreamEvent)`; `send_control(...)` runs
  a session command (`/compact`). Owns routing, sequential vs parallel turns,
  prompt assembly, session resume + pointer advance, RunRecord + run-log, per-
  persona error isolation, and `run_id` threading. See §4.
- `transcript.py` — pure `build_delta` / `delta_messages` (the single source of the
  "messages after the last-seen pointer" slice; raises `TranscriptError` on an
  unknown pointer) + `render_quotes`.
- `prompt.py` — pure `assemble_prompt`: order is **author weight note → quotes →
  delta → directed line**.
- `routing.py` + `mentions.py` — resolve `@handle`/`@everyone` targets; the shared
  mention regex mirrors the backend handle grammar (and the JS `mentions.js`).
- `personas.py` / `rooms.py` / `authors.py` / `session_store.py` — CRUD facades
  that add value over the repos (duplicate, templates, `ensure_defaults()` seeding
  Me+Boss, `reset_session`, in-flight tracking).

### api/ — thin transport (~430 lines)
- `app_factory.py` — **the composition root**: configures logging, ensures dirs +
  the neutral persona scratch dir, opens the db, builds repos/services/orchestrator,
  builds the registry (real adapters unless one is injected), seeds default
  authors, mounts routers + `/static` + `GET /`, registers the WS endpoint and the
  centralized `TeamError`→HTTP handler, and closes the db on lifespan shutdown.
- `routes_*.py` — REST CRUD for personas/authors/rooms(+members)/messages, plus
  `GET /runs/{run_id}/log` (path-traversal-guarded) and a persona session reset.
- `ws.py` — `/ws/rooms/{room_id}`: drives the orchestrator on the event loop and
  streams frames (see §5). `deps.py` provides services from `app.state`;
  `schemas.py` are the write bodies; `health.py` is the PATH-presence check.

### web/ — buildless frontend (~13 ES modules)
`main.js` bootstraps; `api.js`/`ws.js` are the REST/WS clients; `transcript.js`
renders messages + live stream (thinking, tool cards, usage, error cards);
`composer.js` (writer selector, mentions, quotes, reply mode); `rooms.js`/
`personas.js`/`console.js` are the editors/console; `dom.js`/`markdown.js`/
`modal.js`/`error_marker.js`/`errors.js`/`mentions.js` are helpers. **All DOM sinks
are `textContent` / safe construction — no `innerHTML` anywhere (XSS rule).**

### config/ + main.py
- `settings.py` — `Settings` (env prefix `TEAM_`): host (loopback), port, data_dir,
  derived db_path/logs_dir, claude_bin/codex_bin, and `resolved_scratch_dir` (a
  neutral dir **outside** the project tree — see DECISIONS.md D9).
- `logging.py` — JSON formatter + `run_id_var` ContextVar.
- `main.py` — `build_app()` (import-safe seam) + `uvicorn.run` under `__main__`.

## 4. End-to-end: what happens when you post a message

`ChatOrchestrator.post_message(room_id, *, author, text, reply_mode, tagged_override=None, quoted_ids=())`:

1. **Persist** the human `Message` (author_kind=HUMAN, the quoted ids).
2. **Resolve targets** via `routing.resolve_targets(text, room_members)`. No
   mention → no run (the message is stored as context only). `@everyone` → all
   members in room order.
3. For each target **persona turn** (`_run_turn`):
   a. Load its `PersonaSession` → `resume_session_id` + `last_seen_message_id`.
   b. `delta = build_delta(messages, last_seen, persona_handle, name_of)` —
      attributed lines for everything the persona hasn't seen.
   c. `quotes = render_quotes(quoted_messages, name_of)` (deduped against the
      delta slice).
   d. `prompt = assemble_prompt(delta, quotes, author, new_text, persona_handle)`
      — author weight note (Boss) → quotes → delta → directed line.
   e. Build a `RunSpec` (persona model/effort/system_prompt/mcp/working_dir/
      permission_mode + the prompt + resume id). `backend = registry.get_backend(provider)`.
   f. Generate a `run_id`, set `run_id_var`, open a run-log writer, create a
      `RunRecord` (redacted command). Stream `backend.run(spec)`: write each event
      to the log, **yield `(persona_id, event)`**, accumulate reply text, capture
      `RunDone.session_id`/`Usage`.
   g. On success: persist the persona reply `Message`, upsert the `PersonaSession`
      (new session id + advanced last-seen pointer = the pre-reply transcript tail).
      On a raised harness error: emit a `RunError` event (with run_id + log_path),
      set `RunRecord.error_kind`, persist an `[error: …]` placeholder message —
      **the failure never aborts sibling personas**. Always finalize the RunRecord
      and reset `run_id_var` in `finally`.
4. **Sequential** mode runs targets in order, rebuilding the delta from current
   messages each time (so a later persona sees an earlier one's same-turn reply).
   **Parallel** mode snapshots the transcript once and runs targets concurrently
   (an `asyncio.Queue` + per-worker sentinel merges their streams; the generator's
   `finally` cancels + reaps workers on early close), so no persona sees a sibling's
   same-turn reply.

`send_control` is the same machinery for a one-shot session command: validates the
session + that the backend supports the command (else raises — never a silent
no-op), streams events, records a RunRecord.

## 5. WebSocket frame protocol (`api/ws.py` ⇄ `web/ws.js`)

Inbound (client → server):
- `{type:"post", author_id, text, reply_mode, quoted_ids}`
- `{type:"command", persona_id, command}`

Outbound (server → client):
- `{type:"event", persona_id, event:{…StreamEvent model_dump, has "kind"}}`
- `{type:"error_card", persona_id, run_id, error_kind, message, command_redacted, log_url}`
  (a translated `RunError`; raw RunError is not also sent)
- `{type:"turn_complete"}` / `{type:"command_complete"}`
- `{type:"error", kind, message}` for inbound/validation failures (socket stays open)

**Canonical-id contract:** the stream does not carry the persisted reply message
id. On `turn_complete`/`command_complete`/`error`/socket-reopen, the client
refetches `GET /rooms/{id}/messages` to reconcile optimistic bubbles to canonical
messages (this is also how a failed run's persisted `[error:]` marker becomes the
single styled error card). The orchestrator runs **on the event loop** (not a
threadpool) so the shared sqlite connection's per-thread RLock is never crossed.

## 6. Invariants you must not break

1. **One persona = one resumable session per room.** Capture & persist the
   harness `session_id`; resume with it. Don't rebuild context yourself.
2. **`domain/` stays pure**; `harness/` is the only subprocess layer; `persistence/`
   the only DB/FS layer; `api/` stays thin. Only `app_factory` wires harness adapters.
3. **`StreamEvent` is the only cross-layer event shape.** New CLI quirks are
   normalized inside the parser, never leaked upward.
4. **Fail loud, fail located.** No silent fallbacks; every failure raises a typed
   `TeamError` or emits a `RunError`, always carrying `run_id`. No secrets in
   logs/frames (`command_redacted`, `_redact`).
5. **One `run_id` per turn**, threaded through `run_id_var`, the `RunRecord`, the
   log filename, and the error card.
6. **Tests default to `MockHarness`.** Never spawn a real CLI in unit/CI tests
   (the only exception is the opt-in `pytest -m live`).
7. **Frontend is buildless + XSS-safe.** No bundler/npm/CDN; no `innerHTML`.
8. **Personas with no `working_dir` run in the neutral scratch cwd** (outside the
   project tree) so they don't inherit this repo's CLAUDE.md/AGENTS.md.

## 7. Where to change common things

| You want to… | Touch |
| --- | --- |
| Add a persona config field | `domain/models.py` (Persona) → `schema.sql`/`repositories.py` → `api/schemas.py`+routes → `web/personas.js`; map it in the adapter argv (`harness/claude.py`/`codex.py`) |
| Support a new provider | new `harness/<x>.py` + `<x>_parser.py` mirroring claude.py; register in `registry.py`; add `Provider` enum value |
| Change how a persona sees context | `services/transcript.py` / `services/prompt.py` (pure, unit-tested) — NOT the orchestrator |
| Add a control command (`/x`) | the backend's `supported_commands` + `send_command`; surface in `web/console.js` |
| Add a REST endpoint | a `routes_*.py` + a service method (keep the route thin) + a `deps` provider if needed |
| Change a WS frame | `api/ws.py` and the matching handler in `web/ws.js`/`transcript.js` together |
| Tune Claude/Codex CLI flags | the adapter's argv builder + its unit test; verify real behavior with `pytest -m live` |

## 8. Testing topology

- **Unit** (`tests/unit/`): per layer, all against `MockHarness`/fakes. The harness
  adapters use an injectable `spawn` + tiny real `python3`/`sh` children to test the
  subprocess machinery (drain/kill/over-long-line) without the real CLIs.
- **Integration** (`tests/integration/`): FastAPI `TestClient` + WS, Mock registry,
  temp db. `test_e2e.py` is the keystone proving the 7 success criteria.
- **Frontend** (`tests/js/*.mjs`): `node --test` for the pure JS logic
  (mention parsing, markdown tokenizer, error-marker parsing).
- **Live** (`tests/live/`): opt-in `pytest -m live`, spawns the real CLIs.
