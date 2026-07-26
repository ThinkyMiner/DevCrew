from __future__ import annotations

import pytest

from app.config.settings import Settings


def test_harness_timeout_defaults_to_ten_hours() -> None:
    # Long persona turns (deep research / high effort / large resumed context)
    # must not be cut off at the conservative process default; the app ships a
    # 10-hour cap.
    assert Settings().harness_timeout == 36000.0


def test_harness_timeout_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Operators tune the cap without a code change (TEAM_-prefixed, like the
    # other settings).
    monkeypatch.setenv("TEAM_HARNESS_TIMEOUT", "1800")
    assert Settings().harness_timeout == 1800.0
