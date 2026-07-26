"""Tier-2 evals: deterministic deliberation scenario SIMULATIONS.

Full black-box flows through the real ChatOrchestrator + SQLite + MockHarness
with *realistic scripted content*, asserting transcript SHAPE — who spoke, in
what order, that challenges precede closes, that the close synthesizes — and
the mechanical context-budget invariants (no message is ever delivered twice
to the same persona; total prompt payload for a fixed scenario stays under a
checked ceiling).

These run on every PR at zero token cost. They are the layer between the unit
matrix (tests/unit/services/test_deliberation.py) and the nightly LLM-judged
live evals (evals/): a regression that changes conversation *shape* fails
here even when every unit invariant still holds.
"""

from __future__ import annotations

import pytest

from app.domain.models import AuthorKind, HumanAuthor, Persona, Provider, ReplyMode, Room
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
from app.services.orchestrator import ChatOrchestrator

# Fixed ceiling for the fan-out scenario's total prompt payload (characters
# across every harness call). Observed today: ~2.5k over 7 calls; the ceiling
# is ~2x that, so a delta-discipline regression (which multiplies payload)
# trips it while innocent nudge-text tweaks don't. If you hit it legitimately,
# re-measure and re-derive it — don't just raise it.
FANOUT_PROMPT_CHAR_CEILING = 5_000


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
    mock = MockHarness()
    reg = BackendRegistry()
    reg.register(Provider.CLAUDE, mock)
    reg.register(Provider.CODEX, mock)
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
    return {"orch": orch, "repos": repos, "mock": mock, "me": me}


def _persona(repos, room_id, name, handle):
    p = repos["persona"].create(
        Persona(name=name, handle=handle, provider=Provider.CLAUDE, model="m", job=f"{name} job")
    )
    repos["room"].add_member(room_id, p.id)
    return p


async def _post(env, room_id, text):
    return [
        item
        async for item in env["orch"].post_message(
            room_id, author=env["me"], text=text, reply_mode=ReplyMode.SEQUENTIAL
        )
    ]


def _timeline(repos, room_id) -> list[tuple[str, str]]:
    """[(handle-or-'me', content), ...] in chronological order."""
    out = []
    for m in repos["message"].list_for_room(room_id):
        if m.author_kind is AuthorKind.PERSONA:
            out.append((repos["persona"].get(m.author_ref).handle, m.content))
        else:
            out.append(("me", m.content))
    return out


@pytest.mark.asyncio
async def test_scenario_constructive_challenge_owner_changes_course(env):
    """Owner proposes a flawed option; the critic catches it; the owner's
    close adopts the correction — and the close happens AFTER the challenge."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="db-choice", topic="pick a database"))
    _persona(repos, room.id, "Ada", "arch")
    _persona(repos, room.id, "Linus", "critic")
    env["mock"].script_replies(
        "→ @arch]",
        "Proposal: store events in flat JSON files. @critic tear this apart.",
        "Conceded — file locking under concurrent writers sinks it. "
        "Final call: SQLite with WAL; JSON export stays as a backup format.",
    )
    env["mock"].script_replies(
        "→ @critic]",
        "The flaw: concurrent writers. Two personas replying at once corrupt "
        "flat files; you'd reinvent locking. SQLite already solves this.",
    )

    await _post(env, room.id, "@arch decide our storage layer")

    timeline = _timeline(repos, room.id)
    handles = [h for h, _ in timeline]
    assert handles == ["me", "arch", "critic", "arch"]
    challenge_idx = next(i for i, (h, c) in enumerate(timeline) if "flaw" in c)
    close_idx = next(i for i, (h, c) in enumerate(timeline) if "Final call" in c)
    assert challenge_idx < close_idx  # challenge precedes the close
    assert "SQLite" in timeline[close_idx][1]  # the close ADOPTED the correction


@pytest.mark.asyncio
async def test_scenario_qualified_agreement_adds_constraint(env):
    """An advisor agrees but contributes a missed constraint — and the missed
    constraint reaches the owner's next prompt (nothing is lost in the delta)."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Ada", "arch")
    _persona(repos, room.id, "Ken", "backend")
    env["mock"].script_replies(
        "→ @arch]",
        "Plan: paginate the API by offset. @backend sanity-check.",
        "Agreed then: cursor pagination, offset only for admin tools.",
    )
    env["mock"].script_replies(
        "→ @backend]",
        "Agree with the direction, one constraint you missed: offset pagination "
        "degrades past ~10k rows; use keyset cursors for the hot path.",
    )

    await _post(env, room.id, "@arch settle the pagination question")

    owner_close_prompt = env["mock"].calls[-1].prompt
    assert "constraint you missed" in owner_close_prompt
    assert _timeline(repos, room.id)[-1][1].startswith("Agreed then: cursor")


@pytest.mark.asyncio
async def test_scenario_fanout_synthesis_incorporates_both_advisors(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Ada", "arch")
    _persona(repos, room.id, "Ken", "backend")
    _persona(repos, room.id, "Bruce", "security")
    env["mock"].script_replies(
        "→ @arch]",
        "Two angles needed. @backend: storage cost. @security: PII exposure.",
        "Close: ship it — TOKEN-COST bounded per Ken, TOKEN-PII masked per Bruce.",
    )
    env["mock"].script_replies("→ @backend]", "TOKEN-COST: ~40GB/yr at current rates.")
    env["mock"].script_replies("→ @security]", "TOKEN-PII: emails must be masked at ingest.")

    await _post(env, room.id, "@arch should we log full request bodies?")

    close = _timeline(repos, room.id)[-1][1]
    assert "TOKEN-COST" in close and "TOKEN-PII" in close
    # the closing prompt actually contained both advisor contributions
    close_prompt = env["mock"].calls[-1].prompt
    assert "40GB" in close_prompt and "masked at ingest" in close_prompt


@pytest.mark.asyncio
async def test_scenario_advisor_failure_recovery(env):
    """One advisor dies mid-deliberation; the transcript shows the loud error
    marker, the other advisor still contributes, and the owner still closes."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Ada", "arch")
    _persona(repos, room.id, "Ken", "backend")
    _persona(repos, room.id, "Bruce", "security")
    env["mock"].script_replies(
        "→ @arch]",
        "@backend and @security — both perspectives please.",
        "Closing with security's input only; backend crashed, flagging the gap.",
    )
    from app.domain.errors import HarnessTimeout

    env["mock"].script_runs("→ @backend]", HarnessTimeout("simulated 120s timeout"))
    env["mock"].script_replies("→ @security]", "No exposure risk found.")

    await _post(env, room.id, "@arch evaluate the cache change")

    timeline = _timeline(repos, room.id)
    assert any(h == "backend" and c.startswith("[error:") for h, c in timeline)
    assert any(h == "security" and "No exposure risk" in c for h, c in timeline)
    assert timeline[-1][0] == "arch"  # owner still closed


@pytest.mark.asyncio
async def test_no_message_is_ever_delivered_twice_to_the_same_persona(env):
    """Delta discipline, stated mechanically: across ALL of a persona's turns
    in a multi-round deliberation, no transcript message body is sent twice."""
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Ada", "arch")
    _persona(repos, room.id, "Ken", "backend")
    env["mock"].script_replies(
        "→ @arch]",
        "UNIQ-A1 @backend your take?",
        "UNIQ-A2 noted. @backend confirm?",
        "UNIQ-A3 done.",
    )
    env["mock"].script_replies(
        "→ @backend]",
        "UNIQ-B1 @arch pushing back.",
        "UNIQ-B2 confirmed.",
    )

    await _post(env, room.id, "@arch iterate with Ken")

    bodies = ["UNIQ-A1", "UNIQ-A2", "UNIQ-A3", "UNIQ-B1", "UNIQ-B2"]
    all_prompts_per_persona: dict[str, list[str]] = {"arch": [], "backend": []}
    for spec in env["mock"].calls:
        target = "arch" if "→ @arch]" in spec.prompt else "backend"
        all_prompts_per_persona[target].append(spec.prompt)
    for persona, prompts in all_prompts_per_persona.items():
        joined = "\n<TURN>\n".join(prompts)
        for body in bodies:
            assert joined.count(body) <= 1, (
                f"{body!r} delivered {joined.count(body)}x to {persona} — delta leak"
            )


@pytest.mark.asyncio
async def test_fanout_total_prompt_payload_stays_under_ceiling(env):
    repos = env["repos"]
    room = repos["room"].create(Room(name="R"))
    _persona(repos, room.id, "Ada", "arch")
    tags = []
    for i in range(5):
        _persona(repos, room.id, f"B{i}", f"b{i}")
        tags.append(f"@b{i}")
    env["mock"].script_replies(
        "→ @arch]",
        "Everyone, one paragraph each on the rollout risk: " + " ".join(tags),
        "Close: rollout approved with a canary stage.",
    )
    for i in range(5):
        env["mock"].script_replies(f"→ @b{i}]", f"B{i} sees moderate risk in region {i}.")

    await _post(env, room.id, "@arch assess the rollout")

    total_chars = sum(len(spec.prompt) for spec in env["mock"].calls)
    assert total_chars < FANOUT_PROMPT_CHAR_CEILING, (
        f"total prompt payload {total_chars} exceeded ceiling {FANOUT_PROMPT_CHAR_CEILING} — "
        "delta/roster discipline regressed (or re-derive the ceiling deliberately)"
    )
