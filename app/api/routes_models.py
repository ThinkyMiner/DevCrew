"""Model catalog endpoint: the dynamic, backend-owned model list.

``GET /models`` returns ``{provider: [model, ...]}`` for every backend the
composition root wired into the registry. The persona editor fetches this so its
model picker follows the real adapters (e.g. a newly-added ``fable`` alias shows
up automatically) instead of a hardcoded frontend list that silently drifts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_registry
from app.harness.registry import BackendRegistry

router = APIRouter(tags=["models"])

RegistryDep = Annotated[BackendRegistry, Depends(get_registry)]


@router.get("/models")
def list_models(registry: RegistryDep) -> dict[str, list[str]]:
    return registry.list_models()
