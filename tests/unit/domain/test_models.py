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


def test_message_requires_known_author_kind():
    m = Message(room_id="r1", author_kind=AuthorKind.HUMAN, author_ref="me", content="hi")
    assert m.run_id is None
