"""Live evaluation harness for the deliberation system (Tier 3).

Tiering (see docs/plans/2026-07-20-deliberation-and-evals.md):

- Tier 1 (tests/unit/…): deterministic scheduler/orchestrator invariants.
- Tier 2 (tests/sims/…): deterministic scenario simulations, transcript shape.
- Tier 3 (this package): FRESH transcripts generated from the checked-out code
  with real CLIs, then graded — per dimension, in isolated calls — by an LLM
  judge, plus mechanical metrics. Recorded transcripts only calibrate the
  judge; detecting a code regression requires regenerating transcripts.

Run:
    python -m evals.run_live --mock          # pipeline health, zero tokens
    python -m evals.run_live                 # real CLIs (authenticated, local)
    python -m evals.judge <out-dir>          # grade the freshest run
"""
