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


class SessionNotFound(TeamError):
    pass


class ProviderUnavailable(TeamError):
    pass


class TranscriptError(TeamError):
    pass
