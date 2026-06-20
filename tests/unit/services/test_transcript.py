import pytest

from app.domain.errors import TranscriptError
from app.domain.models import AuthorKind, HumanAuthor, Message
from app.services.transcript import (
    assemble_context,
    build_delta,
    delta_messages,
    render_quotes,
)


def _name_of(kind: AuthorKind, ref: str) -> str:
    """Test display-name resolver: maps a few known ids to display names."""
    table = {
        ("human", "kartik"): "Kartik",
        ("human", "boss"): "Boss",
        ("persona", "arch"): "@architect",
        ("persona", "res"): "@researcher",
    }
    return table[(kind.value, ref)]


def _msg(ref: str, kind: AuthorKind, content: str, mid: str) -> Message:
    return Message(id=mid, room_id="r1", author_kind=kind, author_ref=ref, content=content)


# --- build_delta ---------------------------------------------------------


def test_empty_delta_when_nothing_new() -> None:
    msgs = [_msg("kartik", AuthorKind.HUMAN, "hello", "m1")]
    out = build_delta(msgs, "m1", persona_handle="architect", name_of=_name_of)
    assert out == ""


def test_empty_delta_on_empty_list() -> None:
    out = build_delta([], None, persona_handle="architect", name_of=_name_of)
    assert out == ""


def test_all_messages_when_pointer_none() -> None:
    msgs = [
        _msg("kartik", AuthorKind.HUMAN, "first", "m1"),
        _msg("res", AuthorKind.PERSONA, "second", "m2"),
    ]
    out = build_delta(msgs, None, persona_handle="architect", name_of=_name_of)
    assert out == "[Kartik]: first\n[@researcher]: second"


def test_slice_strictly_after_pointer() -> None:
    msgs = [
        _msg("kartik", AuthorKind.HUMAN, "before", "m1"),
        _msg("kartik", AuthorKind.HUMAN, "at pointer", "m2"),
        _msg("res", AuthorKind.PERSONA, "after", "m3"),
    ]
    out = build_delta(msgs, "m2", persona_handle="architect", name_of=_name_of)
    assert out == "[@researcher]: after"


def test_directed_at_this_persona_formats_with_arrow() -> None:
    msgs = [_msg("kartik", AuthorKind.HUMAN, "hey @architect look", "m1")]
    out = build_delta(msgs, None, persona_handle="architect", name_of=_name_of)
    assert out == "[Kartik → @architect]: hey @architect look"


def test_everyone_is_directed_at_this_persona() -> None:
    msgs = [_msg("kartik", AuthorKind.HUMAN, "@everyone status?", "m1")]
    out = build_delta(msgs, None, persona_handle="architect", name_of=_name_of)
    assert out == "[Kartik → @architect]: @everyone status?"


def test_message_tagging_other_persona_not_directed() -> None:
    msgs = [_msg("kartik", AuthorKind.HUMAN, "hey @researcher", "m1")]
    out = build_delta(msgs, None, persona_handle="architect", name_of=_name_of)
    assert out == "[Kartik]: hey @researcher"


def test_personas_own_message_shown_by_name() -> None:
    msgs = [
        _msg("arch", AuthorKind.PERSONA, "I said this earlier", "m1"),
        _msg("res", AuthorKind.PERSONA, "and I replied", "m2"),
    ]
    out = build_delta(msgs, None, persona_handle="architect", name_of=_name_of)
    assert out == "[@architect]: I said this earlier\n[@researcher]: and I replied"


def test_unknown_pointer_raises_rather_than_dumping_history() -> None:
    msgs = [
        _msg("kartik", AuthorKind.HUMAN, "first", "m1"),
        _msg("res", AuthorKind.PERSONA, "second", "m2"),
    ]
    with pytest.raises(TranscriptError) as exc_info:
        build_delta(msgs, "m99", persona_handle="architect", name_of=_name_of)
    assert "m99" in str(exc_info.value)


# --- delta_messages (shared slice; single source of truth) ---------------


def test_delta_messages_none_pointer_returns_all() -> None:
    msgs = [
        _msg("kartik", AuthorKind.HUMAN, "a", "m1"),
        _msg("res", AuthorKind.PERSONA, "b", "m2"),
    ]
    assert [m.id for m in delta_messages(msgs, None)] == ["m1", "m2"]


def test_delta_messages_slices_strictly_after_pointer() -> None:
    msgs = [
        _msg("kartik", AuthorKind.HUMAN, "a", "m1"),
        _msg("kartik", AuthorKind.HUMAN, "b", "m2"),
        _msg("res", AuthorKind.PERSONA, "c", "m3"),
    ]
    assert [m.id for m in delta_messages(msgs, "m2")] == ["m3"]


def test_delta_messages_unknown_pointer_raises() -> None:
    msgs = [_msg("kartik", AuthorKind.HUMAN, "a", "m1")]
    with pytest.raises(TranscriptError) as exc_info:
        delta_messages(msgs, "m99")
    assert "m99" in str(exc_info.value)


# --- render_quotes -------------------------------------------------------


def test_render_quotes_empty_list() -> None:
    assert render_quotes([], _name_of) == ""


def test_render_quotes_single_line() -> None:
    q = _msg("res", AuthorKind.PERSONA, "LISTEN/NOTIFY won't scale", "q1")
    out = render_quotes([q], _name_of)
    assert out == "[@researcher]:\n  > LISTEN/NOTIFY won't scale"


def test_render_quotes_multiline_prefixes_each_line() -> None:
    q = _msg("kartik", AuthorKind.HUMAN, "line one\nline two", "q1")
    out = render_quotes([q], _name_of)
    assert out == "[Kartik]:\n  > line one\n  > line two"


def test_render_quotes_multiple_blocks_blank_separated() -> None:
    q1 = _msg("res", AuthorKind.PERSONA, "first quote", "q1")
    q2 = _msg("kartik", AuthorKind.HUMAN, "second quote", "q2")
    out = render_quotes([q1, q2], _name_of)
    assert out == "[@researcher]:\n  > first quote\n\n[Kartik]:\n  > second quote"


# --- assemble_context (the deep seam: slice once, dedup, assemble) -------


def test_assemble_context_composes_delta_then_directed_line() -> None:
    newer = _msg("res", AuthorKind.PERSONA, "new line", "m2")
    out = assemble_context(
        [_msg("kartik", AuthorKind.HUMAN, "old", "m1"), newer],
        "m1",  # delta = [m2]
        quoted_messages=[],
        persona_handle="architect",
        author=HumanAuthor(name="Kartik"),
        new_text="go",
        name_of=_name_of,
    )
    assert out == "[@researcher]: new line\n\n[Kartik → @architect]: go"


def test_assemble_context_renders_quote_outside_delta() -> None:
    quote = _msg("kartik", AuthorKind.HUMAN, "quoted old", "q1")
    out = assemble_context(
        [_msg("res", AuthorKind.PERSONA, "new line", "m2")],
        None,
        quoted_messages=[quote],
        persona_handle="architect",
        author=HumanAuthor(name="Kartik"),
        new_text="go",
        name_of=_name_of,
    )
    assert "  > quoted old" in out  # rendered as a blockquote
    assert "[@researcher]: new line" in out


def test_assemble_context_dedupes_quote_already_in_delta() -> None:
    # The M4-killing case: a message that is BOTH in the delta and quoted must
    # appear once (as the delta line), never also as a blockquote. Because the
    # slice is taken once and shared, dedup can't drift from the rendered delta.
    shared = _msg("res", AuthorKind.PERSONA, "shared msg", "m1")
    out = assemble_context(
        [shared],
        None,  # delta includes m1
        quoted_messages=[shared],
        persona_handle="architect",
        author=HumanAuthor(name="Kartik"),
        new_text="go",
        name_of=_name_of,
    )
    assert out.count("shared msg") == 1
    assert "  > shared msg" not in out


def test_assemble_context_prepends_enabled_weight_note() -> None:
    author = HumanAuthor(name="Boss", weight_note="be terse", weight_enabled=True)
    out = assemble_context(
        [],
        None,
        quoted_messages=[],
        persona_handle="architect",
        author=author,
        new_text="go",
        name_of=_name_of,
    )
    assert out.startswith("[Author note — Boss]: be terse")
    assert out.endswith("[Boss → @architect]: go")


def test_assemble_context_unknown_pointer_raises() -> None:
    with pytest.raises(TranscriptError):
        assemble_context(
            [_msg("kartik", AuthorKind.HUMAN, "a", "m1")],
            "m99",
            quoted_messages=[],
            persona_handle="architect",
            author=HumanAuthor(name="Kartik"),
            new_text="x",
            name_of=_name_of,
        )
