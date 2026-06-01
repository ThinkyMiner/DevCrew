from __future__ import annotations

from app.api.app_factory import _status_for
from app.domain.errors import (
    NotFound,
    ProviderUnavailable,
    TeamError,
    TranscriptError,
)


def test_not_found_maps_to_404() -> None:
    assert _status_for(NotFound("persona not found: x")) == 404


def test_provider_unavailable_maps_to_503() -> None:
    assert _status_for(ProviderUnavailable("claude down")) == 503


def test_transcript_error_maps_to_409_not_404() -> None:
    # Its message contains "not found" but it is a stale-cursor integrity
    # conflict, NOT a missing resource — must never be classified as 404.
    err = TranscriptError("last_seen_id 'abc' not found in messages")
    assert _status_for(err) == 409


def test_default_team_error_maps_to_400() -> None:
    assert _status_for(TeamError("bad input")) == 400
