"""Author CRUD (FR-A1/A2). The seeded Me/Boss appear in the list after startup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_author_service
from app.api.schemas import AuthorCreate, AuthorUpdate
from app.domain.models import HumanAuthor
from app.services.authors import AuthorService

router = APIRouter(prefix="/authors", tags=["authors"])

ServiceDep = Annotated[AuthorService, Depends(get_author_service)]


@router.post("", status_code=201)
def create_author(body: AuthorCreate, service: ServiceDep) -> HumanAuthor:
    return service.create(HumanAuthor(**body.model_dump()))


@router.get("")
def list_authors(service: ServiceDep) -> list[HumanAuthor]:
    return service.list()


@router.get("/{author_id}")
def get_author(author_id: str, service: ServiceDep) -> HumanAuthor:
    return service.get(author_id)


@router.patch("/{author_id}")
@router.put("/{author_id}")
def update_author(author_id: str, body: AuthorUpdate, service: ServiceDep) -> HumanAuthor:
    existing = service.get(author_id)
    updated = existing.model_copy(update=body.model_dump(exclude_unset=True))
    return service.update(updated)


@router.delete("/{author_id}", status_code=204)
def delete_author(author_id: str, service: ServiceDep) -> None:
    service.get(author_id)  # 404 if missing
    service.delete(author_id)
