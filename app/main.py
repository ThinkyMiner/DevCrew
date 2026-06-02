"""Composition-root entrypoint: run the real app with the real CLI adapters.

``python -m app.main`` builds the app via :func:`create_app` (which wires the
real ``claude``/``codex`` adapters through :func:`default_registry`) and serves
it with uvicorn bound to the loopback host/port from :class:`Settings`
(NFR-1 — local only).

Import-safe: building the app (:func:`build_app`) has no network side effects,
and ``uvicorn.run`` is invoked ONLY under ``if __name__ == "__main__"``, so
importing this module (e.g. for tests/tooling) never starts a server.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.app_factory import create_app
from app.config.settings import Settings, default_settings


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the production app (real CLI registry) without serving it."""
    return create_app(settings or default_settings())


def main() -> None:
    """Serve the app on the configured loopback host/port."""
    import uvicorn

    settings = default_settings()
    print(f"[team] http://{settings.host}:{settings.port}")
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
