from app.domain.models import PermissionMode, Persona, Provider
from app.services.routing import resolve_targets


def _persona(handle: str) -> Persona:
    return Persona(
        name=handle.title(),
        handle=handle,
        provider=Provider.MOCK,
        model="mock",
        permission_mode=PermissionMode.READ_ONLY,
    )


ARCH = _persona("architect")
RES = _persona("researcher")
CRITIC = _persona("critic")
ROOM = [ARCH, RES, CRITIC]


def _handles(personas: list[Persona]) -> list[str]:
    return [p.handle for p in personas]


def test_no_mention_returns_empty() -> None:
    assert resolve_targets("just thinking out loud", ROOM) == []


def test_unknown_handle_ignored() -> None:
    assert resolve_targets("hey @nobody you there", ROOM) == []


def test_everyone_returns_all_in_room_order() -> None:
    assert _handles(resolve_targets("@everyone status?", ROOM)) == [
        "architect",
        "researcher",
        "critic",
    ]


def test_specific_mentions_in_mention_order() -> None:
    out = resolve_targets("@critic and @architect please", ROOM)
    assert _handles(out) == ["critic", "architect"]


def test_dedupe_repeated_mention() -> None:
    out = resolve_targets("@architect @architect once", ROOM)
    assert _handles(out) == ["architect"]


def test_everyone_plus_extra_handle_dedupes() -> None:
    # @everyone yields room order; the explicit @critic is already included.
    out = resolve_targets("@everyone and especially @critic", ROOM)
    assert _handles(out) == ["architect", "researcher", "critic"]


def test_mix_known_and_unknown() -> None:
    out = resolve_targets("@researcher @ghost @critic", ROOM)
    assert _handles(out) == ["researcher", "critic"]
