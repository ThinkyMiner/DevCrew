"""WebSocket streaming endpoint (Task 5.3 / 5.4).

Drives the real ChatOrchestrator over the socket with a MockHarness registry
(no real CLI, fully deterministic). Asserts the frame protocol, error-card
translation, control-command handling, inbound-validation error frames, and
clean disconnect.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.health as health_mod
from app.api.app_factory import create_app
from app.domain.models import Provider
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry

# -- helpers -----------------------------------------------------------------


def _me(client: TestClient) -> str:
    authors = client.get("/authors").json()
    return next(a["id"] for a in authors if a["name"] == "Me")


def _make_persona(client: TestClient, handle: str = "alpha", name: str = "Alpha") -> dict:
    resp = client.post(
        "/personas",
        json={"name": name, "handle": handle, "provider": "mock", "model": "m"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _room_with_persona(client: TestClient, persona: dict) -> str:
    rid = client.post("/rooms", json={"name": "R"}).json()["id"]
    client.post(f"/rooms/{rid}/members", json={"persona_id": persona["id"]})
    return rid


def _drain_until(ws, *types: str) -> list[dict]:
    """Read frames until one of ``types`` is seen; return the collected frames."""
    frames: list[dict] = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] in types:
            return frames


# -- post / streaming --------------------------------------------------------


def test_post_streams_events_then_turn_complete_and_persists(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hello @{persona['handle']}",
                "reply_mode": "sequential",
                "quoted_ids": [],
            }
        )
        frames = _drain_until(ws, "turn_complete")

    event_frames = [f for f in frames if f["type"] == "event"]
    assert event_frames, "expected at least one streamed event frame"
    # Every event frame carries the persona_id and a serialized StreamEvent.
    for f in event_frames:
        assert f["persona_id"] == persona["id"]
        assert "kind" in f["event"]
    # Text deltas were streamed.
    assert any(f["event"]["kind"] == "text" for f in event_frames)
    # Terminal frame.
    assert frames[-1] == {"type": "turn_complete"}

    # A reply Message was persisted (human + persona reply).
    msgs = client.get(f"/rooms/{rid}/messages").json()
    persona_replies = [m for m in msgs if m["author_kind"] == "persona"]
    assert persona_replies, "persona reply should be persisted"


def test_post_no_target_completes_without_events(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        # No @mention -> no targets -> stored as context only.
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": "just thinking out loud",
                "reply_mode": "sequential",
            }
        )
        frame = ws.receive_json()

    assert frame == {"type": "turn_complete"}


# -- error card (FR-E2) ------------------------------------------------------


@pytest.fixture
def raising_registry() -> BackendRegistry:
    class RaisingHarness(MockHarness):
        async def run(self, spec):  # type: ignore[override]
            self.calls.append(spec)
            raise RuntimeError("backend boom")
            yield  # pragma: no cover - makes this an async generator

    reg = BackendRegistry()
    reg.register(Provider.MOCK, RaisingHarness())
    reg.register(Provider.CLAUDE, RaisingHarness())
    reg.register(Provider.CODEX, RaisingHarness())
    return reg


@pytest.fixture
def raising_client(test_settings, raising_registry, monkeypatch) -> TestClient:
    monkeypatch.setattr(health_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    return TestClient(create_app(test_settings, registry=raising_registry))


def test_failing_backend_yields_error_card(raising_client):
    client = raising_client
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hello @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")

    cards = [f for f in frames if f["type"] == "error_card"]
    assert len(cards) == 1, frames
    card = cards[0]
    assert card["persona_id"] == persona["id"]
    assert card["error_kind"] == "RuntimeError"
    assert "boom" in card["message"]
    assert card["command_redacted"]  # filled from the RunRecord
    run_id = card["run_id"]
    assert run_id
    assert card["log_url"] == f"/runs/{run_id}/log"

    # The log_url resolves to a real RunRecord whose log is fetchable.
    log_resp = client.get(card["log_url"])
    assert log_resp.status_code == 200
    assert "RuntimeError" in log_resp.text or "boom" in log_resp.text

    # No raw RunError event frame leaks for this persona (translated to a card).
    raw_errors = [f for f in frames if f["type"] == "event" and f["event"].get("kind") == "error"]
    assert not raw_errors


# -- symmetric TeamError handling on the post path (I1) ----------------------


def test_post_pre_yield_team_error_yields_error_frame_socket_stays_usable(client, monkeypatch):
    """post_message does synchronous repo work BEFORE its first yield, so a
    TeamError raised pre-yield would escape _handle_post -> serve() and crash the
    socket. The handler must convert it to an {type:error} frame and keep the
    socket usable (symmetric with _handle_command).

    A pre-yield TeamError is not naturally reachable with the current services
    (bad author/room/reply_mode are caught earlier), so we inject one by wrapping
    the real orchestrator's post_message to raise NotFound on the FIRST iteration
    of the returned async generator — exactly where post_message does its repo
    work before yielding.
    """
    from app.domain.errors import NotFound

    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    orchestrator = client.app.state.orchestrator
    real_post = orchestrator.post_message

    async def boom_post(*args, **kwargs):
        raise NotFound("room vanished mid-flight")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(orchestrator, "post_message", boom_post)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["kind"] == "NotFound"
        assert "vanished" in frame["message"]

        # Socket stays usable: restore the real method and post again.
        monkeypatch.setattr(orchestrator, "post_message", real_post)
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"again @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


# -- control commands --------------------------------------------------------


def _start_session(client, rid, persona, me) -> None:
    """Run a post turn so the persona has a live harness session to control."""
    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        _drain_until(ws, "turn_complete")


def test_command_supported_streams_then_command_complete(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)
    _start_session(client, rid, persona, me)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json({"type": "command", "persona_id": persona["id"], "command": "/compact"})
        frames = _drain_until(ws, "command_complete")

    event_frames = [f for f in frames if f["type"] == "event"]
    assert event_frames
    assert all(f["persona_id"] == persona["id"] for f in event_frames)
    assert frames[-1] == {"type": "command_complete"}


def test_command_unsupported_yields_error_frame_socket_stays_usable(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)
    _start_session(client, rid, persona, me)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json({"type": "command", "persona_id": persona["id"], "command": "/bogus"})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["kind"]  # HarnessError
        assert "/bogus" in frame["message"]

        # Socket still usable: a valid post afterward works.
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"again @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


def test_command_no_session_yields_error_frame(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json({"type": "command", "persona_id": persona["id"], "command": "/compact"})
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert frame["kind"] == "SessionNotFound"


# -- inbound validation ------------------------------------------------------


def test_bad_author_id_yields_error_frame(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": "ghost",
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["kind"] == "NotFound"
        # Socket stays usable.
        ws.send_json(
            {
                "type": "post",
                "author_id": _me(client),
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


def test_bad_reply_mode_yields_error_frame(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": "hi",
                "reply_mode": "diagonal",
            }
        )
        frame = ws.receive_json()
    assert frame["type"] == "error"


def test_unknown_frame_type_yields_error_frame(client):
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json({"type": "nonsense"})
        frame = ws.receive_json()
    assert frame["type"] == "error"


# -- malformed / binary inbound frames (C1) ----------------------------------


def test_non_json_text_frame_yields_bad_frame_error_socket_stays_usable(client):
    """A non-JSON text frame must not crash the socket: it yields a BadFrame error
    frame and the socket stays usable for a subsequent valid post."""
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_text("not json{")
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["kind"] == "BadFrame"

        # Socket still usable: a valid post afterward works.
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


def test_binary_frame_yields_bad_frame_error_socket_stays_usable(client):
    """A binary frame must not crash the socket (no KeyError on message['text']):
    it yields a BadFrame error frame and the socket stays usable."""
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_bytes(b"\x00\x01\x02binary")
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["kind"] == "BadFrame"

        # Socket still usable: a valid post afterward works.
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


# -- disconnect --------------------------------------------------------------


def test_early_disconnect_does_not_hang(client):
    """Client disconnects right after posting; server must not hang and the
    generator is closed cleanly (no leaked tasks)."""
    persona = _make_persona(client)
    rid = _room_with_persona(client, persona)
    me = _me(client)

    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"hi @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        # Read one frame then bail without draining — context exit disconnects.
        ws.receive_json()

    # A fresh connection still works -> server is healthy.
    with client.websocket_connect(f"/ws/rooms/{rid}") as ws:
        ws.send_json(
            {
                "type": "post",
                "author_id": me,
                "text": f"again @{persona['handle']}",
                "reply_mode": "sequential",
            }
        )
        frames = _drain_until(ws, "turn_complete")
    assert frames[-1] == {"type": "turn_complete"}


# -- is_busy in PARALLEL mode (I2) -------------------------------------------


@pytest.mark.asyncio
async def test_is_busy_transitions_in_parallel_mode():
    """is_busy must turn ON mid-stream and OFF after completion in PARALLEL mode
    (>=2 personas), derived from the event stream (NOT run_id_var).

    We drive the real _RoomConnection._handle_post consumption path against a fake
    orchestrator whose post_message interleaves events from two personas with real
    ``await asyncio.sleep(0)`` points (parallel-style). A gate pauses the stream
    after BOTH personas have emitted their first event; a concurrent observer then
    asserts is_busy(room, persona) is True for both, releases the gate, and after
    the drive completes asserts is_busy is False for both.

    Before the fix, _track_start read run_id_var (None on this consumer task in
    parallel), so mark_active was skipped and is_busy never turned True here.
    """
    import asyncio

    from app.api.ws import _RoomConnection
    from app.domain.events import RunDone, TextDelta
    from app.domain.models import HumanAuthor, ReplyMode
    from app.services.session_store import SessionStore

    room_id = "room1"
    pa, pb = "persona-a", "persona-b"

    both_started = asyncio.Event()  # set once both personas have emitted >=1 event
    release = asyncio.Event()  # observer flips this to let the streams finish

    class FakeOrchestrator:
        def post_message(self, room_id_, *, author, text, reply_mode, quoted_ids):
            async def gen():
                # Interleave: first event for A, then first for B (parallel-style),
                # pause until the observer has checked is_busy, then terminate both.
                yield (pa, TextDelta(text="a0"))
                await asyncio.sleep(0)
                yield (pb, TextDelta(text="b0"))
                both_started.set()
                await release.wait()
                yield (pa, RunDone(session_id="sa", run_id="ra"))
                yield (pb, RunDone(session_id="sb", run_id="rb"))

            return gen()

    class FakeAuthors:
        def get(self, author_id):
            return HumanAuthor(id=author_id, name="Me", weight_enabled=False)

    class FakeServices:
        authors = FakeAuthors()

    class FakeWS:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

    sessions = SessionStore(None, None)  # type: ignore[arg-type]
    conn = _RoomConnection(
        FakeWS(),  # type: ignore[arg-type]
        room_id,
        FakeServices(),  # type: ignore[arg-type]
        FakeOrchestrator(),  # type: ignore[arg-type]
        sessions,
    )

    # Sanity: nothing busy before the turn starts.
    assert not sessions.is_busy(room_id, pa)
    assert not sessions.is_busy(room_id, pb)

    drive = asyncio.create_task(
        conn._handle_post(
            {
                "type": "post",
                "author_id": "me",
                "text": "@a @b go",
                "reply_mode": ReplyMode.PARALLEL.value,
            }
        )
    )

    # Mid-stream: once both personas have emitted, BOTH must read as busy.
    await asyncio.wait_for(both_started.wait(), timeout=1.0)
    assert sessions.is_busy(room_id, pa), "persona A should be busy mid-stream (parallel)"
    assert sessions.is_busy(room_id, pb), "persona B should be busy mid-stream (parallel)"

    # Let the streams finish, then both must read as not busy.
    release.set()
    await asyncio.wait_for(drive, timeout=1.0)
    assert not sessions.is_busy(room_id, pa)
    assert not sessions.is_busy(room_id, pb)
