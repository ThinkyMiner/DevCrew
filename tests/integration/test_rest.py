from __future__ import annotations

import app.api.health as health_mod
from app.api.app_factory import create_app

# -- health -----------------------------------------------------------------


def test_health_ok_when_both_present(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["claude_found"] is True
    assert body["codex_found"] is True


def test_health_not_ok_when_absent(test_settings, registry, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(health_mod.shutil, "which", lambda name: None)
    app = create_app(test_settings, registry=registry)
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["messages"]


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200


# -- personas ----------------------------------------------------------------


def _make_persona(client, handle="alpha", name="Alpha"):
    resp = client.post(
        "/personas",
        json={"name": name, "handle": handle, "provider": "mock", "model": "m"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_persona_crud_lifecycle(client):
    created = _make_persona(client)
    pid = created["id"]
    assert created["handle"] == "alpha"

    assert any(p["id"] == pid for p in client.get("/personas").json())
    assert client.get(f"/personas/{pid}").json()["name"] == "Alpha"

    upd = client.patch(f"/personas/{pid}", json={"name": "Renamed"})
    assert upd.status_code == 200
    assert upd.json()["name"] == "Renamed"
    assert upd.json()["handle"] == "alpha"  # untouched

    dup = client.post(f"/personas/{pid}/duplicate")
    assert dup.status_code == 201
    assert dup.json()["id"] != pid

    assert client.delete(f"/personas/{pid}").status_code == 204
    assert client.get(f"/personas/{pid}").status_code == 404


def test_persona_ignores_client_supplied_id(client):
    # The Create body has no id field; an extra one is ignored, never honored.
    resp = client.post(
        "/personas",
        json={
            "name": "X",
            "handle": "xx",
            "provider": "mock",
            "model": "m",
            "id": "client-chosen",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["id"] != "client-chosen"


def test_persona_templates(client):
    tmpl = client.post(
        "/personas",
        json={
            "name": "T",
            "handle": "tmpl",
            "provider": "mock",
            "model": "m",
            "is_template": True,
        },
    ).json()
    templates = client.get("/personas/templates").json()
    assert [t["id"] for t in templates] == [tmpl["id"]]
    inst = client.post(f"/personas/{tmpl['id']}/from-template")
    assert inst.status_code == 201
    assert inst.json()["is_template"] is False


# -- authors -----------------------------------------------------------------


def test_authors_seeded_me_and_boss(client):
    names = {a["name"] for a in client.get("/authors").json()}
    assert {"Me", "Boss"} <= names


def test_author_crud(client):
    created = client.post("/authors", json={"name": "Guest"}).json()
    aid = created["id"]
    upd = client.patch(f"/authors/{aid}", json={"color": "#ffffff"})
    assert upd.json()["color"] == "#ffffff"
    assert client.delete(f"/authors/{aid}").status_code == 204
    assert client.get(f"/authors/{aid}").status_code == 404


# -- rooms -------------------------------------------------------------------


def test_room_crud_and_patch(client):
    room = client.post("/rooms", json={"name": "General"}).json()
    rid = room["id"]
    patched = client.patch(
        f"/rooms/{rid}",
        json={
            "name": "Renamed",
            "topic": "Topic",
            "default_reply_mode": "parallel",
            "archived": True,
        },
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Renamed"
    assert body["topic"] == "Topic"
    assert body["default_reply_mode"] == "parallel"
    assert body["archived"] is True
    assert client.delete(f"/rooms/{rid}").status_code == 204
    assert client.get(f"/rooms/{rid}").status_code == 404


def test_room_membership(client):
    rid = client.post("/rooms", json={"name": "R"}).json()["id"]
    a = _make_persona(client, handle="alpha", name="Alpha")
    b = _make_persona(client, handle="beta", name="Beta")

    client.post(f"/rooms/{rid}/members", json={"persona_id": b["id"]})
    members = client.post(f"/rooms/{rid}/members", json={"persona_id": a["id"]}).json()
    # membership preserves insertion order
    assert [m["id"] for m in members] == [b["id"], a["id"]]

    listed = client.get(f"/rooms/{rid}/members").json()
    assert [m["id"] for m in listed] == [b["id"], a["id"]]

    assert client.delete(f"/rooms/{rid}/members/{b['id']}").status_code == 204
    remaining = client.get(f"/rooms/{rid}/members").json()
    assert [m["id"] for m in remaining] == [a["id"]]


# -- messages ----------------------------------------------------------------


def test_messages_chronological_and_pagination(client):
    rid = client.post("/rooms", json={"name": "R"}).json()["id"]
    me = next(a for a in client.get("/authors").json() if a["name"] == "Me")

    # Seed messages directly through the repo on app.state.
    from app.domain.models import AuthorKind, Message

    repo = client.app.state.services.messages
    for i in range(3):
        repo.create(
            Message(
                room_id=rid,
                author_kind=AuthorKind.HUMAN,
                author_ref=me["id"],
                content=f"msg-{i}",
            )
        )

    all_msgs = client.get(f"/rooms/{rid}/messages").json()
    assert [m["content"] for m in all_msgs] == ["msg-0", "msg-1", "msg-2"]

    page = client.get(f"/rooms/{rid}/messages", params={"offset": 1, "limit": 1}).json()
    assert [m["content"] for m in page] == ["msg-1"]


def test_messages_missing_room_404(client):
    assert client.get("/rooms/ghost/messages").status_code == 404


# -- run log -----------------------------------------------------------------


def test_run_log_fetch(client):
    from datetime import UTC, datetime

    from app.domain.events import RunDone, TextDelta
    from app.domain.models import RunRecord

    run_log = client.app.state.services.run_log
    runs = client.app.state.services.runs

    writer = run_log.open("room1", "persona1", "run-xyz")
    writer.write_event(TextDelta(text="hello"))
    writer.write_event(RunDone(session_id="s1"))

    runs.create(
        RunRecord(
            run_id="run-xyz",
            room_id="room1",
            persona_id="persona1",
            command_redacted="mock",
            log_path=str(writer.path),
            started_at=datetime.now(UTC),
        )
    )

    resp = client.get("/runs/run-xyz/log")
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert len(lines) == 2
    assert "hello" in lines[0]


def test_run_log_unknown_404(client):
    assert client.get("/runs/nope/log").status_code == 404


def test_run_log_path_outside_logs_dir_404(client, tmp_path):
    """A log_path pointing OUTSIDE the configured logs dir must 404, not serve."""
    from datetime import UTC, datetime

    from app.domain.models import RunRecord

    # A real, readable file but located outside resolved_logs_dir.
    outside = tmp_path / "escape.jsonl"
    outside.write_text('{"x": 1}\n', encoding="utf-8")

    runs = client.app.state.services.runs
    runs.create(
        RunRecord(
            run_id="run-escape",
            room_id="room1",
            persona_id="persona1",
            command_redacted="mock",
            log_path=str(outside),
            started_at=datetime.now(UTC),
        )
    )

    resp = client.get("/runs/run-escape/log")
    assert resp.status_code == 404
    assert resp.json()["error"]["kind"] == "NotFound"


# -- error mapping -----------------------------------------------------------


def test_missing_persona_maps_to_404(client):
    resp = client.get("/personas/does-not-exist")
    assert resp.status_code == 404
    # Now classified by type, not message substring.
    assert resp.json()["error"]["kind"] == "NotFound"


def test_error_body_only_exposes_kind_and_message(client):
    resp = client.get("/personas/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"kind", "message"}  # no stack trace leaked


# -- lifespan ----------------------------------------------------------------


def test_lifespan_closes_db_and_seeds_within_context(test_settings, registry, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(health_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    app = create_app(test_settings, registry=registry)

    with TestClient(app) as client:
        # Startup seeding visible inside the lifespan context.
        names = {a["name"] for a in client.get("/authors").json()}
        assert {"Me", "Boss"} <= names

    # After the context exits, the lifespan shutdown closed the connection.
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.ProgrammingError):
        app.state.db.query("SELECT 1")
