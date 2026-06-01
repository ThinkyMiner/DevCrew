"""Run-log fetch (FR-T4/FR-D3): stream a run's JSONL log file.

Locates the file via the persisted ``RunRecord.log_path``. Returns 404 if the
run is unknown, has no recorded log path, or the file is missing on disk
(fail-loud rather than serving an empty body).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import get_run_repo
from app.domain.errors import TeamError
from app.persistence.repositories import RunRepo

router = APIRouter(prefix="/runs", tags=["runs"])

RepoDep = Annotated[RunRepo, Depends(get_run_repo)]


@router.get("/{run_id}/log")
def get_run_log(run_id: str, repo: RepoDep) -> FileResponse:
    run = repo.get(run_id)
    if run is None:
        raise TeamError(f"run not found: {run_id}")
    if run.log_path is None:
        raise TeamError(f"run has no log: {run_id}")
    path = Path(run.log_path)
    if not path.is_file():
        raise TeamError(f"run log file missing: {run_id}")
    # JSONL: serve as plain text so browsers/clients display the lines directly.
    return FileResponse(path, media_type="application/x-ndjson", filename=path.name)
