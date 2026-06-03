"""Application settings (pydantic-settings).

Env-overridable with the ``TEAM_`` prefix (e.g. ``TEAM_PORT=9000``). All paths
default under ``data_dir`` so a single override repoints the whole app's
on-disk state. ``host`` defaults to loopback per NFR-1 (local-only).

``db_path`` / ``logs_dir`` are derived from ``data_dir`` after init unless the
caller (or env) sets them explicitly, so overriding ``data_dir`` alone is
enough to move everything together.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEAM_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("data")
    db_path: Path | None = None
    logs_dir: Path | None = None
    claude_bin: str = "claude"
    codex_bin: str = "codex"

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        if self.db_path is None:
            self.db_path = self.data_dir / "team.db"
        if self.logs_dir is None:
            self.logs_dir = self.data_dir / "logs"
        return self

    @property
    def resolved_db_path(self) -> Path:
        assert self.db_path is not None  # set by validator
        return self.db_path

    @property
    def resolved_logs_dir(self) -> Path:
        assert self.logs_dir is not None  # set by validator
        return self.logs_dir

    @property
    def resolved_scratch_dir(self) -> Path:
        """Neutral, EMPTY directory used as the spawn cwd for personas with no
        bound working_dir (persona environment isolation).

        MUST live OUTSIDE the project tree: claude/codex discover CLAUDE.md /
        AGENTS.md by walking UP the directory tree, so a scratch dir under
        ``data_dir`` (inside the repo) would still pick up this project's docs
        and make a persona behave like a "Team coding agent". Placing it under
        the system temp dir means the walk-up finds no project docs. (Verified
        live: a scratch dir inside the repo loaded the repo's CLAUDE.md.)
        """
        return Path(tempfile.gettempdir()) / "team-persona-scratch"


def default_settings() -> Settings:
    """Build a :class:`Settings` from defaults + environment (the factory)."""
    return Settings()
