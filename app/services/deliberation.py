"""Pure deliberation scheduling: who speaks next, and when the talking stops.

This module is the D14 replacement for D13's once-per-persona-per-post guard.
It decides *which* persona turns a post's deliberation should run — the
orchestrator executes them through its existing ``_run_turn`` machinery. No
I/O, no models resolved, no prompts built: persona ids in, persona ids out,
so every termination property is unit-testable without a database or harness.

Termination is mechanical, never model-dependent (a marker an LLM may or may
not emit can *shorten* a deliberation, but nothing an LLM does can lengthen
one past the caps):

* **Budget** — one decreasing autonomous-run budget. Every turn this scheduler
  issues consumes budget when issued (attempts count: errors and empty replies
  spend the same slot a success would). The budget never increases; no code
  path — including the forced close — bypasses it, which is the whole
  termination proof.
* **Waves** — a latency/shape bound. Mentions produced during wave N schedule
  wave N+1; at most ``wave_cap`` waves are issued.
* **Causal eligibility** — a persona becomes pending only when a *successful*
  reply mentions it after its own most recent turn this post. Requests are
  coalesced per target: one turn satisfies every mention that preceded it,
  including mentions of an initial target whose wave-0 turn has not run yet.
* **Reserved close** — when the post has a closer (its sole direct target),
  one budget slot is held back so advisors can never starve the synthesis
  turn. If the closer's own last successful reply already ended the
  conversation (no honored mentions), that close was *natural* and the
  reserve goes unused.

Quiescence — a wave that produces no new eligible mentions — is the normal,
human way a deliberation ends.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class TerminationReason(StrEnum):
    """Why no further advisor turns were issued (distinct recorded outcomes —
    a ceiling-hit must never masquerade as a natural finish)."""

    QUIESCENT = "quiescent"
    BUDGET = "budget"
    WAVE_CAP = "wave_cap"


class CloseKind(StrEnum):
    NONE = "none"  # nothing was delegated; the initial reply stands
    NATURAL = "natural"  # closer's own last reply already concluded it
    FORCED = "forced"  # the reserved closing turn was spent


class TurnOutcome(BaseModel):
    """What actually happened on one attempted turn, reported by the executor.

    ``mention_ids`` are the already-resolved, honored delegation targets
    (self/@everyone/unknown handles filtered upstream). They must be empty
    when ``ok`` is False — a failed turn has no frontier.
    """

    persona_id: str
    ok: bool
    mention_ids: tuple[str, ...] = ()


class DeliberationSummary(BaseModel):
    termination_reason: TerminationReason
    close: CloseKind
    waves_used: int
    runs_used: int


class DeliberationScheduler:
    """Stateful but pure: feed it ``TurnOutcome``s, ask it for the next wave.

    Protocol (enforced order):
    1. ``observe(outcome)`` once per initial (wave-0, operator-routed) turn.
    2. Loop: ``next_wave()`` -> run each returned persona -> ``observe`` each.
       An empty wave means advisors are done.
    3. ``closing_turn()`` -> if a persona id is returned, run its closing turn
       (mentions in that reply are NOT honored) and report via
       ``observe_close``.
    4. ``summary()``.
    """

    def __init__(
        self,
        *,
        budget: int,
        wave_cap: int,
        closer_id: str | None,
        initial_targets: tuple[str, ...],
    ) -> None:
        self._budget = budget
        self._wave_cap = wave_cap
        self._closer_id = closer_id
        # Initial targets' wave-0 turns are scheduled by routing, not by us,
        # but a mention of one that hasn't run yet is satisfied by that turn.
        self._awaiting_turn: set[str] = set(initial_targets)
        self._pending: dict[str, None] = {}  # insertion-ordered set of target ids
        self._waves_used = 0
        self._runs_used = 0
        self._delegation_happened = False
        self._last_ok_speaker: str | None = None
        self._last_ok_had_mentions = False
        self._close: CloseKind = CloseKind.NONE
        self._stop_reason: TerminationReason | None = None

    # ------------------------------------------------------------------ #
    # observations
    # ------------------------------------------------------------------ #

    def observe(self, outcome: TurnOutcome) -> None:
        """Record an attempted turn (wave-0 or advisor) and its honored mentions."""
        self._awaiting_turn.discard(outcome.persona_id)
        self._pending.pop(outcome.persona_id, None)  # its turn satisfies pending requests
        if not outcome.ok:
            return
        self._last_ok_speaker = outcome.persona_id
        honored = [
            pid
            for pid in outcome.mention_ids
            if pid != outcome.persona_id and pid not in self._awaiting_turn
        ]
        self._last_ok_had_mentions = bool(honored)
        for pid in honored:
            self._pending.setdefault(pid, None)

    def observe_close(self, outcome: TurnOutcome) -> None:
        """Record the forced closing turn (its mentions are never honored)."""
        self._awaiting_turn.discard(outcome.persona_id)

    # ------------------------------------------------------------------ #
    # scheduling
    # ------------------------------------------------------------------ #

    @property
    def wave_number(self) -> int:
        """The 1-based number of the most recently issued wave."""
        return self._waves_used

    @property
    def runs_remaining(self) -> int:
        return self._budget

    def next_wave(self) -> list[str]:
        """Issue the next advisor wave (consuming budget), or ``[]`` when done."""
        if not self._pending:
            self._stop_reason = TerminationReason.QUIESCENT
            return []
        if self._waves_used >= self._wave_cap:
            self._stop_reason = TerminationReason.WAVE_CAP
            return []
        available = self._budget - self._reserve()
        if available <= 0:
            self._stop_reason = TerminationReason.BUDGET
            return []
        wave = list(self._pending)[:available]
        for pid in wave:
            del self._pending[pid]
        self._budget -= len(wave)
        self._runs_used += len(wave)
        self._waves_used += 1
        self._delegation_happened = True
        return wave

    def closing_turn(self) -> str | None:
        """The persona that should take the reserved closing turn, if any.

        ``None`` either because there is no closer, nothing was ever delegated
        (the initial reply stands), or the closer already closed naturally.
        """
        if self._closer_id is None or not self._delegation_happened:
            return None
        if self._last_ok_speaker == self._closer_id and not self._last_ok_had_mentions:
            self._close = CloseKind.NATURAL
            return None
        if self._budget <= 0:  # defensive: the reserve should have prevented this
            return None
        self._budget -= 1
        self._runs_used += 1
        self._close = CloseKind.FORCED
        return self._closer_id

    def summary(self) -> DeliberationSummary:
        return DeliberationSummary(
            termination_reason=self._stop_reason or TerminationReason.QUIESCENT,
            close=self._close,
            waves_used=self._waves_used,
            runs_used=self._runs_used,
        )

    def _reserve(self) -> int:
        return 1 if self._closer_id is not None else 0
