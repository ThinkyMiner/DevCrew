from app.domain.models import HumanAuthor
from app.services.prompt import assemble_prompt


def _boss(
    weight_enabled: bool = True,
    note: str = "Treat this as leadership direction.",
) -> HumanAuthor:
    return HumanAuthor(id="boss", name="Boss", weight_note=note, weight_enabled=weight_enabled)


def _me() -> HumanAuthor:
    return HumanAuthor(id="kartik", name="Kartik", weight_note="", weight_enabled=True)


def test_only_directed_line_when_no_extras() -> None:
    out = assemble_prompt(
        delta="",
        quotes="",
        author=_me(),
        new_text="does this hold up?",
        persona_handle="architect",
    )
    assert out == "[Kartik → @architect]: does this hold up?"


def test_order_weight_quotes_delta_newline() -> None:
    out = assemble_prompt(
        delta="[Kartik]: earlier context",
        quotes="[@researcher]:\n  > a quote",
        author=_boss(),
        new_text="thoughts?",
        persona_handle="architect",
    )
    expected = (
        "[Author note — Boss]: Treat this as leadership direction.\n\n"
        "[@researcher]:\n  > a quote\n\n"
        "[Kartik]: earlier context\n\n"
        "[Boss → @architect]: thoughts?"
    )
    assert out == expected


def test_weight_note_present_only_when_enabled_and_nonempty() -> None:
    out = assemble_prompt(
        delta="",
        quotes="",
        author=_boss(),
        new_text="go",
        persona_handle="arch",
    )
    assert out.startswith("[Author note — Boss]: Treat this as leadership direction.")


def test_no_weight_when_disabled() -> None:
    out = assemble_prompt(
        delta="",
        quotes="",
        author=_boss(weight_enabled=False),
        new_text="go",
        persona_handle="arch",
    )
    assert "Author note" not in out
    assert out == "[Boss → @arch]: go"


def test_no_weight_when_note_empty() -> None:
    out = assemble_prompt(
        delta="",
        quotes="",
        author=_boss(note=""),
        new_text="go",
        persona_handle="arch",
    )
    assert "Author note" not in out
    assert out == "[Boss → @arch]: go"


def test_quotes_then_delta_without_weight() -> None:
    out = assemble_prompt(
        delta="[@researcher]: prior reply",
        quotes="[Kartik]:\n  > quoted",
        author=_me(),
        new_text="continue",
        persona_handle="architect",
    )
    expected = (
        "[Kartik]:\n  > quoted\n\n[@researcher]: prior reply\n\n[Kartik → @architect]: continue"
    )
    assert out == expected
