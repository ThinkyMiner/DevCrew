"""Multi-round deliberation: personas converse across waves and converge.

Replaces the old once-per-persona-per-post delegation guard (D13) with the
D14 scheduler: a decreasing autonomous-run budget, a wave cap, causal mention
eligibility with per-target coalescing, and a reserved closing turn for the
post's owner (sole direct target) or the dispatcher.

Two layers here:
- pure unit tests for ``DeliberationScheduler`` (no I/O), and
- orchestrator integration tests on MockHarness, scripted per-turn via
  ``script_replies`` (the ``→ @<handle>]`` substring keys to one persona; each
  matching run pops the persona's next reply).
"""

from __future__ import annotations

import pytest

from app.domain.errors import HarnessError
from app.domain.events import TextDelta
from app.domain.models import HumanAuthor, Persona, Provider, ReplyMode, Room
from app.harness.mock import MockHarness
from app.harness.registry import BackendRegistry
from app.persistence.db import Database
from app.persistence.repositories import (
    AuthorRepo,
    MessageRepo,
    PersonaRepo,
    RoomRepo,
    RunRepo,
    SessionRepo,
)
from app.persistence.run_log import RunLogStore
from app.services.deliberation import (
    CloseKind,
    DeliberationScheduler,
    TerminationReason,
    TurnOutcome,
)
from app.services.orchestrator import ChatOrchestrator

# ---------------------------------------------------------------------------
# pure scheduler tests
# ---------------------------------------------------------------------------


def _ok(pid: str, *mentions: str) -> TurnOutcome:
    return TurnOutcome(persona_id=pid, ok=True, mention_ids=tuple(mentions))


def _err(pid: str) -> TurnOutcome:
    return TurnOutcome(persona_id=pid, ok=False)


def test_scheduler_quiescent_when_no_mentions() -> None:
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a"))
    assert s.next_wave() == []
    assert s.closing_turn() is None  # nothing was delegated: the reply stands
    summary = s.summary()
    assert summary.termination_reason is TerminationReason.QUIESCENT
    assert summary.close is CloseKind.NONE
    assert summary.runs_used == 0


def test_scheduler_ping_pong_bounded_by_wave_cap_then_forced_close() -> None:
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", "b"))
    assert s.next_wave() == ["b"]
    s.observe(_ok("b", "a"))
    assert s.next_wave() == ["a"]
    s.observe(_ok("a", "b"))
    assert s.next_wave() == ["b"]
    s.observe(_ok("b", "a"))
    assert s.next_wave() == []  # wave cap reached with "a" still pending
    assert s.closing_turn() == "a"
    s.observe_close(_ok("a"))
    summary = s.summary()
    assert summary.termination_reason is TerminationReason.WAVE_CAP
    assert summary.close is CloseKind.FORCED
    assert summary.runs_used == 4  # b, a, b + forced close


def test_scheduler_reserves_closing_slot_under_fanout() -> None:
    targets = tuple(f"b{i}" for i in range(8))
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", *targets))
    wave = s.next_wave()
    assert wave == list(targets[:5])  # budget 6 minus the reserved closing slot
    for pid in wave:
        s.observe(_ok(pid))
    assert s.next_wave() == []  # budget exhausted for advisors
    assert s.closing_turn() == "a"
    s.observe_close(_ok("a"))
    summary = s.summary()
    assert summary.termination_reason is TerminationReason.BUDGET
    assert summary.runs_used == 6  # 5 advisors + the reserved close


def test_scheduler_no_reserve_without_closer() -> None:
    targets = tuple(f"b{i}" for i in range(8))
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id=None, initial_targets=("a", "x"))
    s.observe(_ok("a", *targets))
    s.observe(_ok("x"))
    assert len(s.next_wave()) == 6  # full budget available
    assert s.closing_turn() is None


def test_scheduler_errors_consume_budget_and_produce_no_frontier() -> None:
    s = DeliberationScheduler(budget=3, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", "b", "c"))
    assert s.next_wave() == ["b", "c"]
    s.observe(_err("b"))
    s.observe(_err("c"))
    assert s.next_wave() == []
    assert s.closing_turn() == "a"  # advisors were attempted; owner may still synthesize
    s.observe_close(_ok("a"))
    assert s.summary().runs_used == 3


def test_scheduler_coalesces_multiple_mentions_of_one_target() -> None:
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id=None, initial_targets=("a", "b"))
    s.observe(_ok("a", "c"))
    s.observe(_ok("b", "c"))
    assert s.next_wave() == ["c"]  # one turn satisfies both requests


def test_scheduler_mention_of_unrun_initial_target_is_satisfied_by_its_turn() -> None:
    # a mentions b before b's own wave-0 turn ran: b's upcoming turn satisfies it.
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id=None, initial_targets=("a", "b"))
    s.observe(_ok("a", "b"))
    s.observe(_ok("b"))
    assert s.next_wave() == []


def test_scheduler_mention_after_target_spoke_reschedules() -> None:
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id=None, initial_targets=("a", "b"))
    s.observe(_ok("a"))
    s.observe(_ok("b", "a"))  # a already spoke; b's mention re-invites it
    assert s.next_wave() == ["a"]


def test_scheduler_natural_close_skips_forced_turn() -> None:
    # closer speaks last, successfully, with no honored mentions: that IS the close.
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", "b"))
    assert s.next_wave() == ["b"]
    s.observe(_ok("b", "a"))
    assert s.next_wave() == ["a"]
    s.observe(_ok("a"))  # no mentions — a concluded on its own
    assert s.next_wave() == []
    assert s.closing_turn() is None
    summary = s.summary()
    assert summary.close is CloseKind.NATURAL
    assert summary.termination_reason is TerminationReason.QUIESCENT


def test_scheduler_self_mentions_ignored() -> None:
    s = DeliberationScheduler(budget=6, wave_cap=3, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", "a"))
    assert s.next_wave() == []


def test_scheduler_budget_never_negative_and_wave_counts() -> None:
    s = DeliberationScheduler(budget=2, wave_cap=5, closer_id="a", initial_targets=("a",))
    s.observe(_ok("a", "b", "c", "d"))
    assert s.next_wave() == ["b"]  # budget 2 minus reserve 1
    s.observe(_ok("b", "c"))
    assert s.next_wave() == []
    assert s.closing_turn() == "a"
    s.observe_close(_ok("a"))
    summary = s.summary()
    assert summary.runs_used == 2
    assert summary.waves_used == 1
    assert summary.termination_reason is TerminationReason.BUDGET


# ---------------------------------------------------------------------------
# orchestrator integration (MockHarness, scripted per-turn)
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    repos = {
        "persona": PersonaRepo(db),
        "author": AuthorRepo(db),
        "room": RoomRepo(db),
        "message": MessageRepo(db),
        "session": SessionRepo(db),
        "run": RunRepo(db),
    }
    mock_a = MockHarness()  # claude personas
    mock_b = MockHarness()  # codex personas
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, mock_a)
    reg.register(Provider.CODEX, mock_b)
    orch = ChatOrchestrator(
        persona_repo=repos["persona"],
        author_repo=repos["author"],
        room_repo=repos["room"],
        message_repo=repos["message"],
        session_repo=repos["session"],
        run_repo=repos["run"],
        run_log=RunLogStore(tmp_path / "runs"),
        registry=reg,
    )
    me = repos["author"].create(HumanAuthor(name="Me", weight_enabled=False))
    return {"orch": orch, "repos": repos, "mock_a": mock_a, "mock_b": mock_b, "me": me}


def _persona(repos, room_id, name, handle, provider, *, member=True):
    p = repos["persona"].create(
        Persona(name=name, handle=handle, provider=provider, model="m", job=f"{name} job")
    )
    if member:
        repos["room"].add_member(room_id, p.id)
    return p


def _replies_by_handle(repos, room_id):
    from app.domain.models import AuthorKind

    out: list[tuple[str, str]] = []
    for m in repos["message"].list_for_room(room_id):
        if m.author_kind is AuthorKind.PERSONA:
            out.append((repos["persona"].get(m.author_ref).handle, m.content))
    return out


async def _post(env, room_id, text):
    return [
        item
        async for item in env["orch"].post_message(
            room_id, author=env["me"], text=text, reply_mode=ReplyMode.SEQUENTIAL
        )
    ]


@pytest.mark.asyncio
async def test_back_and_forth_conversation_with_natural_close(env):
    """A asks B, B asks back, A answers and concludes — a real conversation,
    ended by quiescence + natural close (no forced turn, no marker protocol)."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies(
        "→ @arch]",
        "@back sketch the API for this.",
        "Good point — JWT it is. Final call: ship the sketch with JWT auth.",
    )
    env["mock_b"].script_replies("→ @back]", "Sketched. @arch what about auth?")

    await _post(env, room.id, "@arch design the endpoint")

    assert len(env["mock_a"].calls) == 2  # initial turn + return turn (NOT a third)
    assert len(env["mock_b"].calls) == 1
    # arch's second turn saw back's question via the delta
    assert "what about auth?" in env["mock_a"].calls[1].prompt
    handles = [h for h, _ in _replies_by_handle(repos, room.id)]
    assert handles == ["arch", "back", "arch"]


@pytest.mark.asyncio
async def test_own_past_reply_filtered_from_delta(env):
    """A resumed session already remembers its own replies — they are not
    re-injected (context bloat), but everyone else's messages are."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies(
        "→ @arch]", "MARKER-OWN-REPLY @back check this", "understood, closing."
    )
    env["mock_b"].script_replies("→ @back]", "checked. @arch back to you")

    await _post(env, room.id, "@arch go")

    second_prompt = env["mock_a"].calls[1].prompt
    assert "MARKER-OWN-REPLY" not in second_prompt  # own reply not re-sent
    assert "checked." in second_prompt  # teammate's reply is


@pytest.mark.asyncio
async def test_ping_pong_stops_at_wave_cap_then_forced_close(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies(
        "→ @arch]",
        "@back round one",  # wave 0 (operator turn)
        "@back round two",  # wave 2
        "WRAP-UP: we ship option A.",  # forced close
    )
    env["mock_b"].script_replies(
        "→ @back]",
        "@arch challenge one",  # wave 1
        "@arch challenge two",  # wave 3
    )

    await _post(env, room.id, "@arch go")

    assert len(env["mock_a"].calls) == 3
    assert len(env["mock_b"].calls) == 2
    # the forced close is the last message and its prompt forbids further tags
    handles_contents = _replies_by_handle(repos, room.id)
    assert handles_contents[-1] == ("arch", "WRAP-UP: we ship option A.")
    assert "closing turn" in env["mock_a"].calls[-1].prompt


@pytest.mark.asyncio
async def test_closing_reply_mentions_are_not_honored(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies(
        "→ @arch]",
        "@back one look please",
        "closing — but @back encore!",  # mention in a closing reply: ignored
    )
    env["mock_b"].script_replies("→ @back]", "looked; nothing to add.")

    await _post(env, room.id, "@arch go")

    assert len(env["mock_b"].calls) == 1  # no encore


@pytest.mark.asyncio
async def test_fanout_respects_budget_with_reserved_close(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    tags = []
    for i in range(8):
        h = f"b{i}"
        _persona(repos, room.id, f"B{i}", h, Provider.CODEX)
        tags.append(f"@{h}")
    env["mock_a"].script_replies("→ @arch]", "everyone weigh in: " + " ".join(tags), "WRAP")

    await _post(env, room.id, "@arch go")

    advisor_replies = [h for h, _ in _replies_by_handle(repos, room.id) if h != "arch"]
    assert len(advisor_replies) == 5  # budget 6 minus reserved closing slot
    assert [h for h, c in _replies_by_handle(repos, room.id) if c == "WRAP"] == ["arch"]


@pytest.mark.asyncio
async def test_advisor_error_consumes_budget_and_owner_still_closes(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies("→ @arch]", "@back your take?", "closing without input.")
    env["mock_b"].script_runs("→ @back]", HarnessError("simulated crash"))

    await _post(env, room.id, "@arch go")

    handles_contents = _replies_by_handle(repos, room.id)
    assert any(h == "back" and c.startswith("[error:") for h, c in handles_contents)
    assert handles_contents[-1] == ("arch", "closing without input.")


@pytest.mark.asyncio
async def test_erroring_persona_does_not_delegate_from_stale_older_reply(env):
    """Regression (pre-existing bug): after a delegated turn ERRORS, delegation
    must not continue from that persona's OLDER successful reply."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    _persona(repos, room.id, "Critic", "crit", Provider.CLAUDE)
    env["mock_a"].script_replies(
        "→ @arch]",
        "@back do X",  # post 1
        "post-1 close",  # post 1 forced close
        "@back do Y",  # post 2
        "post-2 close",  # post 2 forced close (after back's error)
    )
    env["mock_b"].script_runs(
        "→ @back]",
        [TextDelta(text="ok — @crit please review")],  # post 1: success mentioning @crit
        HarnessError("boom"),  # post 2: this turn fails
    )
    env["mock_a"].script_replies("→ @crit]", "reviewed, fine.")

    await _post(env, room.id, "@arch kick off X")
    crit_runs_after_post1 = sum("→ @crit]" in c.prompt for c in env["mock_a"].calls)

    await _post(env, room.id, "@arch kick off Y")
    crit_runs_after_post2 = sum("→ @crit]" in c.prompt for c in env["mock_a"].calls)

    assert crit_runs_after_post1 == 1
    assert crit_runs_after_post2 == 1  # stale "@crit" from post 1 must NOT re-fire


@pytest.mark.asyncio
async def test_multi_target_post_coalesces_and_has_no_forced_close(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    _persona(repos, room.id, "Critic", "crit", Provider.CLAUDE)
    env["mock_a"].script_replies("→ @arch]", "needs @crit eyes")
    env["mock_b"].script_replies("→ @back]", "agreed, @crit should look")
    env["mock_a"].script_replies("→ @crit]", "looked: fine by me.")

    await _post(env, room.id, "@arch @back review this")

    handles = [h for h, _ in _replies_by_handle(repos, room.id)]
    assert handles.count("crit") == 1  # coalesced
    assert handles.count("arch") == 1  # multi-target: no owner, no forced close
    assert handles.count("back") == 1


@pytest.mark.asyncio
async def test_mention_of_unrun_initial_target_not_double_scheduled(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies("→ @arch]", "@back will cover this next")
    env["mock_b"].script_replies("→ @back]", "covered.")

    await _post(env, room.id, "@arch @back both of you")

    assert len(env["mock_b"].calls) == 1  # wave-0 turn satisfied arch's mention


@pytest.mark.asyncio
async def test_everyone_self_and_unknown_do_not_schedule(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies("→ @arch]", "@everyone @arch @ghost — thinking aloud")

    await _post(env, room.id, "@arch go")

    assert len(env["mock_a"].calls) == 1
    assert env["mock_b"].calls == []


@pytest.mark.asyncio
async def test_dispatcher_sole_target_gets_wrapup_close(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "systemd", "systemd", Provider.CLAUDE)
    _persona(repos, room.id, "B0", "b0", Provider.CODEX)
    _persona(repos, room.id, "B1", "b1", Provider.CODEX)
    env["mock_a"].script_replies(
        "→ @systemd]",
        "plan: @b0 do the API, @b1 do the schema",
        "WRAP: recommendation is X; b1 dissents on Y; risk Z remains.",
    )
    env["mock_b"].script_replies("→ @b0]", "API done.")
    env["mock_b"].script_replies("→ @b1]", "schema done.")

    await _post(env, room.id, "@systemd ship the feature")

    handles_contents = _replies_by_handle(repos, room.id)
    assert handles_contents[-1][0] == "systemd"
    assert handles_contents[-1][1].startswith("WRAP")
    closing_prompt = env["mock_a"].calls[-1].prompt
    assert "closing turn" in closing_prompt
    assert "Wrap up" in closing_prompt  # dispatcher variant, not owner-synthesis


@pytest.mark.asyncio
async def test_advisor_turn_prompt_carries_wave_context(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Architect", "arch", Provider.CLAUDE)
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_a"].script_replies("→ @arch]", "@back thoughts?", "done then.")
    env["mock_b"].script_replies("→ @back]", "no further thoughts.")

    await _post(env, room.id, "@arch go")

    advisor_prompt = env["mock_b"].calls[0].prompt
    assert "wave 1" in advisor_prompt
    assert "team turns left" in advisor_prompt


@pytest.mark.asyncio
async def test_own_error_marker_stays_in_delta(env):
    """Fail-loud exception to own-message filtering: a persona whose turn died
    must still SEE its own [error:] marker next turn (the harness session may
    not contain the failed turn at all)."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Backend", "back", Provider.CODEX)
    env["mock_b"].script_runs(
        "→ @back]",
        HarnessError("first turn crashed"),
        [TextDelta(text="recovered.")],
    )

    await _post(env, room.id, "@back try this")
    await _post(env, room.id, "@back try again")

    second_prompt = env["mock_b"].calls[1].prompt
    assert "[error:" in second_prompt
