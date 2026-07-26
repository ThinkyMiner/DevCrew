# Team — Handoff & Status

**This is the entry point for a new contributor/agent.** It tells you the current
state, what's proven vs unverified, the backlog (prioritized), and how to get
oriented fast. Last updated: **2026-06-04**.

## 1. What Team is

A locally-run, Slack-style group chat where one human (speaking as **Me** or
**Boss**) collaborates with configurable AI **personas**, each backed by a real
**Claude Code** or **Codex** CLI session. Tag personas with `@handle`, let them
debate (sequential) or answer in parallel, quote earlier messages into their
context, run `/compact` on a heavy session, and resume everything after a restart.
Full streaming + every run logged to disk.

Vision & success criteria: [`../goal.md`](../goal.md). Requirements:
[`PRD.md`](PRD.md). Architecture/code-map: [`ARCHITECTURE.md`](ARCHITECTURE.md).
Why-it's-shaped-this-way: [`DECISIONS.md`](DECISIONS.md). Engineering bar:
[`../AGENTS.md`](../AGENTS.md). Running it: [`../README.md`](../README.md).

## 1a. 2026-07-20 — multi-round deliberation + eval system (D14)

Personas can now genuinely TALK to each other: mentioned teammates answer
back across bounded waves and the conversation converges on a close. Designed
via an adversarial Codex `gpt-5.6-sol` review + literature pass; plan in
[`plans/2026-07-20-deliberation-and-evals.md`](plans/2026-07-20-deliberation-and-evals.md),
rationale in [`DECISIONS.md`](DECISIONS.md) **D14**, requirement **FR-M7**.

- **Engine:** pure scheduler `app/services/deliberation.py` (decreasing
  autonomous budget 6, wave cap 3, causal mention eligibility + coalescing,
  reserved owner/dispatcher closing turn, quiescence). Termination is
  mechanical — no marker protocol, nothing depends on model compliance.
  Fixed en route: the stale-`_latest_reply` delegation bug (an errored turn
  could re-delegate from an older reply).
- **Prompts:** @ now means *request-a-turn* (plain names for reference);
  agreement must add substance; critic has an explicit dissent mandate;
  dispatcher wraps up what it dispatched. Existing DBs: run
  `python -m scripts.migrate_personas --refresh-prompts` to re-stamp seeded
  handles (operator-created personas untouched).
- **Context:** own successful replies are filtered from a persona's delta
  (its resumed session already has them; own `[error:]` markers stay).
- **Evals (new):** three tiers — unit invariants + `tests/sims/` scenario
  sims gate every PR at zero tokens; `evals/` generates fresh live
  transcripts and grades 8 dimensions with per-dimension anonymized LLM-judge
  calls (report-only until calibrated; see `evals/README.md` for the
  calibration protocol). Hard cap invariants are enforced mechanically in
  `evals/run_live.py` — never by the judge.
- **CI (new):** `.github/workflows/ci.yml` — ruff, format, mypy (app+evals),
  pytest incl. sims, eval pipeline health (`--mock`), node JS tests. Live
  judged tier is `workflow_dispatch` on a self-hosted/authenticated runner.
- **Live smoke (ran once, 2026-07-20):** one real scenario
  (`storage-tradeoff`, real Opus persona) + a full judged report. Findings:
  (1) caps/quiescence held mechanically (1 turn, ceiling 8); (2) the judge
  DISCRIMINATES — it caught real protocol leakage (persona narrating
  ExitPlanMode/skills in chat, naturalness 2/5), traced to `evals/run_live.py`
  not passing `scratch_dir`, so the persona inherited the repo cwd and picked
  up this project's CLAUDE.md (fixed: neutral system-temp scratch dir, same
  rule as `Settings.resolved_scratch_dir`); (3) `responsiveness` is
  unjudgeable on solo transcripts (now `min_persona_turns=2`); (4) read-only
  permission mode held — the persona narrated wanting to write
  docs/DECISIONS.md but could not.
- **Open / unverified live:** multi-persona live deliberation smoke (wave
  nudges, closing behavior, own-message filtering under real resumed
  sessions) with the scratch fix in place; first judged baseline
  (`evals/baseline.json` doesn't exist yet — create from a good post-fix
  run); judge calibration vs human labels (protocol in `evals/README.md`).

## 1b. 2026-07-20 — team, orchestrator, dynamic models, sidebar order

Four operator-requested changes landed (all test-first, gates green):

- **`@systemd` orchestrator.** A seeded persona that routes work: tag it with a
  goal and it decomposes the task and @-mentions the right teammates with a crisp
  problem statement each; the existing delegation loop turns those mentions into
  real teammate turns. **Auto-added to every new room** (`RoomService.create`).
- **Named team + code seed.** The default team now has human first-names (Ada,
  Ken, Linus, …) with the role kept in `job`, defined in
  `app/services/persona_seed.py` and seeded at startup (idempotent, by handle).
  Seeded as normal, addable personas (not templates) — templates were being
  filtered out of the room "add member" picker, which is what pushed operators to
  duplicate them.
- **Dynamic model list.** Models are now backend-owned (`supported_models` on each
  harness, incl. `fable`) and served at `GET /models`; the persona editor fetches
  it instead of a hardcoded JS list. Free-typing still works.
- **Sidebar order.** `GET /rooms` returns rooms most-recently-active first
  (`RoomRepo.list_by_activity`); the active room floats to the top on
  `turn_complete`.

**One-time cleanup of an EXISTING `data/team.db`** (dedupe the old `-copy`
personas, merge the two in-use ones, human-name rename, ensure `@systemd`):

```bash
# STOP the running app first (it must not write while this runs).
python -m scripts.migrate_personas --dry-run     # preview counts, writes nothing
python -m scripts.migrate_personas               # backs up team.db, then migrates
```

It backs up `team.db` (+ WAL/SHM) with a timestamp before touching anything and is
idempotent. On the current DB it removes 15 unused copies, merges 2 (re-pointing
their messages/membership/sessions to the originals — 0 orphaned), renames 13, and
seeds `@systemd`, leaving 14 personas. Startup seeding (`ensure_default_personas`)
covers fresh installs; the script covers the pre-existing DB.

## 2. Current state

- **Status:** feature-complete against the PRD and the 7 success criteria; merged
  to `master`. Built across 13 reviewed units (each spec- + code-quality-reviewed),
  then hardened by live testing.
- **Tests (all green):** `pytest -q` → **272 passed, 2 deselected** (the deselected
  two are the opt-in `live` tests). `node --test tests/js/` → **25 passed**.
  `ruff check .`, `ruff format --check .`, `mypy app` → all clean.
- **Code size:** ~4,700 lines of `app/` Python + ~13 buildless JS modules.
- **Runs for real:** `python -m app.main` (uses your `claude`/`codex` logins).
  Token-free demo: `python -m scripts.dev_mock`.

## 3. Live-verified (real CLIs, manually) ✅

These were confirmed end-to-end through the running server, not just mocks:

- **Claude personas work and behave on-persona** — a configured "architect"
  answered as an architect (no repo-debugging contamination after the cwd-isolation
  fix, D9).
- **Session resume / memory works** — a follow-up turn correctly recalled the prior
  turn (proves D2 + the `--resume` path end-to-end).
- **Codex personas work** — answered cleanly after the global-AGENTS.md fix
  (see Environment notes below).
- **Both CLIs detected** by the startup health check; `--permission-mode` mapping
  (D7) and the `--` argv guard (D8) verified.

## 4. NOT yet verified / open questions ⚠️

Run `pytest -m live` and manual checks when you touch the adapters. Consolidated
from code `TODO(OQ-*)` markers and PRD §9:

- **OQ-1 — `/compact` & `/clear` real behavior.** Claude `send_command` resumes the
  session passing the slash command as the prompt; **unverified** that the headless
  CLI actually compacts. Codex `supported_commands` is empty (no verified
  non-interactive mechanism) so `/compact` to a codex persona fails loud by design.
  Markers: `harness/claude.py:433`, `harness/codex.py:236`.
- **OQ-3 — Codex event schema + system prompt.** The happy path (`agent_message`,
  `command_execution`, `usage`, `thread_id`) is live-verified; the `reasoning`,
  `error`, and `mcp_tool_call` shapes are best-effort guesses (tolerant parsing,
  won't crash), and it's **unverified** that `-c model_instructions=` actually
  applies the persona system prompt on codex. Markers: `harness/codex_parser.py:41`,
  `harness/codex.py:308`.
- **OQ-5 — Full environment isolation.** Personas still inherit the operator's
  user-global Claude Code plugins/hooks and codex's global `~/.codex/AGENTS.md`
  (D9). Robust fix: spawn each persona with an isolated config dir (auth copied in,
  no plugins/hooks/global docs). Would make codex personas as clean as claude in
  all cases. Not built.
- **OQ-4 — Attribution is forgeable** (D3). Accepted for single-user; revisit for
  multi-user/untrusted input.
- *(OQ-2 — the per-persona context indicator — is handled: both parsers derive
  `Usage.context_tokens`. Treat as resolved-by-proxy.)*

## 5. Backlog (suggested order)

**Verification (do first, cheap, high-value):**
1. Run `pytest -m live` and a manual UI pass; resolve OQ-1 and OQ-3. Adjust the
   adapter/parser if reality differs (reality wins; keep wire-format knowledge in
   the parser).

**Robustness / polish:**
2. OQ-5: isolated per-persona config dir (kills the last global-config leakage for
   both providers).
3. Codex `/compact` (and any clear/compaction) once a real mechanism is confirmed;
   then wire `send_command`'s argv via the shared common-flags helper (already
   stubbed) and surface it in the console.
4. UI: the codex console already disables `/compact`,`/clear` for codex personas;
   revisit when OQ-1 resolves.

**Features (none required; see goal.md non-goals before building):**
5. 1:1 DM rooms; richer markdown (keep it XSS-safe — text nodes only); message
   search/pagination-in-SQL; export a transcript.
6. Per-persona context/usage meter UI from the `Usage` events (data already flows).

**Known deliberate non-goals** (don't build without a reason): multi-human login,
API-key/per-token mode, cloud hosting, mobile, autonomous/background persona
activity. See [`../goal.md`](../goal.md).

## 6. Get oriented in ~15 minutes

1. Read [`../goal.md`](../goal.md) (5 min) — what & why.
2. Skim [`ARCHITECTURE.md`](ARCHITECTURE.md) §1–§6 (5 min) — the one idea, layers,
   the message-flow walkthrough, invariants.
3. Skim [`DECISIONS.md`](DECISIONS.md) (5 min) — so you don't break a hard-won fix.
4. Run it token-free: `python -m scripts.dev_mock`, open `http://127.0.0.1:8000`,
   send a message to `@arch`, watch it stream.
5. Read `app/services/orchestrator.py` (the keystone) and `app/harness/mock.py`
   (how tests work).

Then make your change **test-first** (AGENTS.md §5), keep the gates green, and
verify against the real CLIs with `pytest -m live` if you touched a harness.

## 7. Operating it

- **Run:** `python -m app.main` → `http://127.0.0.1:8000`. Config via `TEAM_*` env
  (`TEAM_PORT`, `TEAM_DATA_DIR`, `TEAM_CLAUDE_BIN`, `TEAM_CODEX_BIN`,
  `TEAM_HARNESS_TIMEOUT` — per-run wall-clock cap in seconds, default `36000` = 10h).
- **State on disk:** SQLite at `data/team.db`; run logs at
  `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl`; persona scratch cwd under the
  system temp dir. (`dev_mock` uses `./data-dev`.)
- **Personas/rooms** are created in the UI (or via REST). A persona bound to a
  `working_dir` gets that repo's context; one with no working_dir runs isolated.
  **Changing a persona's provider or working_dir → reset its session** (UI console
  or `POST /rooms/{id}/personas/{pid}/reset-session`) so it doesn't try to resume a
  session from the old environment.
- **Error-finding playbook:** UI error card → its `run_id` → open
  `GET /runs/<run_id>/log` (or the JSONL file) → `run_record` row for the redacted
  command/exit code → reproduce in `MockHarness`. (AGENTS.md §7.)

## 8. Environment notes (host machine, not the repo)

These are about the operator's `~/.codex` / `~/.claude`, not Team's code — relevant
because personas inherit them (D9, OQ-5):

- **Fixed:** `~/.codex/AGENTS.md` had a stale superpowers *bootstrap* block pointing
  at a removed `superpowers-codex` command, which made **every** codex session
  error and added a noisy preamble to codex personas. Migrated per the project's
  `INSTALL.md`: removed the block (backup `~/.codex/AGENTS.md.bak.*`) and created
  the native-discovery symlink `~/.agents/skills/superpowers → ~/.codex/superpowers/skills`.
  Also renamed the deprecated `[features].codex_hooks` → `[features].hooks` in
  `~/.codex/config.toml` (backup `~/.codex/config.toml.bak.*`). Codex now runs
  warning/error-free.
- **Still present (OQ-5):** any other user-global plugins/hooks/AGENTS still load
  for persona children; the durable fix is the isolated-config-dir approach.
