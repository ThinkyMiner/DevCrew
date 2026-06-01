from __future__ import annotations

from collections.abc import AsyncIterator

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

    def __init__(self, script: dict[str, list[StreamEvent]] | None = None) -> None:
        self._script: dict[str, list[StreamEvent]] = dict(script or {})
        self.calls: list[RunSpec] = []
        self._counter = 0

    # -- scripting / introspection helpers (used by orchestrator tests) ---------

    def script_reply(self, substring: str, text: str) -> None:
        """Script a simple text reply for any prompt containing ``substring``.

        Convenience over :meth:`__init__`'s ``script``: emits a single
        ``TextDelta(text)`` followed by an auto-allocated ``RunDone``.
        """
        self._script[substring] = [TextDelta(text=text)]

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
        for substring, events in self._script.items():
            if substring in prompt:
                return events
        return None

    async def run(self, spec: RunSpec) -> AsyncIterator[StreamEvent]:
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
