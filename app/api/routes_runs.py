"""Run-log fetch (FR-T4/FR-D3): stream a run's JSONL log file.

Locates the file via the persisted ``RunRecord.log_path``. Raises
:class:`~app.domain.errors.NotFound` (mapped to 404 by the app factory) if the
run is unknown, has no recorded log path, the file is missing on disk
(fail-loud rather than serving an empty body), or the resolved path escapes the
configured logs directory. The path-boundary check is defense-in-depth: today
``log_path`` is server-written, but resolving and confining it to
``settings.resolved_logs_dir`` makes the trust boundary explicit so a future
client-influenceable id can never become an arbitrary-file read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import get_run_repo, get_settings
from app.config.settings import Settings
from app.domain.errors import NotFound
from app.persistence.repositories import RunRepo

router = APIRouter(prefix="/runs", tags=["runs"])

RepoDep = Annotated[RunRepo, Depends(get_run_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/{run_id}/log")
def get_run_log(run_id: str, repo: RepoDep, settings: SettingsDep) -> FileResponse:
    run = repo.get(run_id)
    if run is None:
        raise NotFound(f"run not found: {run_id}")
    if run.log_path is None:
        raise NotFound(f"run has no log: {run_id}")
    path = Path(run.log_path).resolve()
    logs_dir = settings.resolved_logs_dir.resolve()
    # Trust-boundary: confine served files to the configured logs dir.
    if not path.is_relative_to(logs_dir):
        raise NotFound(f"run log outside logs dir: {run_id}")
    if not path.is_file():
        raise NotFound(f"run log file missing: {run_id}")
    # JSONL: serve as plain text so browsers/clients display the lines directly.
    return FileResponse(path, media_type="application/x-ndjson", filename=path.name)
