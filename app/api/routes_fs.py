"""Folder-picker endpoint: list directories so the UI can choose a room's
shared ``working_dir`` without the operator typing a raw path.

Thin: delegates to :func:`app.services.fs_browser.list_dirs`. Read-only and
loopback-only (see that module). A missing/non-directory path raises ``NotFound``
(mapped to 404 by the app factory).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.services.fs_browser import DirListing, list_dirs

router = APIRouter(prefix="/fs", tags=["fs"])


@router.get("/dirs")
def list_directories(path: str | None = None) -> DirListing:
    return list_dirs(path)
