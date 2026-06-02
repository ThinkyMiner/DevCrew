"""Persona CRUD + duplicate + templates (FR-P1). Thin: delegates to PersonaService."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_persona_service
from app.api.schemas import PersonaCreate, PersonaUpdate
from app.domain.models import Persona
from app.services.personas import PersonaService

router = APIRouter(prefix="/personas", tags=["personas"])

ServiceDep = Annotated[PersonaService, Depends(get_persona_service)]


@router.post("", status_code=201)
def create_persona(body: PersonaCreate, service: ServiceDep) -> Persona:
    return service.create(Persona(**body.model_dump()))


@router.get("")
def list_personas(service: ServiceDep) -> list[Persona]:
    return service.list()


@router.get("/templates")
def list_templates(service: ServiceDep) -> list[Persona]:
    return service.list_templates()


@router.get("/{persona_id}")
def get_persona(persona_id: str, service: ServiceDep) -> Persona:
    return service.get(persona_id)


@router.patch("/{persona_id}")
@router.put("/{persona_id}")
def update_persona(persona_id: str, body: PersonaUpdate, service: ServiceDep) -> Persona:
    existing = service.get(persona_id)
    updated = existing.model_copy(update=body.model_dump(exclude_unset=True))
    return service.update(updated)


@router.post("/{persona_id}/duplicate", status_code=201)
def duplicate_persona(persona_id: str, service: ServiceDep) -> Persona:
    return service.duplicate(persona_id)


@router.post("/{persona_id}/from-template", status_code=201)
def create_from_template(persona_id: str, service: ServiceDep) -> Persona:
    return service.create_from_template(persona_id)


@router.delete("/{persona_id}", status_code=204)
def delete_persona(persona_id: str, service: ServiceDep) -> None:
    service.get(persona_id)  # 404 if missing
    service.delete(persona_id)
