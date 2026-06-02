"""Startup health check (FR-E1).

Verifies that the ``claude`` and ``codex`` CLIs are resolvable on ``PATH`` via
:func:`shutil.which`. This is the reliable, cheap signal we always report.

Auth probing
------------
FR-E1 also wants a best-effort auth/availability signal. We deliberately do NOT
spawn the real CLIs here: a real auth probe would launch an interactive/long
process and (for some providers) burn tokens, which violates the "fast, cheap,
no tokens" constraint. There is no documented, reliable, side-effect-free
"am I logged in?" subcommand we trust across both CLIs, so rather than fabricate
a result we report ``*_authenticated=None`` ("unknown") with an explanatory
note. ``ok`` reflects only PATH resolvability — the thing we can verify
honestly. The seam is here if a cheap, verified probe is added later.
"""

from __future__ import annotations

import shutil

from pydantic import BaseModel

from app.config.settings import Settings


class HealthReport(BaseModel):
    ok: bool
    claude_found: bool
    codex_found: bool
    claude_authenticated: bool | None = None
    codex_authenticated: bool | None = None
    messages: list[str]


def check_health(settings: Settings) -> HealthReport:
    """Return a structured health report for the configured CLIs (no spawn)."""
    claude_found = shutil.which(settings.claude_bin) is not None
    codex_found = shutil.which(settings.codex_bin) is not None

    messages: list[str] = []
    if not claude_found:
        messages.append(f"claude CLI not found on PATH (looked for {settings.claude_bin!r})")
    if not codex_found:
        messages.append(f"codex CLI not found on PATH (looked for {settings.codex_bin!r})")
    messages.append(
        "auth status not probed (avoids interactive/token-burning calls); "
        "authenticated fields are 'unknown'"
    )

    return HealthReport(
        ok=claude_found and codex_found,
        claude_found=claude_found,
        codex_found=codex_found,
        claude_authenticated=None,
        codex_authenticated=None,
        messages=messages,
    )
