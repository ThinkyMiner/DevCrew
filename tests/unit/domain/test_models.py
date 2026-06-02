import pytest
from pydantic import ValidationError

from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Message,
    Persona,
    Provider,
)


def test_persona_handle_normalised_and_validated():
    p = Persona(name="Architect", handle="Architect", provider=Provider.CLAUDE, model="opus")
    assert p.handle == "architect"  # lowercased, no leading @
    with pytest.raises(ValidationError):
        Persona(name="x", handle="has space", provider=Provider.CLAUDE, model="m")


def test_boss_author_weight_enabled_by_default():
    boss = HumanAuthor(name="Boss", weight_note="Leadership input — weight heavily.")
    assert boss.weight_enabled is True


def test_persona_handle_invariant_total_under_mutation():
    p = Persona(name="Architect", handle="architect", provider=Provider.CLAUDE, model="opus")
    with pytest.raises(ValidationError):
        p.handle = "has space"


def test_message_dedupes_quoted_ids_preserving_order():
    m = Message(
        room_id="r1",
        author_kind=AuthorKind.HUMAN,
        author_ref="me",
        content="hi",
        quoted_message_ids=["x", "x", "y"],
    )
    assert m.quoted_message_ids == ["x", "y"]


def test_message_requires_known_author_kind():
    m = Message(room_id="r1", author_kind=AuthorKind.HUMAN, author_ref="me", content="hi")
    assert m.run_id is None
    with pytest.raises(ValidationError):
        Message(room_id="r1", author_kind="alien", author_ref="me", content="hi")  # type: ignore[arg-type]
