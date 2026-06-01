"""Application composition root.

:func:`create_app` is the ONE place that wires settings -> db -> repos ->
services -> orchestrator -> routers and builds the real :class:`BackendRegistry`.
This is the only module in the ``api`` layer permitted to import harness
adapters (to wire the real CLIs); everything else in ``api`` depends solely on
services/domain.

The ``registry`` seam keeps the REAL claude/codex CLIs out of tests: tests pass
a ``BackendRegistry`` with a :class:`MockHarness` registered, so no test ever
spawns a real process. When ``registry`` is ``None`` the factory builds the real
one via :func:`default_registry`.

TeamError -> HTTP mapping is centralized here as FastAPI exception handlers, so
routes raise/let domain errors propagate and never hand-build error JSON.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import (
    routes_authors,
    routes_messages,
    routes_personas,
    routes_rooms,
    routes_runs,
    ws,
)
from app.api.deps import Services
from app.api.health import HealthReport, check_health
from app.config.logging import configure_logging
from app.config.settings import Settings, default_settings
from app.domain.errors import (
    NotFound,
    ProviderUnavailable,
    TeamError,
    TranscriptError,
)
from app.harness.registry import BackendRegistry, default_registry
from app.persistence.db import Database
from app.persistence.repositories import (
    AuthorRepo,
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    RunRepo,
    SessionRepo,
)
from app.persistence.run_log import RunLogStore
from app.services.authors import AuthorService
from app.services.orchestrator import ChatOrchestrator
from app.services.personas import PersonaService
from app.services.rooms import RoomService
from app.services.session_store import SessionStore


def _status_for(error: TeamError) -> int:
    """Map a TeamError to an HTTP status BY TYPE (never by message substring).

    - :class:`NotFound` -> 404 (missing resource).
    - :class:`ProviderUnavailable` -> 503 (backend CLI unavailable).
    - :class:`TranscriptError` -> 409 (stale-cursor / data-integrity conflict;
      its message may contain "not found" but it is NOT a missing resource).
    - any other :class:`TeamError` -> 400 (bad request).
    """
    if isinstance(error, NotFound):
        return 404
    if isinstance(error, ProviderUnavailable):
        return 503
    if isinstance(error, TranscriptError):
        return 409
    return 400


def create_app(
    settings: Settings | None = None,
    *,
    registry: BackendRegistry | None = None,
) -> FastAPI:
    settings = settings or default_settings()
    configure_logging()

    # Ensure on-disk locations exist before opening the db / writing logs.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_logs_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database(settings.resolved_db_path)
    db.init_schema()

    persona_repo = PersonaRepo(db)
    author_repo = AuthorRepo(db)
    room_repo = RoomRepo(db)
    message_repo = MessageRepo(db)
    session_repo = SessionRepo(db)
    run_repo = RunRepo(db)
    run_log = RunLogStore(settings.resolved_logs_dir)  # FR-D3 location

    backend_registry = registry if registry is not None else default_registry()

    persona_service = PersonaService(persona_repo)
    author_service = AuthorService(author_repo)
    room_service = RoomService(room_repo, persona_repo)
    session_store = SessionStore(session_repo, persona_repo)
    orchestrator = ChatOrchestrator(
        persona_repo=persona_repo,
        author_repo=author_repo,
        room_repo=room_repo,
        message_repo=message_repo,
        session_repo=session_repo,
        run_repo=run_repo,
        run_log=run_log,
        registry=backend_registry,
    )

    # Seed default authors (FR-A1/A3) at startup; idempotent.
    author_service.ensure_defaults()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup work already happened eagerly above (schema + seeding) so a
        # bare TestClient still sees a ready app; here we only register the
        # shutdown hook that releases the sqlite connection (no resource leak).
        try:
            yield
        finally:
            db.close()

    app = FastAPI(title="Team", lifespan=lifespan)
    app.state.db = db
    app.state.registry = backend_registry
    app.state.session_store = session_store
    app.state.orchestrator = orchestrator
    app.state.services = Services(
        settings=settings,
        personas=persona_service,
        authors=author_service,
        rooms=room_service,
        messages=message_repo,
        runs=run_repo,
        run_log=run_log,
    )

    @app.exception_handler(TeamError)
    async def _team_error_handler(_request: Request, exc: TeamError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for(exc),
            content={"error": {"kind": exc.kind, "message": str(exc)}},
        )

    @app.get("/health")
    def health() -> HealthReport:
        return check_health(settings)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"app": "team", "status": "ok"}

    app.include_router(routes_personas.router)
    app.include_router(routes_authors.router)
    app.include_router(routes_rooms.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_runs.router)

    ws.register_ws(app)

    return app
