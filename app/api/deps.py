"""FastAPI dependency providers.

Routes depend on these instead of reaching into ``app.state`` directly, which
keeps the routes thin and decoupled from how the composition root stored things.
Everything is built once in the app factory and stashed on ``app.state``; these
just read it back, typed.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config.settings import Settings
from app.persistence.repositories import MessageRepo, RunRepo
from app.persistence.run_log import RunLogStore
from app.services.authors import AuthorService
from app.services.personas import PersonaService
from app.services.rooms import RoomService


@dataclass
class Services:
    """Container for the wired services + bits routes need (composition root output)."""

    settings: Settings
    personas: PersonaService
    authors: AuthorService
    rooms: RoomService
    messages: MessageRepo
    runs: RunRepo
    run_log: RunLogStore


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_settings(request: Request) -> Settings:
    services: Services = request.app.state.services
    return services.settings


def get_persona_service(request: Request) -> PersonaService:
    return get_services(request).personas


def get_author_service(request: Request) -> AuthorService:
    return get_services(request).authors


def get_room_service(request: Request) -> RoomService:
    return get_services(request).rooms


def get_message_repo(request: Request) -> MessageRepo:
    return get_services(request).messages


def get_run_repo(request: Request) -> RunRepo:
    return get_services(request).runs
