# Design: Team — multi-persona group chat over CLI harnesses

- **Date:** 2026-06-01
- **Status:** Approved (brainstorming complete)
- **Supersedes:** n/a
- **Implementation plan:** to be created via writing-plans

This document captures the validated design produced during brainstorming. It is
the canonical "why it is shaped this way" reference; [`docs/PRD.md`](../PRD.md) is
the requirements view and [`goal.md`](../../goal.md) is the vision.

## Decisions locked during brainstorming

| # | Decision | Choice | Rationale |
| --- | --- | --- | --- |
| 1 | Engine | Wrap Claude Code CLI + Codex CLI as headless subprocesses | Reuse native context, resume, thinking, MCP, and subscription auth; satisfies "log harness sessions so we can restart" |
| 2 | Interface | FastAPI web app + buildless browser UI | Most "group chat like"; real-time streaming; no Node toolchain |
| 3 | Participants | One human operator + many AI personas | Matches "validate my ideas with personas" |
| 4 | Routing | `@`-tag specific personas; `@everyone`; shared transcript | Operator stays in control |
| 5 | Organization | Multiple rooms | Clean context separation, like Slack channels |
| 6 | File access | Per-persona optional working dir + permission mode | Some personas inspect code, others are pure advisors |
| 7 | Multi-reply | Per-message sequential/parallel toggle | Debate when wanted, speed when not |
| 8 | Transparency | Full: live tokens, collapsible thinking, inline tool calls | Trust + debuggability |
| 9 | Quote-reply | Quote earlier messages into tagged personas' context | Deliberate, visible context injection |
| 10 | Authors | Human authors Me/Boss (extensible), Boss weighted | Weight leadership input correctly |
| 11 | Session control | Per-persona `/compact`, `/clear`, raw commands + usage indicator | Manage context bloat explicitly |

## The central idea: one persona = one resumable harness session per room

A persona is not a prompt we replay; it is a **long-lived harness session**. Creating
`@architect` in a room starts a `claude`/`codex` session. Each time the persona
speaks again, we **resume that same session** (`claude --resume <session_id>`), so
the harness owns the persona's private context, thinking, and MCP state. We never
rebuild the agent loop.

Because each persona keeps its own per-room session, a persona used in two rooms has
two independent contexts — matching the "rooms separate context" decision.

## Shared transcript via delta + quote injection

Each `(room, persona)` has a **last-seen-message pointer**. When a persona is
invoked, we build its prompt from:

1. **Delta** — the attributed messages it has not yet seen since it last spoke:
   ```
   [Kartik]: Here's what my boss said: "..."
   [@researcher]: I found three relevant papers...
   [Kartik → @architect]: Does this approach hold up?
   ```
2. **Quotes** — any messages the operator explicitly quoted, marked as quotes, even
   if previously seen:
   ```
   [Kartik → @architect] quoting [@researcher]:
     > "LISTEN/NOTIFY won't scale past ~8k subscribers"
     Given that, is our pub/sub design still valid?
   ```
3. **Author weighting** — when the speaking author is Boss (and weighting is on),
   prepend the author's weight note.

This keeps each persona's harness context lean (it only ever ingests what it is
shown), makes sequential debate correct (a later persona's delta includes the
earlier persona's same-turn reply), and is fully reconstructable after a restart
from SQLite.

## Sequential vs parallel

- **Sequential**: invoke tagged personas in order; after each reply, advance the
  shared transcript so the next persona's delta includes it. Real cross-talk.
- **Parallel**: snapshot the transcript at the operator's message; invoke all tagged
  personas against that snapshot concurrently; no same-turn sibling visibility.

The toggle is per message; the room carries a default.

## Backend abstraction

```python
class AgentBackend(Protocol):
    name: str
    supported_commands: set[str]

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]: ...
    async def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]: ...
```

- `ClaudeHarness` — `claude -p --output-format stream-json --resume <id>
  --append-system-prompt ... --model ... --effort ... --mcp-config ...
  --add-dir ... --permission-mode ...`; captures the emitted `session_id`.
- `CodexHarness` — `codex exec` with resume; normalizes Codex's event schema into
  the shared `StreamEvent`.
- `MockHarness` — scripted deterministic streams for tests; declares the same
  capabilities. Lets the whole app run with no real CLI and no token spend.

All three emit the same normalized `StreamEvent` union (text delta, thinking,
tool_use, tool_result, usage, error, done). Normalization lives in `domain/`.

## Persistence

- **SQLite** (stdlib `sqlite3` + a thin typed repository layer) for all structured
  state — schema in PRD §7. Deliberately no heavy ORM: avoids Python 3.14 wheel gaps
  and keeps queries inspectable.
- **On-disk run logs** — full harness stream per run at
  `data/logs/<room>/<persona>/<ts>-<run_id>.jsonl`. The DB's `run_record.log_path`
  points at it; the UI links to it.
- Harness-native session files (`~/.claude/...`, Codex's store) remain the source of
  truth for context; we store only the `session_id` needed to resume.

## Error handling — "fail loud, fail located"

- Typed taxonomy: `HarnessError`, `HarnessTimeout`, `HarnessAuthError`,
  `SessionNotFound`, `ProviderUnavailable`.
- Every subprocess run captures exit code + stderr. Nonzero/timeout → a structured
  error card in chat with the redacted command and a link to the run log. No silent
  fallbacks.
- Startup health check verifies both CLIs are present and authenticated.
- A `run_id` correlation id threads through logs, the error card, and the log file
  name, so any symptom leads to its run in one hop.

## Testing strategy (TDD)

- **Unit**: orchestrator routing (tags, `@everyone`, untagged → no run), delta
  builder, quote injection, Boss weighting, stream-json parsers (recorded fixtures).
- **Integration**: full message → reply flow against `MockHarness`, including
  sequential cross-talk and parallel snapshotting, plus restart/resume.
- **Contract**: each real backend parsed against captured fixture streams so schema
  drift is caught without live calls.

## Tech stack

- Python 3.12+ (local 3.14). FastAPI + uvicorn, Pydantic v2, asyncio subprocess.
- stdlib `sqlite3` + typed repos. Buildless vanilla-JS frontend over WebSocket.
- stdlib `logging` → structured JSON file + readable console.
- Auth via existing CLI subscriptions; no API keys.

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Headless `/compact` mechanics differ per CLI | `supported_commands` capability; graceful degrade (OQ-1) |
| Codex vs Claude stream schema drift | Normalize to one `StreamEvent`; contract tests on fixtures |
| Python 3.14 dependency wheels missing | Minimal deps; stdlib sqlite3; buildless frontend |
| Long-lived subprocesses leak | SessionStore lifecycle + health checks + explicit reset |

## Next step

Invoke the writing-plans skill to turn this into a sequenced, test-first
implementation plan.
