"""WebSocket streaming endpoint (Task 5.3 / 5.4).

A THIN transport: it translates JSON text frames to/from the
:class:`~app.services.orchestrator.ChatOrchestrator` and the service facades and
holds NO business logic. It imports only services / domain / fastapi.

Frame protocol (JSON text frames)
----------------------------------
Client -> server (inbound):

* ``{"type": "post", "author_id", "text", "reply_mode": "sequential"|"parallel",
  "quoted_ids": [...]}`` — drive ``orchestrator.post_message``.
* ``{"type": "command", "persona_id", "command": "/compact"}`` — drive
  ``orchestrator.send_control``.

Server -> client (outbound):

* ``{"type": "event", "persona_id", "event": {<StreamEvent.model_dump>}}`` — one
  per streamed event. A ``RunError`` event is NOT sent raw; it is translated into
  an ``error_card`` frame instead (FR-E2 — every failure is visible exactly once).
  The error_card is the TRANSIENT in-stream view; the orchestrator also persists
  the failure as an error-marker ``Message`` ("[error: <kind>] <msg>"). On
  ``turn_complete`` the client repaints the canonical transcript (clearing the
  transient cards) and renders that persisted marker as the same styled error
  card (see app/web/js/transcript.js), so the failure remains visible exactly
  once after reconcile — the canonical render owns the persisted display.
* ``{"type": "error_card", "persona_id", "run_id", "error_kind", "message",
  "command_redacted", "log_url": "/runs/<run_id>/log"}`` — emitted when a
  ``RunError`` event is seen. ``run_id``/``log_path`` come from the enriched
  event; ``command_redacted`` is looked up on the ``RunRecord`` (already redacted
  by the orchestrator — no raw prompt ever crosses the wire).
* ``{"type": "turn_complete"}`` — a ``post`` finished.
* ``{"type": "command_complete"}`` — a ``command`` finished.
* ``{"type": "error", "kind", "message"}`` — inbound-validation / pre-run errors
  (bad author/room/reply_mode, unsupported command, no session). The socket stays
  open and usable.

Canonical message ids: the stream does not carry the persisted reply ``Message``
id. The ``turn_complete`` frame signals end-of-turn; the client reconciles
canonical ids by refetching ``GET /rooms/{id}/messages`` (Unit 12 frontend).

Concurrency / sqlite (Unit 10 review)
--------------------------------------
The orchestrator does synchronous DB work between async awaits over a single
shared sqlite connection guarded by a per-thread reentrant lock
(``check_same_thread=False``). We therefore drive it DIRECTLY on the event-loop
thread (plain ``async for``) and never offload turns to a threadpool: keeping
every transaction on one thread avoids a cross-thread RLock deadlock, and the
sync repo calls contain no awaits so each completes atomically without
interleaving.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from itertools import count
from typing import cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.api.deps import Services
from app.domain.errors import TeamError
from app.domain.events import RunDone, RunError, StreamEvent
from app.domain.models import HumanAuthor, ReplyMode
from app.services.orchestrator import ChatOrchestrator
from app.services.session_store import SessionStore

_BAD_FRAME = object()
"""Sentinel returned by ``_receive_frame`` when an inbound frame was malformed and
a ``BadFrame`` error frame was already sent; tells ``serve`` to keep looping."""


def register_ws(app: FastAPI) -> None:
    """Wire the WebSocket route onto an app built by the composition root."""

    @app.websocket("/ws/rooms/{room_id}")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        await websocket.accept()
        services: Services = websocket.app.state.services
        orchestrator: ChatOrchestrator = websocket.app.state.orchestrator
        session_store: SessionStore = websocket.app.state.session_store
        handler = _RoomConnection(websocket, room_id, services, orchestrator, session_store)
        await handler.serve()


class _RoomConnection:
    """One open socket: a receive loop dispatching inbound frames to the orchestrator."""

    def __init__(
        self,
        websocket: WebSocket,
        room_id: str,
        services: Services,
        orchestrator: ChatOrchestrator,
        session_store: SessionStore,
    ) -> None:
        self._ws = websocket
        self._room_id = room_id
        self._services = services
        self._orch = orchestrator
        self._sessions = session_store
        # Monotonic source of WS-local activity tokens (see _track_start). NOT the
        # orchestrator's run_id — purely an opaque id so mark_active/mark_done stay
        # balanced regardless of which task the orchestrator runs the turn on.
        self._activity_seq = count()

    async def serve(self) -> None:
        try:
            while True:
                # Serialization invariant: each inbound frame is fully drained
                # (its orchestrator drive awaited to completion in _dispatch)
                # BEFORE the next frame is read here. A single socket therefore
                # never drives two overlapping orchestrator runs — the
                # no-concurrent-drive guarantee is intentional, not incidental.
                frame = await self._receive_frame()
                if frame is _BAD_FRAME:
                    continue  # BadFrame error already sent; keep the socket open
                await self._dispatch(frame)
        except WebSocketDisconnect:
            # Client went away (possibly mid-stream). _handle_post/_handle_command
            # run their own try/finally that aclose()s the orchestrator generator,
            # so in-flight harness subprocesses are reaped; nothing to do here.
            return

    async def _receive_frame(self) -> object:
        """Read and JSON-decode one inbound frame.

        Guards against malformed/binary frames (C1): a non-JSON text frame
        (json.JSONDecodeError / ValueError), a binary frame (KeyError on
        ``message['text']``), or invalid UTF-8 (UnicodeDecodeError) must NOT tear
        down the socket. On any of these we send a ``BadFrame`` error frame and
        return a sentinel so the serve loop continues. WebSocketDisconnect is left
        to propagate as the clean exit.
        """
        try:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            text = message["text"]  # KeyError if a binary frame ("bytes" only)
            return json.loads(text)
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError, ValueError):
            await self._error("BadFrame", "frame must be a UTF-8 JSON text message")
            return _BAD_FRAME

    async def _dispatch(self, frame: object) -> None:
        if not isinstance(frame, dict):
            await self._error("BadFrame", "frame must be a JSON object")
            return
        ftype = frame.get("type")
        if ftype == "post":
            await self._handle_post(frame)
        elif ftype == "command":
            await self._handle_command(frame)
        else:
            await self._error("BadFrame", f"unknown frame type: {ftype!r}")

    # -- post --------------------------------------------------------------- #

    async def _handle_post(self, frame: dict[str, object]) -> None:
        author = await self._resolve_author(frame.get("author_id"))
        if author is None:
            return  # error frame already sent
        reply_mode = await self._resolve_reply_mode(frame.get("reply_mode"))
        if reply_mode is None:
            return
        text = str(frame.get("text", ""))
        raw_quoted = frame.get("quoted_ids") or []
        quoted_ids = [str(q) for q in raw_quoted] if isinstance(raw_quoted, list) else []

        # Driven directly on the event loop (see module docstring: sqlite/RLock).
        # The orchestrator methods are async generators; cast so we can aclose()
        # them on disconnect (their declared AsyncIterator return type hides it).
        stream = cast(
            "AsyncGenerator[tuple[str, StreamEvent], None]",
            self._orch.post_message(
                self._room_id,
                author=author,
                text=text,
                reply_mode=reply_mode,
                quoted_ids=quoted_ids,
            ),
        )
        # In-flight tracking (FR): the orchestrator hides per-turn boundaries, so we
        # derive activity from the stream (see _track_start) — works in BOTH
        # sequential and parallel. Personas left active by an early disconnect are
        # cleared in the finally as a backstop.
        active: dict[str, str] = {}
        try:
            async for persona_id, event in stream:
                self._track_start(active, persona_id)
                if isinstance(event, RunError):
                    self._track_end(active, persona_id)
                    await self._send_error_card(persona_id, event)
                    continue
                if isinstance(event, RunDone):
                    self._track_end(active, persona_id)
                await self._send_event(persona_id, event)
            await self._ws.send_json({"type": "turn_complete"})
        except TeamError as exc:
            # Symmetric with _handle_command (I1): post_message does synchronous repo
            # work BEFORE its first yield, so a TeamError raised pre-yield would
            # otherwise escape to serve() (which only catches WebSocketDisconnect)
            # and tear down the socket. Convert it to an {type:error} frame instead.
            await self._error(exc.kind, str(exc))
        except WebSocketDisconnect:
            raise
        finally:
            await stream.aclose()
            self._clear_active(active)

    # -- command ------------------------------------------------------------ #

    async def _handle_command(self, frame: dict[str, object]) -> None:
        persona_id = str(frame.get("persona_id", ""))
        command = str(frame.get("command", ""))
        # send_control is an async generator: its pre-run validation (SessionNotFound
        # / unsupported HarnessError) raises on the FIRST iteration, not at creation,
        # so the except TeamError below turns those into an error frame.
        stream = cast(
            "AsyncGenerator[StreamEvent, None]",
            self._orch.send_control(self._room_id, persona_id, command),
        )
        active: dict[str, str] = {}
        try:
            async for event in stream:
                self._track_start(active, persona_id)
                if isinstance(event, RunError):
                    self._track_end(active, persona_id)
                    await self._send_error_card(persona_id, event)
                    continue
                if isinstance(event, RunDone):
                    self._track_end(active, persona_id)
                await self._send_event(persona_id, event)
            await self._ws.send_json({"type": "command_complete"})
        except TeamError as exc:
            # Pre-run validation (raised on first iteration of the generator).
            await self._error(exc.kind, str(exc))
        except WebSocketDisconnect:
            raise
        finally:
            await stream.aclose()
            self._clear_active(active)

    # -- in-flight tracking heuristic --------------------------------------- #

    def _track_start(self, active: dict[str, str], persona_id: str) -> None:
        # Derive activity PURELY from the event stream, mode-independently (I2). We
        # do NOT read run_id_var: in PARALLEL the orchestrator runs each turn in a
        # child task where run_id_var is set, invisible to this (parent) connection
        # task — so depending on it skipped mark_active in parallel. Instead, on the
        # FIRST event seen for a persona we mint a WS-local activity token and
        # mark_active with it; the matching terminal event marks_done the SAME token.
        if persona_id in active:
            return
        token = f"{persona_id}#{next(self._activity_seq)}"
        active[persona_id] = token
        self._sessions.mark_active(self._room_id, persona_id, token)

    def _track_end(self, active: dict[str, str], persona_id: str) -> None:
        # Balance mark_active with the SAME token recorded at start (opaque to the
        # store — it only powers the boolean is_busy / active_runs).
        token = active.pop(persona_id, None)
        if token is not None:
            self._sessions.mark_done(self._room_id, persona_id, token)

    def _clear_active(self, active: dict[str, str]) -> None:
        # Backstop: any persona still marked active (e.g. early disconnect mid-turn)
        # is released with its stored token so is_busy never sticks on.
        for persona_id, token in active.items():
            self._sessions.mark_done(self._room_id, persona_id, token)
        active.clear()

    # -- outbound frames ---------------------------------------------------- #

    async def _send_event(self, persona_id: str, event: StreamEvent) -> None:
        await self._ws.send_json(
            {"type": "event", "persona_id": persona_id, "event": event.model_dump()}
        )

    async def _send_error_card(self, persona_id: str, event: RunError) -> None:
        run_id = event.run_id
        command_redacted = ""
        if run_id is not None:
            run = self._services.runs.get(run_id)
            if run is not None:
                command_redacted = run.command_redacted
        log_url = f"/runs/{run_id}/log" if run_id is not None else None
        await self._ws.send_json(
            {
                "type": "error_card",
                "persona_id": persona_id,
                "run_id": run_id,
                "error_kind": event.error_kind,
                "message": event.message,
                "command_redacted": command_redacted,
                "log_url": log_url,
            }
        )

    async def _error(self, kind: str, message: str) -> None:
        await self._ws.send_json({"type": "error", "kind": kind, "message": message})

    # -- inbound resolution ------------------------------------------------- #

    async def _resolve_author(self, author_id: object) -> HumanAuthor | None:
        try:
            return self._services.authors.get(str(author_id))
        except TeamError as exc:
            await self._error(exc.kind, str(exc))
            return None

    async def _resolve_reply_mode(self, raw: object) -> ReplyMode | None:
        try:
            return ReplyMode(str(raw))
        except ValueError:
            await self._error("BadReplyMode", f"invalid reply_mode: {raw!r}")
            return None
