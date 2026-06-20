"""The ChatOrchestrator — the keystone that turns one human message into zero or
more persona turns and a single merged, streamed transcript.

Responsibilities (Task 4.6 / FR-M1..M5, FR-MR1..3, FR-E2, FR-D1/D2)
-------------------------------------------------------------------
1. Persist the human message (with any quotes).
2. Route it to the tagged personas (``resolve_targets``) — or honour an explicit
   override. No targets -> the message is stored as context only and the call
   yields nothing further (FR-M3).
3. For each target persona run a *turn*: build its delta + quotes + prompt from
   the shared transcript, resume its harness session, stream the events out,
   persist its reply, and advance its last-seen pointer.
4. SEQUENTIAL: turns run in order and each persona's delta is rebuilt from the
   *current* transcript, so a later persona sees an earlier sibling's same-turn
   reply (real cross-talk). PARALLEL: a single transcript snapshot is taken up
   front and all turns run concurrently against it, so no sibling sees another's
   same-turn reply; their events are interleaved into the single yielded stream
   via an :class:`asyncio.Queue`.

Fail-loud but ISOLATED (AGENTS §4): a single persona's failure is turned into a
``RunError`` event + an error-placeholder message + a finalized ``RunRecord``
(``error_kind`` set) for *that* persona, and NEVER aborts the post or the other
personas — in EITHER SEQUENTIAL or PARALLEL mode. This holds uniformly for a
raised typed harness error, a stale transcript pointer, AND any unexpected
(untyped) exception from a backend: every persona turn is recorded then
contained; no failure ever propagates out of its own turn.

The harness adapters RAISE errors (typed or otherwise) and never emit a terminal
``RunError`` themselves; translating a raised error into a recorded ``RunError``
event is the orchestrator's job (per the adapter contract).

last-seen pointer semantics
---------------------------
After a persona replies we set ``last_seen_message_id`` to the id of the most
recent message that existed *at the moment the turn started* — i.e. the last
message before the persona's own reply was persisted. This is captured as
``pre_reply_tail`` per turn. Setting it to the persona's own reply id would be
wrong: the persona would then never see its own reply re-quoted, but more
importantly it must NOT treat its own freshly-written reply as "already seen
context" that gets skipped — yet it also must not re-see, on its next turn, the
messages that were already in this turn's delta. Pointing at the pre-reply tail
means: next turn, the delta starts strictly after everything this persona was
shown this turn (which already included its sibling context), and the persona's
own reply — being authored by it — is shown attributed-by-name as part of that
next delta (build_delta keeps a persona's own past messages). In PARALLEL the
pre-reply tail is the shared snapshot tail (the human message), identical for
all siblings, so none advances past a sibling's same-turn reply.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence

from app.domain.errors import HarnessError, SessionNotFound, TeamError, TranscriptError
from app.domain.events import RunDone, RunError, StreamEvent, TextDelta, Usage
from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Message,
    Persona,
    PersonaSession,
    ReplyMode,
)
from app.harness.base import RunSpec
from app.harness.registry import BackendRegistry
from app.persistence.repositories import (
    AuthorRepo,
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    RunRepo,
    SessionRepo,
)
from app.persistence.run_log import RunLogStore, RunLogWriter
from app.services.mentions import find_mentions
from app.services.routing import resolve_targets
from app.services.run_scope import run_scope
from app.services.transcript import assemble_context

# Delegation caps (the "Balanced" preset). A persona's reply may pull in teammates
# by @handle; these bound the blast radius so one post can't fan out without limit.
# depth: operator post = 0, each delegation hop +1 (turns deeper than this are
# dropped). runs: total delegated turns allowed per operator post. A persona is
# also invoked at most ONCE per post (cycle guard), so chains always terminate.
DELEGATION_DEPTH_CAP = 2
DELEGATION_RUN_CAP = 6

YieldedEvent = tuple[str, StreamEvent]


class ChatOrchestrator:
    """Drives one human post into a merged stream of ``(persona_id, StreamEvent)``."""

    def __init__(
        self,
        *,
        persona_repo: PersonaRepo,
        author_repo: AuthorRepo,
        room_repo: RoomRepo,
        message_repo: MessageRepo,
        session_repo: SessionRepo,
        run_repo: RunRepo,
        run_log: RunLogStore,
        registry: BackendRegistry,
    ) -> None:
        self._personas = persona_repo
        self._authors = author_repo
        self._rooms = room_repo
        self._messages = message_repo
        self._sessions = session_repo
        self._runs = run_repo
        self._run_log = run_log
        self._registry = registry

    # ------------------------------------------------------------------ #
    # name resolution (cached per call)
    # ------------------------------------------------------------------ #

    def _make_name_of(self) -> _NameResolver:
        return _NameResolver(self._authors, self._personas)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    async def post_message(
        self,
        room_id: str,
        *,
        author: HumanAuthor,
        text: str,
        reply_mode: ReplyMode,
        tagged_override: list[Persona] | None = None,
        quoted_ids: Sequence[str] = (),
    ) -> AsyncIterator[YieldedEvent]:
        """Persist the human message, route it, and stream the persona turns.

        Yields ``(persona_id, StreamEvent)`` tuples as personas produce them. In
        PARALLEL mode events from concurrent personas are interleaved.
        """
        quoted_id_list = list(quoted_ids)
        human_message = Message(
            room_id=room_id,
            author_kind=AuthorKind.HUMAN,
            author_ref=author.id,
            content=text,
            quoted_message_ids=quoted_id_list,
        )
        self._messages.create(human_message)

        targets = self._resolve(room_id, text, tagged_override)
        if not targets:
            # FR-M3: stored as context only; nothing further.
            return

        name_of = self._make_name_of()
        room = self._rooms.get(room_id)
        delegation_on = bool(room and room.delegation_enabled)

        # Hold the inner generator explicitly and aclose() it in a finally. A bare
        # ``async for ... in inner(): yield`` does NOT close ``inner`` when *this*
        # generator is itself aclose()'d (GeneratorExit) — the inner generator's
        # own finally (which cancels + awaits the parallel workers, running their
        # backend cleanup) would otherwise only fire on GC, leaking in-flight tasks
        # on early stop. Explicit aclose() propagates GeneratorExit promptly.
        inner = self._drive(
            room_id,
            targets,
            author,
            text,
            quoted_id_list,
            name_of,
            reply_mode,
            human_message.id,
            delegation_on,
        )
        try:
            async for item in inner:
                yield item
        finally:
            await inner.aclose()

    async def _drive(
        self,
        room_id: str,
        targets: list[Persona],
        author: HumanAuthor,
        text: str,
        quoted_ids: list[str],
        name_of: _NameResolver,
        reply_mode: ReplyMode,
        human_message_id: str,
        delegation_on: bool,
    ) -> AsyncGenerator[YieldedEvent, None]:
        """Run the operator's targeted turns, then any delegated turns they spawn.

        Each sub-generator (the initial wave and the delegation loop) is held and
        aclose()'d in a finally so GeneratorExit (early client disconnect) reaps
        in-flight harness subprocesses promptly — same contract as post_message.
        """
        if reply_mode is ReplyMode.PARALLEL:
            initial = self._run_parallel(
                room_id, targets, author, text, quoted_ids, name_of, delegation_on
            )
        else:
            initial = self._run_sequential(
                room_id, targets, author, text, quoted_ids, name_of, delegation_on
            )
        try:
            async for item in initial:
                yield item
        finally:
            await initial.aclose()

        if not delegation_on:
            return

        deleg = self._run_delegations(room_id, targets, human_message_id, name_of)
        try:
            async for item in deleg:
                yield item
        finally:
            await deleg.aclose()

    async def send_control(
        self, room_id: str, persona_id: str, command: str
    ) -> AsyncIterator[StreamEvent]:
        """Run a harness control command (e.g. ``/compact``) against a persona's
        live session and stream the resulting events (FR-C1/FR-C2).

        Contract / error handling:

        * No persona, or no session with a ``harness_session_id`` yet -> raise
          :class:`SessionNotFound` (BEFORE any run is recorded): you cannot run a
          control command against a session that does not exist. This surfaces to
          the caller as an exception (no run/log is created).
        * ``command`` not in ``backend.supported_commands`` -> raise
          :class:`HarnessError` naming the command AND the supported set (FR-C2:
          never silently no-op). Also surfaces to the caller as an exception.
        * Otherwise a ``RunRecord`` is opened, the command is streamed, every
          event is logged + yielded, and the ``RunRecord`` is finalized. A
          ``RunDone.session_id`` (a ``/compact`` may keep the SAME id; a ``/clear``
          may change it) is persisted onto the ``PersonaSession`` — whatever the
          backend returns, including ``None``. The ``last_seen_message_id``
          pointer is preserved (a control command shows the persona no new
          transcript).
        * If the backend RAISES mid-stream, we record it consistently with
          ``post_message``: yield a terminal ``RunError`` event + finalize the
          ``RunRecord`` with ``error_kind`` set, and DO NOT re-raise (the stream
          ends cleanly). The two pre-run validation errors above are the only
          cases that reach the caller as exceptions.
        """
        persona = self._personas.get(persona_id)
        if persona is None:
            raise SessionNotFound(f"unknown persona: {persona_id}")
        session = self._sessions.get(room_id, persona_id)
        if session is None or session.harness_session_id is None:
            raise SessionNotFound(
                f"no live harness session for persona {persona_id} in room {room_id}; "
                "post a message first to start one"
            )
        harness_session_id = session.harness_session_id

        backend = self._registry.get_backend(persona.provider)
        if command not in backend.supported_commands:
            raise HarnessError(
                f"command {command!r} not supported by provider "
                f"{persona.provider.value!r} (supported: {sorted(backend.supported_commands)})"
            )

        with run_scope(
            self._runs,
            self._run_log,
            room_id=room_id,
            persona_id=persona_id,
            command_redacted=f"{persona.provider.value} control {command} resume=True",
        ) as (run, writer):
            run_id = run.run_id
            captured_session_id: str | None = harness_session_id
            try:
                async for event in backend.send_command(harness_session_id, command):
                    writer.write_event(event)
                    if isinstance(event, Usage):
                        run.usage = event.model_dump()
                    elif isinstance(event, RunDone):
                        captured_session_id = event.session_id
                        # Stamp the owning run_id onto the terminal event so a
                        # transport can correlate end-of-turn with this RunRecord.
                        event = event.model_copy(update={"run_id": run_id})
                    yield event
            except TeamError as exc:
                run.error_kind = exc.kind
                err = RunError(
                    error_kind=exc.kind,
                    message=_redact(str(exc)),
                    run_id=run_id,
                    log_path=str(writer.path),
                )
                writer.write_event(err)
                yield err
                return
            except Exception as exc:
                run.error_kind = type(exc).__name__
                err = RunError(
                    error_kind=run.error_kind,
                    message=_redact(str(exc)),
                    run_id=run_id,
                    log_path=str(writer.path),
                )
                writer.write_event(err)
                yield err
                return

            self._sessions.upsert(
                PersonaSession(
                    room_id=room_id,
                    persona_id=persona_id,
                    provider=persona.provider,
                    harness_session_id=captured_session_id,
                    last_seen_message_id=session.last_seen_message_id,
                    status="idle",
                )
            )

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #

    def _resolve(
        self, room_id: str, text: str, tagged_override: list[Persona] | None
    ) -> list[Persona]:
        if tagged_override is not None:
            return tagged_override
        member_ids = self._rooms.list_members(room_id)
        members: list[Persona] = []
        for pid in member_ids:
            persona = self._personas.get(pid)
            if persona is not None:
                members.append(persona)
        return resolve_targets(text, members)

    # ------------------------------------------------------------------ #
    # sequential
    # ------------------------------------------------------------------ #

    async def _run_sequential(
        self,
        room_id: str,
        targets: list[Persona],
        author: HumanAuthor,
        text: str,
        quoted_ids: list[str],
        name_of: _NameResolver,
        delegation_on: bool = False,
    ) -> AsyncGenerator[YieldedEvent, None]:
        for persona in targets:
            # current transcript: includes prior siblings' same-turn replies.
            messages = self._messages.list_for_room(room_id)
            pre_reply_tail = messages[-1].id if messages else None
            async for item in self._run_turn(
                room_id,
                persona,
                author,
                text,
                quoted_ids,
                name_of,
                messages,
                pre_reply_tail,
                delegation_on,
            ):
                yield item

    # ------------------------------------------------------------------ #
    # parallel
    # ------------------------------------------------------------------ #

    async def _run_parallel(
        self,
        room_id: str,
        targets: list[Persona],
        author: HumanAuthor,
        text: str,
        quoted_ids: list[str],
        name_of: _NameResolver,
        delegation_on: bool = False,
    ) -> AsyncGenerator[YieldedEvent, None]:
        # One snapshot for everyone — no sibling sees another's same-turn reply.
        snapshot = self._messages.list_for_room(room_id)
        snapshot_tail = snapshot[-1].id if snapshot else None

        queue: asyncio.Queue[YieldedEvent | _Sentinel] = asyncio.Queue()

        async def worker(persona: Persona) -> None:
            turn = self._run_turn(
                room_id,
                persona,
                author,
                text,
                quoted_ids,
                name_of,
                snapshot,
                snapshot_tail,
                delegation_on,
            )
            try:
                async for item in turn:
                    await queue.put(item)
            finally:
                # On normal completion OR on cancellation (early stop / GeneratorExit
                # propagated as CancelledError into the worker), explicitly close the
                # per-turn generator so its finally — and the backend's run() finally —
                # run promptly rather than waiting for GC. This is what composes
                # GeneratorExit -> worker cancellation -> backend cleanup.
                await turn.aclose()
                await queue.put(_DONE)

        tasks = [asyncio.create_task(worker(p)) for p in targets]
        remaining = len(tasks)
        try:
            while remaining > 0:
                item = await queue.get()
                if isinstance(item, _Sentinel):
                    remaining -= 1
                    continue
                yield item
        finally:
            # Cancel and await every worker so none leaks. Each persona's turn is
            # fully isolated inside _run_turn (failures are recorded as a RunError
            # event and never propagated), so a worker never raises a turn error
            # out here; the only exceptions we expect from gather are CancelledError
            # from the early-stop path, which return_exceptions=True absorbs.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # delegation (persona -> persona)
    # ------------------------------------------------------------------ #

    async def _run_delegations(
        self,
        room_id: str,
        initial_targets: list[Persona],
        human_message_id: str,
        name_of: _NameResolver,
    ) -> AsyncGenerator[YieldedEvent, None]:
        """Follow @-mentions in persona replies, breadth-first, within the caps.

        Each persona runs at most ONCE per post (cycle guard) and the chain is
        bounded by ``DELEGATION_DEPTH_CAP`` (hops) and ``DELEGATION_RUN_CAP``
        (total delegated turns). A mentioned persona that isn't a room member is
        pulled in (added to the room) before it runs.
        """
        invoked: set[str] = {p.id for p in initial_targets}
        runs_used = 0
        # The operator's targets replied at depth 0; turns they spawn are depth 1.
        frontier: list[tuple[Message, int]] = [
            (reply, 1) for reply in self._replies_after(room_id, human_message_id)
        ]
        while frontier:
            next_frontier: list[tuple[Message, int]] = []
            for reply, depth in frontier:
                if depth > DELEGATION_DEPTH_CAP:
                    continue
                delegator = self._personas.get(reply.author_ref)
                if delegator is None:
                    continue
                for target in self._resolve_delegations(reply.content, room_id, delegator.handle):
                    if runs_used >= DELEGATION_RUN_CAP:
                        return  # hard fan-out stop for the whole post
                    if target.id in invoked:
                        continue  # cycle guard: one turn per persona per post
                    invoked.add(target.id)
                    runs_used += 1
                    messages = self._messages.list_for_room(room_id)
                    pre_reply_tail = messages[-1].id if messages else None
                    turn = self._run_delegated_turn(
                        room_id, target, delegator, name_of, messages, pre_reply_tail
                    )
                    try:
                        async for item in turn:
                            yield item
                    finally:
                        await turn.aclose()
                    child = self._latest_reply(room_id, target.id)
                    if child is not None:
                        next_frontier.append((child, depth + 1))
            frontier = next_frontier

    async def _run_delegated_turn(
        self,
        room_id: str,
        target: Persona,
        delegator: Persona,
        name_of: _NameResolver,
        messages: list[Message],
        pre_reply_tail: str | None,
    ) -> AsyncGenerator[YieldedEvent, None]:
        """Run ``target``'s turn in response to ``delegator``'s reply.

        The delegator's reply already sits in the transcript (directed at the
        target via its @handle), so it arrives through the normal delta. We pass
        a transient author = the delegator's name and a short nudge as the directed
        line — no second agent loop, just the existing turn machinery.
        """
        pseudo_author = HumanAuthor(name=delegator.name, weight_enabled=False, weight_note="")
        nudge = f"(brought in by @{delegator.handle} above — please respond)"
        async for item in self._run_turn(
            room_id,
            target,
            pseudo_author,
            nudge,
            [],
            name_of,
            messages,
            pre_reply_tail,
            delegation_on=True,
        ):
            yield item

    def _resolve_delegations(
        self, content: str, room_id: str, delegator_handle: str
    ) -> list[Persona]:
        """Personas a reply delegates to: explicit @handles (not @everyone, not
        the delegator itself), resolved globally and auto-added to the room."""
        delegator_handle = delegator_handle.lstrip("@").lower()
        handles: list[str] = []
        seen: set[str] = set()
        for mention in find_mentions(content):
            if mention == "everyone" or mention == delegator_handle or mention in seen:
                continue
            seen.add(mention)
            handles.append(mention)
        if not handles:
            return []
        by_handle = {p.handle: p for p in self._personas.list()}
        members = set(self._rooms.list_members(room_id))
        out: list[Persona] = []
        for handle in handles:
            persona = by_handle.get(handle)
            if persona is None:
                continue  # unknown handle: ignored, same as operator routing
            if persona.id not in members:
                self._rooms.add_member(room_id, persona.id)  # pull-in
            out.append(persona)
        return out

    def _replies_after(self, room_id: str, message_id: str) -> list[Message]:
        """Persona reply messages created strictly after ``message_id`` (skipping
        error markers) — the operator-turn replies that can seed delegation."""
        messages = self._messages.list_for_room(room_id)
        idx = next((i for i, m in enumerate(messages) if m.id == message_id), -1)
        tail = messages[idx + 1 :] if idx >= 0 else messages
        return [m for m in tail if self._is_delegatable_reply(m)]

    def _latest_reply(self, room_id: str, persona_id: str) -> Message | None:
        """The most recent non-error reply authored by ``persona_id``."""
        for message in reversed(self._messages.list_for_room(room_id)):
            if message.author_ref == persona_id and self._is_delegatable_reply(message):
                return message
        return None

    @staticmethod
    def _is_delegatable_reply(message: Message) -> bool:
        return message.author_kind is AuthorKind.PERSONA and not message.content.startswith(
            "[error:"
        )

    def _roster_note(self, room_id: str, exclude: Persona) -> str:
        """The teammate roster appended to a persona's system prompt when
        delegation is enabled, so it knows who it can call by @handle."""
        members: list[Persona] = []
        for pid in self._rooms.list_members(room_id):
            persona = self._personas.get(pid)
            if persona is not None and persona.id != exclude.id:
                members.append(persona)
        if not members:
            return ""
        lines = "\n".join(f"- @{p.handle} — {p.job or p.name}" for p in members)
        return (
            "# Your team\n"
            "You're in a group chat with the operator and the teammates below. To bring a "
            "teammate in or ask them to work on something, mention their @handle in your "
            "reply — they'll be pulled into the room if needed and will respond. Delegate "
            "only when it genuinely helps; don't tag everyone.\n" + lines
        )

    # ------------------------------------------------------------------ #
    # one persona turn (shared by sequential & parallel)
    # ------------------------------------------------------------------ #

    async def _run_turn(
        self,
        room_id: str,
        persona: Persona,
        author: HumanAuthor,
        text: str,
        quoted_ids: list[str],
        name_of: _NameResolver,
        messages: list[Message],
        pre_reply_tail: str | None,
        delegation_on: bool = False,
    ) -> AsyncGenerator[YieldedEvent, None]:
        session = self._sessions.get(room_id, persona.id)
        resume_session_id = session.harness_session_id if session else None
        last_seen = session.last_seen_message_id if session else None

        with run_scope(
            self._runs,
            self._run_log,
            room_id=room_id,
            persona_id=persona.id,
            command_redacted=self._redacted_command(persona, resume_session_id),
        ) as (run, writer):
            run_id = run.run_id
            accumulated: list[str] = []
            captured_session_id: str | None = resume_session_id

            # Build the prompt. A stale pointer (TranscriptError) is a per-persona
            # failure, not a crash of the whole post.
            try:
                prompt = self._build_prompt(
                    messages, last_seen, persona, author, text, quoted_ids, name_of
                )
            except TranscriptError as exc:
                run.error_kind = exc.kind
                async for item in self._emit_error(
                    room_id, persona, run_id, writer, exc.kind, str(exc)
                ):
                    yield item
                return

            roster = self._roster_note(room_id, persona) if delegation_on else ""
            # The room's shared working_dir (if set) wins over the persona's own, so
            # the whole team operates in one directory; else fall back per-persona.
            room = self._rooms.get(room_id)
            working_dir = (room.working_dir if room else None) or persona.working_dir
            spec = self._build_spec(persona, prompt, resume_session_id, roster, working_dir)
            backend = self._registry.get_backend(persona.provider)

            try:
                async for event in backend.run(spec):
                    writer.write_event(event)
                    if isinstance(event, TextDelta):
                        accumulated.append(event.text)
                    elif isinstance(event, Usage):
                        run.usage = event.model_dump()
                    elif isinstance(event, RunDone):
                        if event.session_id is not None:
                            captured_session_id = event.session_id
                        # Stamp the owning run_id onto the terminal event so a
                        # transport can correlate end-of-turn with this RunRecord.
                        event = event.model_copy(update={"run_id": run_id})
                    yield (persona.id, event)
            except TeamError as exc:
                run.error_kind = exc.kind
                async for item in self._emit_error(
                    room_id, persona, run_id, writer, exc.kind, _redact(str(exc))
                ):
                    yield item
                return
            except Exception as exc:
                # Uniform per-persona isolation (AGENTS §4): an untyped error is
                # recorded exactly like a typed one — structured RunError event +
                # placeholder Message + finalized RunRecord (error_kind set on the
                # run, finalized by run_scope) — and then we RETURN, never
                # re-raise. Re-raising here used to abort later siblings in
                # SEQUENTIAL mode and was silently swallowed by gather() in
                # PARALLEL: an asymmetry. A persona's failure must never propagate
                # out of its own turn in either mode.
                run.error_kind = type(exc).__name__
                async for item in self._emit_error(
                    room_id, persona, run_id, writer, run.error_kind, _redact(str(exc))
                ):
                    yield item
                return

            # success: persist reply + advance pointer.
            self._messages.create(
                Message(
                    room_id=room_id,
                    author_kind=AuthorKind.PERSONA,
                    author_ref=persona.id,
                    content="".join(accumulated),
                    run_id=run_id,
                )
            )
            self._sessions.upsert(
                PersonaSession(
                    room_id=room_id,
                    persona_id=persona.id,
                    provider=persona.provider,
                    harness_session_id=captured_session_id,
                    last_seen_message_id=pre_reply_tail,
                    status="idle",
                )
            )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _build_prompt(
        self,
        messages: list[Message],
        last_seen: str | None,
        persona: Persona,
        author: HumanAuthor,
        text: str,
        quoted_ids: list[str],
        name_of: _NameResolver,
    ) -> str:
        # Resolve the quoted ids to Message objects (the only I/O here); slicing,
        # quote-dedup, and assembly all happen once behind assemble_context, so
        # the delta is sliced exactly once and the dedup can never drift from it.
        quoted_messages = [m for qid in quoted_ids if (m := self._messages.get(qid)) is not None]
        return assemble_context(
            messages,
            last_seen,
            quoted_messages=quoted_messages,
            persona_handle=persona.handle,
            author=author,
            new_text=text,
            name_of=name_of.resolve,
        )

    def _build_spec(
        self,
        persona: Persona,
        prompt: str,
        resume_session_id: str | None,
        system_suffix: str = "",
        working_dir: str | None = None,
    ) -> RunSpec:
        system_prompt = persona.system_prompt
        if system_suffix:
            # Append the live teammate roster (delegation enabled) so the persona
            # knows who it can pull in. Kept in the system prompt, not the user
            # prompt, so it steers behavior without polluting the transcript.
            system_prompt = f"{system_prompt}\n\n{system_suffix}".strip()
        return RunSpec(
            prompt=prompt,
            provider=persona.provider,
            model=persona.model,
            system_prompt=system_prompt,
            effort=persona.effort,
            resume_session_id=resume_session_id,
            mcp_servers=persona.mcp_servers,
            allowed_tools=persona.allowed_tools,
            working_dir=working_dir if working_dir is not None else persona.working_dir,
            permission_mode=persona.permission_mode,
        )

    @staticmethod
    def _redacted_command(persona: Persona, resume_session_id: str | None) -> str:
        """High-level, secret-free run descriptor stored on the RunRecord.

        Deliberately excludes the system prompt, prompt body, MCP config, and any
        argv that could carry secrets (AGENTS §4). The adapter owns the literal
        argv and the full stream goes to the JSONL log. Full-fidelity argv capture
        is a future enhancement.
        """
        return f"{persona.provider.value} model={persona.model} resume={bool(resume_session_id)}"

    async def _emit_error(
        self,
        room_id: str,
        persona: Persona,
        run_id: str,
        writer: RunLogWriter,
        error_kind: str,
        message: str,
    ) -> AsyncIterator[YieldedEvent]:
        """Translate a per-persona failure into a recorded RunError + placeholder.

        Writes a ``RunError`` to the run log, yields it to the caller, and persists
        a short error-marker ``Message`` so the transcript reflects the failure.
        Does not raise — isolation is the caller's contract.
        """
        event = RunError(
            error_kind=error_kind,
            message=message,
            run_id=run_id,
            log_path=str(writer.path),
        )
        writer.write_event(event)
        self._messages.create(
            Message(
                room_id=room_id,
                author_kind=AuthorKind.PERSONA,
                author_ref=persona.id,
                content=f"[error: {error_kind}] {message}",
                run_id=run_id,
            )
        )
        yield (persona.id, event)


class _Sentinel:
    """Marker for 'one parallel worker finished' on the merge queue."""


_DONE = _Sentinel()


class _NameResolver:
    """Per-call cached display-name lookup (human -> author.name, persona -> @handle).

    Caches both repo hits and misses within a single ``post_message`` call to
    avoid repeated DB reads when many messages share an author.
    """

    def __init__(self, authors: AuthorRepo, personas: PersonaRepo) -> None:
        self._authors = authors
        self._personas = personas
        self._cache: dict[tuple[AuthorKind, str], str] = {}

    def resolve(self, author_kind: AuthorKind, author_ref: str) -> str:
        key = (author_kind, author_ref)
        if key in self._cache:
            return self._cache[key]
        display: str
        if author_kind is AuthorKind.HUMAN:
            author = self._authors.get(author_ref)
            display = author.name if author is not None else author_ref
        else:
            persona = self._personas.get(author_ref)
            display = f"@{persona.handle}" if persona is not None else author_ref
        self._cache[key] = display
        return display


def _redact(message: str) -> str:
    """Best-effort redaction for error messages surfaced to the transcript/log.

    Error strings from the harness layer are already redacted at the source
    (stderr is redacted there); this is a defensive truncation to keep markers
    short and avoid accidental secret echo in long messages.
    """
    flat = " ".join(message.split())
    return flat[:500]
