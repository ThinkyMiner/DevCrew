# Deliberation evals

How we know the multi-agent deliberation system (D14) works — and keeps
working — on every change. Three tiers, cheapest first; each tier catches what
the one below it can't.

| Tier | What | When | Cost | Gate |
|---|---|---|---|---|
| 1 | Deterministic scheduler/orchestrator invariants (`tests/unit/services/test_deliberation.py`) | every PR | zero | blocking |
| 2 | Scenario simulations: transcript shape + context-budget ceilings (`tests/sims/`) | every PR | zero | blocking |
| 3 | Fresh live transcripts + per-dimension LLM judge (`evals/`) | nightly / manual | real tokens | report-only until calibrated |

**Why tier 3 regenerates transcripts:** a fixed recorded transcript can only
calibrate the judge — it cannot detect a regression in the *current* code.
Behavior evals must run the checked-out commit against the pinned scenarios.

**Hard invariants never reach the judge.** Turn-count caps are checked
mechanically in `run_live.py` and fail the run outright; a quality score can
never launder a safety violation. This is the regression/capability split:
the safety suite is expected at 100 % (any failure blocks); quality scores are
tracked against a baseline.

## Running

```bash
python -m evals.run_live --mock            # pipeline health, zero tokens (also in CI)
python -m evals.run_live                   # real CLIs — local, authenticated
python -m evals.judge evals/out/<stamp>    # grade + write report.{json,md}
python -m evals.judge evals/out/<stamp> --trials 3 --gate   # nightly form
```

`--judge-cmd` swaps the judge (default `claude --print --model sonnet`); tests
use it to inject a stub, and you can point it at a different model family to
reduce self-preference bias.

## Judged dimensions

Each dimension is graded by an **isolated** judge call (1–5, absolute rubric,
"unknown" escape hatch, cited message indices, anonymized speakers):

1. **role_adherence** — personas stay in-lane, defer explicitly.
2. **substantive_contribution** — every reply adds evidence/consequence/
   sharper formulation; ceremony scores low.
3. **responsiveness** — replies engage the specific preceding argument.
4. **epistemic_independence** — planted false premises get challenged
   (only graded on scenarios with `planted_error`).
5. **decision_quality** — the close is concrete, grounded, names the residual
   risk.
6. **synthesis_fidelity** — the close weighs the discussion's inputs
   (only graded when ≥3 persona turns).
7. **naturalness** — no protocol leakage (waves/budgets/boilerplate).
8. **unnecessary_delegation** — every @-mention earned its cost.

Deliberately NOT a dimension: raw disagreement rate. Good teams often agree;
what we measure is whether agreement adds substance and whether planted errors
get challenged.

Mechanical metrics recorded alongside (no judge): persona turns vs ceiling,
error turns, distinct speakers, tokens in/out.

## Calibration protocol (before `--gate` becomes blocking)

1. Collect ~20 real transcripts (the `evals/out/` runs accumulate them).
2. Human-label them on 3–4 dimensions.
3. Run the judge on the same transcripts; measure judge↔human agreement.
4. Fix the rubric (the only lever) until agreement is acceptable; re-check for
   the known biases: verbosity preference, position effects, self-preference.
5. Write `evals/baseline.json` (dimension → mean from a good run), then turn on
   `--gate` in the nightly job. Use multiple `--trials`; termination-style
   guarantees should be judged by "all trials pass" (pass^k), quality means by
   threshold-vs-baseline.

## Growing the scenario set

Add a `Scenario` to `evals/scenarios.py` whenever a real room misbehaves —
distill the failure, keep it small, give it `mock_scripts` so `--mock` stays
green. Start-small-and-grow-from-failures is the documented best practice; do
not bulk-generate synthetic scenarios.

## Sources

- Anthropic — [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  (grader taxonomy, outcome-over-path, isolated per-dimension judges, "read the transcripts")
- Anthropic — [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
  (~20-task start, rubric dimensions, 15× token economics)
- Anthropic — [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- τ-bench ([arXiv 2406.12045](https://arxiv.org/abs/2406.12045)) — pass^k reliability framing
- MultiAgentBench ([arXiv 2503.01935](https://arxiv.org/abs/2503.01935)) — collaboration dimensions
- Du et al., multiagent debate ([arXiv 2305.14325](https://arxiv.org/pdf/2305.14325)); Peacemaker or
  Troublemaker ([arXiv 2509.23055](https://arxiv.org/abs/2509.23055)) — sycophancy as the dominant
  failure mode; centralized closes robust to it
- When Identity Skews Debate ([arXiv 2510.07517](https://arxiv.org/html/2510.07517)) — anonymize speakers
- Evidently — [LLM-as-a-judge guide](https://www.evidentlyai.com/llm-guide/llm-as-a-judge) (bias mitigations)
