# Team — Product Requirements Document

- **Status:** Implemented & merged (feature-complete vs the requirements below)
- **Owner:** Kartik
- **Last updated:** 2026-06-04
- **Related:** [`goal.md`](../goal.md), [`HANDOFF.md`](HANDOFF.md) (status/backlog),
  [`ARCHITECTURE.md`](ARCHITECTURE.md) (code map), [`DECISIONS.md`](DECISIONS.md)
  (rationale), [design doc](plans/2026-06-01-team-chat-design.md),
  [`CLAUDE.md`](../CLAUDE.md), [`AGENTS.md`](../AGENTS.md)

---

## 1. Summary

Team is a locally-run, Python web application that presents a Slack-style group
chat between one human operator and a set of configurable AI personas. Each
persona is backed by a real CLI harness — the Claude Code CLI or the Codex CLI —
running as a managed subprocess. The harness owns each persona's context,
thinking, tool use, and MCP servers and provides a resumable session; Team
orchestrates these sessions into one shared, durable, fully observable
conversation.

## 2. Goals & success criteria

See [`goal.md`](../goal.md) for the vision and the seven success criteria. This
PRD translates them into concrete requirements.

## 3. Personas, providers, and authors — definitions

| Term | Meaning |
| --- | --- |
| **Operator** | The single human user (you). |
| **Human author** | An attribution identity the operator types as. Defaults: **Me**, **Boss**. Extensible. Not a login. |
| **Persona** | An AI participant backed by one harness session. Has a handle (`@architect`), model, provider, personality, tools. |
| **Provider** | The harness behind a persona: `claude` (Claude Code CLI) or `codex` (Codex CLI). |
| **Room** | A conversation (Slack channel). Has its own transcript and member personas. |
| **Run** | One harness invocation that produces one persona reply. Always logged. |

## 4. Functional requirements

### 4.1 Rooms

- **FR-R1** Create, rename, archive, and delete rooms.
- **FR-R2** Each room has a topic/description and an ordered set of member personas.
- **FR-R3** Each room stores its own transcript; contexts never leak between rooms.
- **FR-R4** A room has a default multi-reply mode (sequential / parallel) that the
  per-message toggle can override.

### 4.2 Personas

- **FR-P1** Create, edit, duplicate, and delete personas. Editing a persona never
  destroys its existing sessions unless the operator explicitly resets them.
- **FR-P2** A persona has: display name, unique handle (`@handle`), a short
  **job** label (role descriptor shown next to the name in chat, e.g. "System
  architect"), color/avatar, provider (`claude`|`codex`), model, effort/thinking
  level, system prompt (personality + role), MCP server set, allowed tools,
  optional working directory, and permission mode (`read-only` | `ask` | `auto`).
  The editor presents model/effort as dropdowns (model is typeable for
  forward-compat) and allowed-tools/MCP as multi-select pickers.
- **FR-P3** Persona templates: save a configured persona as a reusable template.
- **FR-P4** A persona may belong to multiple rooms; it holds a **separate session
  per room** (see FR-S1).

### 4.3 Human authors

- **FR-A1** The composer has a "Writing as" selector. Defaults: **Me**, **Boss**.
- **FR-A2** Authors are editable in settings: name, color, and an optional **weight
  note** injected into persona context when that author speaks.
- **FR-A3** The Boss author ships with a default, editable weight note that
  instructs personas to treat its input as high-weight leadership direction. The
  weight note is **on by default** per author and can be toggled/edited.

### 4.4 Messaging & routing

- **FR-M1** The operator posts a message as the selected author into a room.
- **FR-M2** Tagging: `@handle` directs a message at one or more personas; `@everyone`
  targets all room members.
- **FR-M3** Only tagged personas respond. If no persona is tagged, no run starts
  (the message is recorded as context only).
- **FR-M4** Personas share the transcript via **delta injection**: when a persona is
  invoked, it receives the attributed messages it has not yet seen since it last
  spoke in that room.
- **FR-M5** **Quote-reply**: the operator can quote one or more earlier messages.
  Quoted messages are injected into the tagged personas' context explicitly marked
  as quotes, regardless of whether those personas had seen them.
- **FR-M6** **Persona-to-persona delegation** (per-room toggle, `delegation_enabled`,
  default on): when a persona's reply `@`-mentions another persona, that persona is
  given a turn (seeing the delegating message via the normal delta). A mentioned
  non-member is **auto-added** to the room first. Bounded by the orchestrator's caps
  — delegation **depth** ≤ 2, ≤ **6** delegated turns per operator post, and each
  persona runs **at most once per post** (cycle guard) — so chains always terminate.
  When enabled, each run's system prompt carries the live teammate roster (handles +
  jobs). No second agent loop: delegation reuses the same `@`-routing and turn
  machinery as operator posts. See [`DECISIONS.md`](DECISIONS.md) D13.

### 4.5 Multi-reply behavior

- **FR-MR1** Per-message toggle: **Sequential** or **Parallel**.
- **FR-MR2** Sequential: tagged personas reply one after another; each later persona
  sees earlier personas' replies from the same turn (enables debate/validation).
- **FR-MR3** Parallel: tagged personas reply concurrently; each sees the transcript
  only up to the operator's message, not same-turn sibling replies.

### 4.6 Session control & context management

- **FR-C1** Each persona exposes a session console supporting harness control
  commands — at minimum `/compact` and `/clear` — plus a raw-command field.
- **FR-C2** Each backend declares its `supported_commands`. Unsupported commands
  fail with a clear message; they never silently no-op.
- **FR-C3** A per-persona context/usage indicator (token or context-size signal,
  derived from the stream where the harness reports it) shows when a session is
  getting heavy.
- **FR-C4** Resetting a persona's session in a room starts a fresh harness session
  and clears the last-seen pointer (with confirmation).

### 4.7 Transparency & streaming

- **FR-T1** Persona replies stream token-by-token over WebSocket to the UI.
- **FR-T2** Thinking/reasoning blocks render inline, collapsible.
- **FR-T3** Tool and MCP calls render inline per persona, with their inputs and
  results.
- **FR-T4** Each message shows which run produced it and links to that run's log.

### 4.8 Durability & restart

- **FR-D1** Rooms, personas, authors, messages, quotes, per-room persona sessions
  (harness `session_id` + last-seen pointer), and run records persist in SQLite.
- **FR-D2** After an app restart, all of the above reload and any persona can resume
  its harness session exactly where it left off.
- **FR-D3** Every run is logged to `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl`
  containing the full harness stream.

### 4.9 Health & errors

- **FR-E1** On startup the app verifies `claude` and `codex` are on PATH and
  authenticated, surfacing a clear banner if not.
- **FR-E2** A nonzero harness exit (or timeout) surfaces a structured error card in
  the chat: human-readable cause, the redacted command, and a one-click link to the
  full run log. Never a silent failure.
- **FR-E3** Typed error taxonomy: `HarnessError`, `HarnessTimeout`,
  `HarnessAuthError`, `SessionNotFound`, `ProviderUnavailable`.

## 5. Non-functional requirements

- **NFR-1 Local-only.** Binds to localhost; no external services beyond the CLIs.
- **NFR-2 Minimal dependencies.** Backend: FastAPI, uvicorn, Pydantic v2. Storage:
  stdlib `sqlite3` behind a typed repository layer (no heavy ORM — avoids Python
  3.14 wheel gaps). Frontend: buildless vanilla JS + WebSocket + CSS (no Node).
- **NFR-3 Observability.** Structured JSON logs with a `run_id` correlation thread
  through every harness call.
- **NFR-4 Testability.** A `MockHarness` backend lets the full app be tested
  end-to-end without spending tokens or requiring the real CLIs. TDD throughout.
- **NFR-5 Debuggability.** Layered architecture, typed boundaries (Pydantic), and
  the "fail loud, fail located" rule make any error fast to trace.
- **NFR-6 Python 3.12+** (developed against the local 3.14).

## 6. Architecture overview

```
app/
├── domain/        Pydantic models: Persona, Room, Message, HumanAuthor,
│                    PersonaSession, RunRecord, StreamEvent
├── harness/       AgentBackend protocol + ClaudeHarness, CodexHarness, MockHarness
│                    (subprocess spawn, stream-json parsing, session capture/resume,
│                     control commands, capability declaration)
├── services/      ChatOrchestrator (routing, sequential/parallel, delta + quote
│                    injection, Boss weighting), RoomService, PersonaService,
│                    AuthorService, SessionStore
├── persistence/   SQLite repositories + on-disk run-log store
├── api/           FastAPI routes + WebSocket streaming endpoint
├── web/           Buildless frontend (ES modules, WebSocket, CSS)
├── config/        pydantic-settings, paths, startup health checks
└── main.py        entrypoint
```

The defining design choice — **one persona = one resumable harness session per
room**, with a shared transcript reconstructed via delta + quote injection — is
detailed in the [design doc](plans/2026-06-01-team-chat-design.md).

## 7. Data model (SQLite)

- **persona** — id, name, handle, color, job, provider, model, effort,
  system_prompt, mcp_servers (json), allowed_tools (json), working_dir,
  permission_mode, is_template, timestamps. (`job` added via an idempotent
  additive migration in `Database._migrate` for pre-existing DBs.)
- **human_author** — id, name, color, weight_note, weight_enabled.
- **room** — id, name, topic, default_reply_mode, delegation_enabled, archived,
  timestamps. (`delegation_enabled` added via the additive migration in
  `Database._migrate`; default 1/on.)
- **room_persona** — room_id, persona_id, order. (membership)
- **message** — id, room_id, author_kind (`human`|`persona`), author_ref (author or
  persona id), content, created_at, run_id (nullable).
- **message_quote** — message_id, quoted_message_id. (quote-reply links)
- **persona_session** — room_id, persona_id, harness_session_id, last_seen_message_id,
  provider, status, timestamps. (the resume + delta anchor)
- **run_record** — run_id, room_id, persona_id, command_redacted, exit_code,
  started_at, finished_at, usage (json), log_path, error_kind (nullable).

## 8. Message flow

1. Operator posts a message (as Me/Boss), with optional tags and quotes.
2. Message persisted; tagged personas resolved.
3. For each persona, the orchestrator builds the prompt: attributed delta since the
   persona's last-seen pointer + any explicitly quoted messages + Boss weight note
   when applicable.
4. The harness adapter spawns or resumes the persona's CLI session in stream-json
   mode with the persona's model, effort, system prompt, MCP config, working dir,
   and permission mode.
5. Stream events (text / thinking / tool_use / tool_result / usage) are pushed over
   WebSocket and persisted; the new harness `session_id` and last-seen pointer are
   saved.
6. A `RunRecord` is written with the redacted command, exit code, usage, and log
   path. On failure, an error card is emitted (FR-E2).
7. Sequential mode: the next persona's delta now includes the prior persona's reply.

## 9. Open questions

> Consolidated and kept current in [`HANDOFF.md`](HANDOFF.md) §4–§5 (with code
> `TODO(OQ-*)` markers). Status as of 2026-06-04:

- **OQ-1 — OPEN.** Real `/compact` & `/clear` behavior in headless mode. Built
  behind `supported_commands` and fails loud when unsupported (FR-C2). Claude's
  `send_command` resumes with the slash command as the prompt (unverified it
  compacts); Codex declares no compaction command. Verify via `pytest -m live`.
- **OQ-2 — RESOLVED (by proxy).** Both parsers derive `Usage.context_tokens` from
  the CLIs' usage events, powering the FR-C3 indicator. Not an authoritative
  context-window figure, but a usable signal.
- **OQ-3 — PARTIALLY OPEN.** Codex `codex exec --json` happy-path schema
  (`agent_message`/`command_execution`/`usage`/`thread_id`) is live-verified and
  normalized into `StreamEvent`; the `reasoning`/`error`/`mcp_tool_call` shapes are
  best-effort (tolerant parsing) and whether `-c model_instructions=` applies the
  persona system prompt is unverified. Claude schema is verified.
- **OQ-4 — ACCEPTED LIMITATION.** Attribution lines in the persona prompt are
  forgeable by message content (a body can contain a fake `[Boss → @x]:` line).
  Fine for the local single-user model; a structural delimiter/escape is the
  mitigation if multi-user or untrusted input is added.
- **OQ-5 — OPEN (new).** Personas inherit the operator's user-global Claude Code
  plugins/hooks and codex's global `~/.codex/AGENTS.md`. Per-persona environment
  isolation today is cwd-only (a neutral dir outside the repo); the durable fix is
  an isolated per-persona config dir with credentials copied in. See
  [`DECISIONS.md`](DECISIONS.md) D9.

## 10. Out of scope

Multi-human login, API-key/per-token mode, cloud hosting, mobile, autonomous/
background persona activity. See `goal.md` non-goals.
