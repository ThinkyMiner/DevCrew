"""Token-free dev launcher: run the full app with MockHarness for every provider.

Click around the real web UI at http://127.0.0.1:8000 with ZERO token cost — no
real claude/codex subprocess is ever spawned. The MockHarness echoes prompts and
emits deterministic text/usage/done events, so streaming, the transcript, quote
chips, reply modes, the session console (/compact, /clear, reset), and error
cards can all be exercised end to end.

On startup it seeds a demo room + two mock personas (if none exist) so the page
is immediately usable. Re-running reuses whatever is already in the dev db.

Run:
    source .venv/bin/activate
    python -m scripts.dev_mock
    # then open http://127.0.0.1:8000

Override the data dir (default ./data-dev so it never touches real data):
    TEAM_DATA_DIR=./scratch python -m scripts.dev_mock
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

import app.api.health as health_mod
from app.api.app_factory import create_app
from app.config.settings import Settings
from app.domain.models import Persona, Provider, ReplyMode, Room
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry


def _mock_registry() -> BackendRegistry:
    """A registry where CLAUDE/CODEX/MOCK all resolve to one MockHarness."""
    reg = BackendRegistry()
    mock = MockHarness()
    reg.register(Provider.MOCK, mock)
    reg.register(Provider.CLAUDE, mock)
    reg.register(Provider.CODEX, mock)
    return reg


def _seed(app) -> None:
    """Seed a demo room + two mock personas if the db is empty (idempotent-ish)."""
    services = app.state.services
    if services.rooms.list():
        return
    p1 = services.personas.create(
        Persona(
            name="Architect",
            handle="arch",
            provider=Provider.MOCK,
            model="mock",
            color="#7aa2ff",
        )
    )
    p2 = services.personas.create(
        Persona(
            name="Critic",
            handle="critic",
            provider=Provider.MOCK,
            model="mock",
            color="#f0b35e",
        )
    )
    room = services.rooms.create(
        Room(
            name="demo",
            topic="Mock playground — no tokens spent",
            default_reply_mode=ReplyMode.SEQUENTIAL,
        )
    )
    services.rooms.add_member(room.id, p1.id)
    services.rooms.add_member(room.id, p2.id)
    print(f"[dev_mock] seeded room #{room.name} with @{p1.handle}, @{p2.handle}")


def main() -> None:
    data_dir = Path(os.environ.get("TEAM_DATA_DIR", "data-dev"))
    settings = Settings(data_dir=data_dir, db_path=data_dir / "team.db", logs_dir=data_dir / "logs")

    # The health route shells out to `shutil.which`; pretend both CLIs are
    # present so the banner is green even on a machine without them installed.
    health_mod.shutil.which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]

    app = create_app(settings, registry=_mock_registry())
    _seed(app)

    print("[dev_mock] http://127.0.0.1:8000  (MockHarness — zero token cost)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
