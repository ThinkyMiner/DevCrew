from __future__ import annotations


class TeamError(Exception):
    @property
    def kind(self) -> str:
        return type(self).__name__


class HarnessError(TeamError):
    pass


class HarnessTimeout(HarnessError):
    pass


class HarnessAuthError(HarnessError):
    pass


class NotFound(TeamError):
    """A requested resource (persona/room/author/run/log) does not exist.

    Mapped to HTTP 404. Distinct from :class:`TranscriptError`, whose message may
    also contain "not found" but signals a stale-cursor/data-integrity conflict.
    """


class SessionNotFound(TeamError):
    pass


class ProviderUnavailable(TeamError):
    pass


class TranscriptError(TeamError):
    pass
