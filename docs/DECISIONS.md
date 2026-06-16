# Team — Decisions Log

Why the project is shaped the way it is. A new agent should read this before
changing the harness layer, the orchestrator, or anything about how personas are
spawned — several of these were learned the hard way (some only surfaced under
**live** testing, invisible to the mock suite). Don't re-litigate or silently
revert them; if you must change one, update this file and say why.

Format: decision → rationale → consequences. The brainstorming-era decisions are
also in [`plans/2026-06-01-team-chat-design.md`](plans/2026-06-01-team-chat-design.md);
the requirements are in [`PRD.md`](PRD.md).

---

## D1. Engine: wrap the real CLI harnesses (not SDKs/APIs)

Each persona is driven by the actual `claude` / `codex` CLI run as a subprocess,
not by the Anthropic/OpenAI SDKs.

- **Why:** the CLIs already own context windows, resume, thinking, tool use, and
  MCP, and they authenticate via the user's existing **subscription** (no API
  keys, no per-token billing). Reusing them avoids reimplementing the agent loop
  and gives durable, resumable sessions for free.
- **Consequences:** we inherit CLI quirks and the operator's CLI environment
  (see D8/D9), and several behaviors can only be verified live (D10). Auth is
  whatever the CLIs have; there is deliberately no API-key mode.

## D2. One persona = one resumable harness session per room

State lives in the harness session, keyed `(room, persona)`. We persist only the
`session_id` + a last-seen-message pointer and resume each turn.

- **Why:** keeps each persona's context lean and correct, makes restart/resume
  trivial, and cleanly separates rooms (a persona in two rooms has two contexts).
- **Consequences:** the "shared transcript" is reconstructed by delta injection
  (D3), not stored in the harness. Resuming across a **cwd change** fails (the CLI
  scopes sessions per directory) — so changing a persona's `working_dir` requires
  a session reset.

## D3. Shared transcript via delta + quote injection (pure functions)

Per turn, a persona receives the attributed messages it hasn't seen since it last
spoke (`build_delta`) plus any explicitly quoted messages (`render_quotes`),
assembled in `prompt.assemble_prompt`. These are pure, unit-tested functions.

- **Why:** correctness + testability; keeps the orchestrator thin and the context
  logic in one place. An unknown last-seen pointer raises `TranscriptError` rather
  than silently dumping full history (fail-loud).
- **Consequence / known limitation (OQ-4):** message content is untrusted and is
  concatenated with `[author]:` attribution lines, so a body can contain a fake
  `[Boss → @x]:` line — attribution is **forgeable**. Accepted for a local
  single-user tool; a structural delimiter/escape is the mitigation if multi-user
  or untrusted input is ever added.

## D4. Per-message sequential vs parallel; uniform per-persona error isolation

Sequential rebuilds the delta from current messages between targets (real
cross-talk); parallel snapshots once and runs concurrently (no same-turn sibling
visibility), merging streams via an `asyncio.Queue` + sentinels.

- **Why:** debate when you want it, speed when you don't.
- **Decision:** a failure in one persona's turn is **recorded** (RunError +
  placeholder message + finalized RunRecord) and **never aborts siblings**, in
  both modes and for both typed and untyped exceptions. (Early versions re-raised
  in sequential mode, which aborted later siblings — fixed.)
- **Consequence:** the generator's `finally` must `aclose()`/cancel workers so an
  early client disconnect reaps in-flight harness subprocesses (this caught a real
  task/subprocess leak — keep the `aclose` chain intact).

## D5. Storage: stdlib `sqlite3`, no ORM

A thin typed `Database` wrapper + hand-written repositories.

- **Why:** zero heavy-dependency risk on Python 3.14 (ORM wheels lagged), and the
  SQL stays inspectable. Single local user → sqlite is plenty.
- **Consequences:** one shared connection (`check_same_thread=False`) guarded by an
  **RLock + transaction-nesting counter** so an interleaved `execute()` can't
  prematurely commit an open `transaction()`. The orchestrator therefore runs on
  the **event loop** (not a threadpool) so a transaction never spans threads (D6).
  `run_record` has no FK to room/persona on purpose (audit rows outlive deletion).
  Row→model uses `model_construct` (trusted store; no re-validation on read).

## D6. WebSocket drives the orchestrator on the event loop

`ws.py` does `async for … in orchestrator.post_message(...)` directly; it does not
offload to a threadpool.

- **Why:** the shared sqlite connection's RLock is per-thread-reentrant; keeping
  all DB work on one thread avoids a cross-thread deadlock, and the synchronous
  repo calls have no inner awaits so they complete atomically without interleaving.
- **Consequence:** a single socket serializes its posts (one inbound frame is fully
  drained before the next is read). Malformed/binary inbound frames are caught and
  answered with an `{type:error}` frame, never crashing the socket.

## D7. PermissionMode is provider-agnostic; each adapter maps it to its CLI's vocabulary

Domain `PermissionMode` is `read-only | ask | auto`. The CLIs use different words.

- **Claude** `--permission-mode` accepts `acceptEdits|auto|bypassPermissions|default|dontAsk|plan`.
  Map: `read-only → plan`, `ask → default`, `auto → acceptEdits`.
- **Codex** uses `--sandbox`. Map: `read-only → read-only`, `ask → read-only`
  (codex exec is non-interactive — it *can't* prompt for approval, so "ask"
  conservatively means no writes), `auto → workspace-write`. Codex also gets an
  explicit `-c approval_policy=never`.
- **Why:** found live — passing our raw values to `claude --permission-mode`
  errors ("argument 'read-only' is invalid"). This was invisible to mocks.
- **Consequence:** if you add a PermissionMode value, update **both** adapter maps;
  a test asserts every value maps to a CLI-valid choice.

## D8. Argv safety: `--` end-of-options guard; no shell

Both adapters build argv as a list (no shell) and insert a literal `--` before the
prompt positional, on run/resume/send_command.

- **Why:** found live — a user message beginning with `-` (e.g. `--version`, or
  worse `-c sandbox=danger-full-access`) was parsed as a CLI flag/injection. `--`
  forces it to be treated as prompt text. Verified against both CLIs.

## D9. Persona environment isolation = neutral cwd outside the project; NOT `--bare`

Personas with no bound `working_dir` are spawned in a neutral scratch directory
**outside** the repo tree (`Settings.resolved_scratch_dir`, under the system temp).

- **Why:** found live — personas were inheriting this repo's `CLAUDE.md`/`AGENTS.md`
  (the CLIs walk **up** the directory tree to find them) and behaving like a "Team
  coding agent" instead of their configured persona. A scratch dir *inside* `data/`
  was not enough (still found the repo's docs by walking up); it must be outside.
- **Rejected:** `--bare` and `--setting-sources ""` both *looked* like the fix
  (they skip hooks/plugins) but **break auth** ("Not logged in · Please run
  /login") because they also drop the credential bootstrap. So **no isolation
  flags are passed.** Verified live, twice.
- **Known limitation (OQ-5):** the operator's **user-global** Claude Code
  plugins/hooks and codex's global `~/.codex/AGENTS.md` still load for persona
  children. The robust fix is to run each persona with an isolated config dir
  (credentials copied in, no plugins/hooks/global AGENTS.md). Not yet built. (The
  specific superpowers-bootstrap *error* in `~/.codex/AGENTS.md` was fixed at the
  environment level — see HANDOFF.md "Environment notes".)

## D10. Live behavior is verified by an opt-in test, not the default suite

The default `pytest` run uses `MockHarness` exclusively (no real CLI, no tokens).
Real-CLI behavior is `tests/live/test_real_cli_smoke.py`, marked `live` and
deselected by default (`addopts = -m 'not live'`).

- **Why:** fast, deterministic, free CI; but mocks can't catch real-CLI surprises
  (D7/D8/D9 were all found by manual live runs). Run `pytest -m live` (and the
  remaining open questions in HANDOFF.md) when touching the adapters.

## D11. Frontend is buildless and XSS-safe by construction

Vanilla ES modules + CSS, served as static files. No bundler, no npm, no CDN. The
only DOM sinks are `textContent` / safe element construction; there is no
`innerHTML` anywhere, and the markdown renderer emits text nodes only.

- **Why:** zero toolchain to break, and persona/model output (untrusted) can never
  inject markup. Markdown intentionally avoids `_`-emphasis so `snake_case`/code
  isn't mangled.

## D12. Errors are visible exactly once, and traceable in one hop

A failed run persists an `[error:]` marker message *and* the WS emits a transient
`error_card`. The canonical render coalesces the persisted marker into the single
styled card (transient cards are cleared on the reconcile repaint). `run_id` ties
the UI card → `RunRecord` → `data/logs/.../<run_id>.jsonl` → reproduce-in-mock.

- **Why:** the operator's stated top priority is "errors easy to find."

## D14. Codex web search is enabled by a `web_search` entry in `allowed_tools`

`allowed_tools` is otherwise stored-but-not-enforced (operator's choice), with one
exception: if a **codex** persona's `allowed_tools` contains a web-search marker
(`web_search`/`WebSearch`/`web search`/`web-search`, normalised to alphanumerics),
the adapter prepends the top-level `--search` flag (`codex --search exec …`),
enabling codex's native `web_search` tool.

- **Why this shape:** `--search` is a *global* codex flag — `codex --search exec`
  parses but `codex exec --search` errors (verified, codex-cli 0.130.0) — so it
  goes before the subcommand, and there is no exec-level flag or stable config key
  to use instead. Gating on `allowed_tools` ties it to the existing picker rather
  than inventing a new persona field, and keeps it per-persona/opt-in.
- **Consequence:** only the wiring (config → argv) is unit-verified; that codex
  actually performs a live search and returns sourced output is a live behaviour
  to confirm with a real run (OQ-style, like the other live checks). Claude's web
  search is separate (its own tools/MCP) and not covered here.

## D13. Persona-to-persona delegation reuses `@`-routing — bounded, no second loop

A persona's reply that `@`-mentions another persona delegates a turn to that
persona (auto-adding them to the room if needed). Implemented in the orchestrator
as a breadth-first pass *after* the operator's targets reply: scan each reply for
mentions (`_resolve_delegations`), run the mentioned persona via the **same**
`_run_turn` machinery (a transient author = the delegator's name + a short nudge;
the delegator's actual message arrives through the normal delta), then scan its
reply, and so on.

- **Why this shape:** it does **not** introduce a second agent loop or a new
  prompt path — it reuses `find_mentions` + the existing turn/delta/stream code,
  staying true to the core rule (AGENTS §2). A persona learns who it can call from
  an auto-injected **roster** (handle + job) appended to its *system* prompt (not
  the user prompt, so it steers behaviour without polluting the transcript).
- **Caps (the hard part):** unbounded delegation is a runaway-cost / infinite-loop
  risk. Three independent bounds, all in `orchestrator.py`: depth ≤
  `DELEGATION_DEPTH_CAP` (2), ≤ `DELEGATION_RUN_CAP` (6) delegated turns per post,
  and **once per persona per post** (the cycle guard — the strongest of the three;
  it alone guarantees termination). The "Balanced" preset was the operator's choice.
- **Per-room toggle:** `Room.delegation_enabled` (default on) gates the whole
  feature; when off, no scanning, no roster injection. Auto-adding a mentioned
  non-member is the "personas can add others to rooms" capability.
- **Consequences:** delegated turns stream and persist exactly like operator-driven
  turns (same `(persona_id, event)` contract, same per-persona error isolation, same
  `aclose` cleanup chain — each sub-generator is held + aclose()'d in `_drive` /
  `_run_delegations`). The frontend re-fetches membership on reconcile so an
  auto-added persona appears in the header/roster. Attribution remains forgeable
  (D3/OQ-4): a message body could fake a `@handle` — accepted for the local
  single-user model, and bounded by the caps regardless.
