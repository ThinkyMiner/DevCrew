from app.domain.models import AuthorKind, Message
from app.services.transcript import build_delta, render_quotes


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
