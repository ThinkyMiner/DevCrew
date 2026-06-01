from app.domain.errors import (
    HarnessAuthError,
    HarnessError,
    HarnessTimeout,
    ProviderUnavailable,
    SessionNotFound,
    TeamError,
)


def test_hierarchy_and_kind():
    subclasses = (
        HarnessError,
        HarnessTimeout,
        HarnessAuthError,
        SessionNotFound,
        ProviderUnavailable,
    )
    for exc in subclasses:
        assert issubclass(exc, TeamError)
    assert HarnessTimeout("x").kind == "HarnessTimeout"
