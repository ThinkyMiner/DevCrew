from __future__ import annotations

import app.api.health as health_mod
from app.api.health import check_health
from app.config.settings import Settings


def test_both_present_ok(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    report = check_health(Settings())
    assert report.ok is True
    assert report.claude_found is True
    assert report.codex_found is True
    # auth is never probed -> unknown
    assert report.claude_authenticated is None
    assert report.codex_authenticated is None


def test_both_absent_not_ok_with_messages(monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", lambda name: None)
    report = check_health(Settings())
    assert report.ok is False
    assert report.claude_found is False
    assert report.codex_found is False
    assert any("claude" in m for m in report.messages)
    assert any("codex" in m for m in report.messages)


def test_one_missing_not_ok(monkeypatch):
    monkeypatch.setattr(
        health_mod.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    report = check_health(Settings())
    assert report.ok is False
    assert report.claude_found is True
    assert report.codex_found is False
