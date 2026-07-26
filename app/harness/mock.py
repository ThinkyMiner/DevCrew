from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence

from app.domain.errors import HarnessError
from app.domain.events import RunDone, StreamEvent, TextDelta, Usage
from app.harness.base import RunSpec


class MockHarness:
    """Deterministic, scriptable in-memory backend — the testing backbone.

    No subprocess, no token spend. Every run is reproducible:

    * Session ids are allocated from an internal counter as ``mock-<n>``.
    * Resuming (``spec.resume_session_id`` set) keeps the *same* session id, so
      restart/resume flows can be asserted exactly.
    * Every received :class:`RunSpec` is appended to :attr:`calls`, so tests can
      assert on prompt assembly (delta/quote/weight injection by the orchestrator).

    Scripting
    ---------
    Provide canned streams keyed by a prompt substring. If a run's prompt contains
    a scripted substring, that stream is emitted (a terminal :class:`RunDone` is
    appended only if the script did not include one). Otherwise the default
    behavior emits a couple of prompt-referencing :class:`TextDelta`s, a
    :class:`Usage`, and a :class:`RunDone`.
    """

    name = "mock"
    supported_commands: set[str] = {"/compact", "/clear"}
    supported_models: tuple[str, ...] = ("mock",)

    def __init__(self, script: dict[str, list[StreamEvent]] | None = None) -> None:
        self._script: dict[str, list[StreamEvent]] = dict(script or {})
        self._queues: dict[str, deque[list[StreamEvent] | Exception]] = {}
        self.calls: list[RunSpec] = []
        self._counter = 0

    # -- scripting / introspection helpers (used by orchestrator tests) ---------

    def script_reply(self, substring: str, text: str) -> None:
        """Script a simple text reply for any prompt containing ``substring``.

        Convenience over :meth:`__init__`'s ``script``: emits a single
        ``TextDelta(text)`` followed by an auto-allocated ``RunDone``.
        """
        self._script[substring] = [TextDelta(text=text)]

    def script_replies(self, substring: str, *texts: str) -> None:
        """Script an ordered QUEUE of text replies for ``substring``.

        Each matching run pops and emits the next reply (as one ``TextDelta``
        plus an auto ``RunDone``); a matching run after the queue is exhausted
        raises :class:`HarnessError` loudly — a test that runs a persona more
        times than it scripted is a test bug, never a silent repeat. This is
        what multi-round deliberation tests use to give one persona different
        replies on successive turns.
        """
        self.script_runs(substring, *([TextDelta(text=t)] for t in texts))

    def script_runs(self, substring: str, *runs: Sequence[StreamEvent] | Exception) -> None:
        """Low-level sibling of :meth:`script_replies`: queue full event lists
        and/or exceptions. A queued ``Exception`` is RAISED when its turn comes,
        simulating a mid-run harness failure (timeout, crash) on exactly that
        call. Deliberately takes literal sequences, not callbacks — scripted
        tests must stay auditable data, not little procedural agents.
        """
        queue = self._queues.setdefault(substring, deque())
        for run in runs:
            queue.append(run if isinstance(run, Exception) else list(run))

    @property
    def last_prompt(self) -> str | None:
        """The prompt of the most recent run, or ``None`` if there were none."""
        return self.calls[-1].prompt if self.calls else None

    def last_prompt_for(self, session_id: str) -> str | None:
        """The most recent prompt of a run that resumed ``session_id``.

        Returns ``None`` if no recorded run resumed that session. Useful for
        asserting cross-talk, e.g. "persona B's prompt contains A's reply text".
        """
        for spec in reversed(self.calls):
            if spec.resume_session_id == session_id:
                return spec.prompt
        return None

    # -- AgentBackend implementation -------------------------------------------

    def _next_session_id(self) -> str:
        self._counter += 1
        return f"mock-{self._counter}"

    def _resolve_session_id(self, spec: RunSpec) -> str:
        if spec.resume_session_id is not None:
            return spec.resume_session_id
        return self._next_session_id()

    def _scripted_for(self, prompt: str) -> list[StreamEvent] | None:
        """Resolve the scripted stream for ``prompt`` across BOTH script stores.

        Static scripts (``script_reply``) and queued scripts (``script_replies``
        / ``script_runs``) share one namespace: a prompt matching more than one
        key — of either kind — is ambiguous and raises. A queued match consumes
        its next entry; exhaustion and queued exceptions raise loudly.
        """
        matches = sorted(
            {s for s in self._script if s in prompt} | {s for s in self._queues if s in prompt}
        )
        if len(matches) > 1:
            raise HarnessError(
                "ambiguous mock script match: prompt contains multiple scripted "
                f"substrings {matches!r} — refusing to silently pick one"
            )
        if not matches:
            return None
        key = matches[0]
        if key in self._script:
            return self._script[key]
        queue = self._queues[key]
        if not queue:
            raise HarnessError(
                f"mock script queue exhausted for substring {key!r}: the persona ran "
                "more times than the test scripted — script more replies or fix the test"
            )
        entry = queue.popleft()
        if isinstance(entry, Exception):
            raise entry
        return entry

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
        """Emit a scripted stream or the default prompt-referencing stream.

        Note: the default-path :class:`Usage` values
        (``input_tokens``/``output_tokens``/``context_tokens``) are placeholders
        derived from the current prompt length and are NOT session-cumulative, so
        tests asserting on the FR-C3 context indicator should script an explicit
        ``Usage`` rather than rely on these defaults. Scripting is keyed on prompt
        substring only (not session or persona), so session-aware assertions
        should use :meth:`last_prompt_for`.
        """
        self.calls.append(spec)
        session_id = self._resolve_session_id(spec)

        scripted = self._scripted_for(spec.prompt)
        if scripted is not None:
            has_done = False
            for event in scripted:
                if isinstance(event, RunDone):
                    has_done = True
                yield event
            if not has_done:
                yield RunDone(session_id=session_id)
            return

        yield TextDelta(text=f"[mock:{self.name}] received: {spec.prompt}")
        yield TextDelta(text=" — acknowledged.")
        yield Usage(input_tokens=len(spec.prompt), output_tokens=8, context_tokens=len(spec.prompt))
        yield RunDone(session_id=session_id)

    async def send_command(self, session_id: str, command: str) -> AsyncIterator[StreamEvent]:
        if command not in self.supported_commands:
            raise HarnessError(
                f"command {command!r} not supported by {self.name} "
                f"(supported: {sorted(self.supported_commands)})"
            )
        yield TextDelta(text="[compacted]")
        yield RunDone(session_id=session_id)
