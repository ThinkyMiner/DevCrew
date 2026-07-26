# Plan: bounded multi-round deliberation + an eval system that actually gates quality

**Date:** 2026-07-20 · **Status:** implemented (see DECISIONS D14, FR-M7, HANDOFF §1a)
**Inputs:** operator brief; adversarial design review by Codex `gpt-5.6-sol` (xhigh) over this
repo; literature/industry survey (Anthropic multi-agent + context-engineering + evals posts,
AutoGen/LangGraph/OpenAI-SDK termination models, Du et al. debate, 2025-26 sycophancy work,
τ-bench pass^k, MultiAgentBench dimensions — full citations in the eval README).

## 1. Context — why

Personas can already delegate (`@handle` in a reply schedules that persona once — D13), but the
**once-per-persona-per-post cycle guard makes conversation impossible by construction**: A asks B,
B answers, A can never respond. The operator wants agents that genuinely work a task together —
back-and-forth that *converges* — while (a) never looping/runaway-spending, (b) reading like a
human team, not protocol robots, (c) not bloating context (the #1 bottleneck), and (d) with a real
eval system wired into CI so quality is measured on every change.

Research anchors that shaped the shape:
- Multi-agent ≈ **15×** chat tokens; token spend explains ~80 % of perf variance (Anthropic) —
  budgets are the design center, not an afterthought.
- Debate accuracy **plateaus at 2–3 rounds / 2–4 agents**; more is waste.
- Universal termination pattern: **deterministic hard stop OR semantic soft stop** — never rely on
  model compliance (a marker) for termination.
- **Sycophancy/rubber-stamping is the dominant failure mode**; centralized synthesis closes are
  robust to it; identity cues make it worse; "agreement must add substance" prompting helps.
- Ceiling-hit vs natural close must be **distinct recorded outcomes** (cheap, load-bearing eval
  signal).

## 2. Decisions (agreed in the Claude↔Codex review)

1. **No second agent loop** — everything still flows through `_run_turn` / resumable sessions /
   delta injection (AGENTS §2). The MCP "ask_teammate tool" idea stays rejected.
2. **Termination is mechanical, never model-dependent.** A single **decreasing autonomous-run
   budget** (`AUTONOMOUS_RUN_CAP = 6`, consumed by every *attempted* autonomous turn, including
   errors/empty replies) + a **wave cap** (`DELIBERATION_WAVE_CAP = 3`, latency/shape bound) +
   **causal eligibility**: a persona is eligible only when a successful message mentions it *after*
   its own most recent turn this post; pending requests are **coalesced per target**. Quiescence
   (no new eligible mentions) ends the deliberation naturally.
3. **Reserved closing slot.** One budget slot is reserved for a synthesis turn: advisors may spend
   at most `CAP-1`. The closer gets a no-more-delegation nudge (mentions in the closing reply are
   not honored). Guarantee holds "while the post remains active" (client disconnect still aborts
   via the existing `aclose()` chain — unchanged).
4. **Ownership MVP:** exactly one direct non-dispatcher target ⇒ that persona owns the close.
   Sole-direct-target **dispatcher (@systemd) ⇒ dispatcher takes the closing slot as a
   role-consistent wrap-up** (summarize recommendation/trade-off/dissent/risks — it dispatched the
   work; it reaps the results — Codex-approved with conditions: only when it successfully
   dispatched ≥1 teammate; the closing nudge prohibits new mentions; consumes the reserved slot).
   Multi-target posts: bounded conversation, quiescence, no claimed owner.
5. **No `DECISION:` protocol in V1.** The closer's natural final reply *is* the decision. No
   marker parsing, no `[no-decision]` magic messages, no new Message kind. Machine-extractable
   decisions are a later, typed feature if evidence demands it.
6. **No presets in V1.** One fixed conservative policy (cap 6 / waves 3 / reserve 1), constants in
   `orchestrator.py` next to the old ones. `Room.delegation_enabled` remains the master toggle.
   Presets return only if live measurement justifies them.
7. **Prompt semantics overhaul — the highest-risk fix.** `@handle` becomes *"request a turn"*;
   plain names for reference/credit. The seeded line "Build on teammates by @handle when you
   agree" is removed (it would turn polite agreement into paid turns). New roster text requires
   every reply to add evidence, rebut a specific point, or concede with a reason — no ceremonial
   endorsement; never `@everyone`. Dynamic state ("wave 2, four runs remain", closing nudges) goes
   in the per-turn directed nudge, not the system suffix.
8. **Context bloat:** (i) the budget/wave caps are the primary control; (ii) advisors are prompted
   to one concrete contribution (~150 words); (iii) **stop re-injecting a persona's own past
   messages in `render_delta`** — its resumed session already remembers them — *except* its own
   `[error: …]` markers (a failed turn may not exist in the session at all; hiding the marker
   would violate fail-loud). Documented delta semantics change ⇒ docs updated in the same commit.
   No auto-compact, no hidden summaries, no fabricated last-seen advancement.
9. **Fix-first bug (pre-existing):** after a delegated run errors, `_latest_reply()` can return
   that persona's *older* successful reply and continue delegation from stale content. The
   scheduler must observe the exact attempted turn's outcome (typed `TurnOutcome`), not re-scan
   the transcript.
10. **Scheduler is a pure module** (`app/services/deliberation.py`): budget, waves, coalescing,
    eligibility, reserved close — no I/O, fully unit-testable. `ChatOrchestrator` executes the
    turns it requests.

## 3. Implementation map

| Change | Where |
|---|---|
| Pure scheduler (budget/waves/coalescing/eligibility/reserved close, typed `TurnOutcome`, `DeliberationSummary` w/ `termination_reason`) | `app/services/deliberation.py` (new) |
| Drive scheduler; delete depth/once-per-post guard; wire dispatcher/owner close; log summary | `app/services/orchestrator.py` (`_run_delegations` → `_run_deliberation`) |
| Own-message delta filtering (error-marker exception) | `app/services/transcript.py` (`render_delta`/`delta_messages` callers), orchestrator docstring |
| Roster/protocol text + per-turn nudges | `orchestrator.py` (`_roster_note`, `_run_delegated_turn`), `app/services/persona_seed.py` (teammate + dispatcher prompts) |
| `script_replies(substring, *texts)` ordered queue; exhaustion raises; `script_runs` for event/exception queues | `app/harness/mock.py` |
| Tests (failing-first) | `tests/unit/services/test_deliberation.py` (new), `test_delegation.py` (updated semantics), `tests/unit/harness/test_mock.py`, `tests/unit/services/test_transcript.py` |
| Scenario sims (deterministic, per-PR) | `tests/sims/` (runs in the default pytest suite) |
| Live eval harness + rubric judge | `evals/` (runner, scenarios JSONL, judge prompts, README) |
| CI | `.github/workflows/ci.yml` (new — repo has none) |
| Docs | `DECISIONS.md` D14, `PRD.md` FR-M7 + NFR-EV1..3, `HANDOFF.md`, `AGENTS.md` §6 commands |

## 4. Eval architecture (three layers, distinct purposes)

**Tier 1 — deterministic invariants (per-PR, blocking, zero tokens).** pytest on MockHarness:
A→B→A works; A↔B exhausts at exactly the cap; every attempt (incl. errors) spends budget; the
reserved close survives fan-out pressure; coalescing (N mentions ⇒ 1 turn); mention-before-turn
doesn't double-schedule, mention-after-turn re-schedules; `@everyone`/self/unknown never schedule;
stale-reply bug reproduced + fixed; own-message filtering (with error-marker exception); each
transcript message appears ≤ once in any receiving prompt; aggregate prompt chars for fixed
scenarios under a checked threshold; `aclose()` chain still reaps in-flight turns.

**Tier 2 — scripted scenario sims (per-PR, blocking, zero tokens).** Full orchestrator + temp
SQLite + scripted multi-round personas, asserting transcript shape: constructive-challenge,
qualified-agreement, fan-out-and-synthesis, rubber-stamp-trap (planted error must be challenged —
scripted), budget-pressure forced close, advisor-failure recovery, dispatcher wrap-up.

**Tier 3 — live LLM-judged evals (nightly / manual dispatch, non-blocking until calibrated).**
Fresh transcripts generated from the checked-out commit against pinned scenarios (recorded
transcripts only calibrate the judge — they can't detect code regressions). Judge = claude CLI
headless, **different model family than the debaters where possible, isolated call per dimension,
speakers anonymized, "Unknown" allowed, cited message IDs required.** Dimensions: role adherence;
substantive contribution; responsiveness to the preceding argument; epistemic independence
(planted-error challenge); decision quality/actionability; synthesis fidelity; turn efficiency;
naturalness/protocol leakage; unnecessary delegation; rubber-stamp classification. Mechanical
metrics computed alongside (termination_reason distribution, waves used, tokens/turns per close,
redundant re-triggers). **Repeated pass@1** (the product runs one conversation — no best-of-k);
hard safety invariants must pass every sample; quality scores reported vs a pinned baseline
(blinded pairwise for regressions) and gate only after human calibration.

**CI:** `ci.yml` runs pytest (unit+integration+sims), `ruff check`, `ruff format --check`,
`mypy app`, `node --test tests/js/*.test.mjs` on every push/PR. Live tier runs on
`workflow_dispatch` + nightly cron **only where authenticated CLIs exist** (self-hosted/local;
hosted runners have no subscription logins) with an explicit token budget guard.

## 5. Verification

- TDD throughout: each behavior lands as a failing test first (AGENTS §5).
- Done = `pytest` + `ruff check` + `ruff format --check` + `mypy app` green, docs updated (§9).
- Live smoke (manual, real CLIs): one deliberation in a real room; observe wave nudges, close,
  spend; run `evals/` tier 3 once to produce the first baseline report.

## 6. Deferred (explicitly, with reasons)

Presets (measure first) · typed DECISION extraction (protocol-leakage risk; needs evidence) ·
Message.kind/status rows (schema cost; no V1 consumer) · auto-`/compact` (unverified live;
provider-dependent) · deliberation branch graphs/aggregates (no consumer) · MockHarness callback
DSL (tests must stay auditable).
