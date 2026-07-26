"""Generate fresh deliberation transcripts for the eval scenarios.

Each scenario runs against a THROWAWAY database seeded with the canonical
personas, through the real ``ChatOrchestrator`` — with the real CLIs by
default, or ``--mock`` (MockHarness + each scenario's ``mock_scripts``) for a
zero-token pipeline-health run. Outputs, per scenario, under
``evals/out/<stamp>/<scenario-id>/``:

- ``transcript.json`` — ordered messages with author handles/kinds
- ``metrics.json`` — mechanical metrics + hard-invariant verdicts

Hard invariants are checked HERE (not by the LLM judge): turn counts within
the D14 caps and no silent truncation. A violation is a run failure (exit 1)
regardless of any quality score — safety is never a rubric item.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.domain.models import (
    AuthorKind,
    HumanAuthor,
    Message,
    Provider,
    ReplyMode,
    Room,
    RunRecord,
)
from app.harness.claude import ClaudeHarness
from app.harness.codex import CodexHarness
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
from app.services.orchestrator import AUTONOMOUS_RUN_CAP, ChatOrchestrator
from app.services.persona_seed import ensure_default_personas
from evals.scenarios import SCENARIOS, Scenario

# Max persona messages a single post may produce: the initial targeted turns
# (bounded by targets mentioned in the post) + the autonomous budget. Eval
# posts tag at most 2 personas directly.
_MAX_DIRECT_TARGETS = 2
TURN_CEILING = _MAX_DIRECT_TARGETS + AUTONOMOUS_RUN_CAP


def _neutral_scratch_dir() -> Path:
    """A neutral persona cwd under the SYSTEM temp dir — never inside the repo,
    or the CLIs' walk-up discovery finds this project's CLAUDE.md/AGENTS.md and
    the eval measures a "Team coding agent" instead of the persona (same rule
    as ``Settings.resolved_scratch_dir``)."""
    return Path(tempfile.gettempdir()) / "team-evals-persona-scratch"


def _build_registry(mock: bool, scenario: Scenario, scratch_dir: Path) -> BackendRegistry:
    registry = BackendRegistry()
    if mock:
        harness = MockHarness()
        for handle, replies in scenario.mock_scripts.items():
            harness.script_replies(f"→ @{handle}]", *replies)
        registry.register(Provider.CLAUDE, harness)
        registry.register(Provider.CODEX, harness)
    else:
        # Same persona-environment isolation as the app (settings.resolved_
        # scratch_dir): a neutral empty cwd OUTSIDE the repo, or the CLIs walk
        # up, find this project's CLAUDE.md/AGENTS.md, and the eval measures a
        # "Team coding agent" instead of the persona.
        scratch_dir.mkdir(parents=True, exist_ok=True)
        registry.register(Provider.CLAUDE, ClaudeHarness(scratch_dir=str(scratch_dir)))
        registry.register(Provider.CODEX, CodexHarness(scratch_dir=str(scratch_dir)))
    return registry


async def _run_scenario(scenario: Scenario, out_dir: Path, mock: bool) -> dict[str, object]:
    scenario_dir = out_dir / scenario.id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    db = Database(scenario_dir / "eval.db")
    db.init_schema()
    personas = PersonaRepo(db)
    ensure_default_personas(personas)
    rooms = RoomRepo(db)
    messages = MessageRepo(db)
    runs = RunRepo(db)

    room = rooms.create(Room(name=f"eval:{scenario.id}", topic=scenario.title))
    by_handle = {p.handle: p for p in personas.list()}
    for handle in scenario.personas:
        rooms.add_member(room.id, by_handle[handle].id)

    orchestrator = ChatOrchestrator(
        persona_repo=personas,
        author_repo=AuthorRepo(db),
        room_repo=rooms,
        message_repo=messages,
        session_repo=SessionRepo(db),
        run_repo=runs,
        run_log=RunLogStore(scenario_dir / "runs"),
        registry=_build_registry(mock, scenario, _neutral_scratch_dir()),
    )
    operator = AuthorRepo(db).create(HumanAuthor(name="Operator", weight_enabled=False))

    async for _ in orchestrator.post_message(
        room.id, author=operator, text=scenario.post, reply_mode=ReplyMode.SEQUENTIAL
    ):
        pass

    transcript = _transcript_rows(messages.list_for_room(room.id), personas)
    metrics = _metrics(transcript, runs.list_for_room(room.id))
    (scenario_dir / "transcript.json").write_text(
        json.dumps({"scenario": scenario.model_dump(), "messages": transcript}, indent=2)
    )
    (scenario_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    db.close()
    return metrics


def _transcript_rows(rows: list[Message], personas: PersonaRepo) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i, m in enumerate(rows):
        if m.author_kind is AuthorKind.PERSONA:
            persona = personas.get(m.author_ref)
            author = f"@{persona.handle}" if persona else m.author_ref
        else:
            author = "operator"
        out.append(
            {
                "index": i,
                "author": author,
                "kind": m.author_kind.value,
                "content": m.content,
                "is_error": m.is_error_marker(),
            }
        )
    return out


def _token_count(usage: dict[str, object], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _metrics(
    transcript: list[dict[str, object]], run_records: list[RunRecord]
) -> dict[str, object]:
    persona_turns = [r for r in transcript if r["kind"] == "persona"]
    error_turns = [r for r in persona_turns if r["is_error"]]
    usage_in = usage_out = 0
    for record in run_records:
        usage = record.usage or {}
        usage_in += _token_count(usage, "input_tokens")
        usage_out += _token_count(usage, "output_tokens")
    within_caps = len(persona_turns) <= TURN_CEILING
    return {
        "persona_turns": len(persona_turns),
        "error_turns": len(error_turns),
        "distinct_speakers": len({r["author"] for r in persona_turns}),
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "turn_ceiling": TURN_CEILING,
        "hard_invariants": {"turns_within_caps": within_caps},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="MockHarness pipeline-health run")
    parser.add_argument("--scenario", help="run a single scenario id")
    parser.add_argument(
        "--out", type=Path, default=None, help="output dir (default evals/out/<ts>)"
    )
    args = parser.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or Path("evals/out") / (stamp + ("-mock" if args.mock else ""))
    chosen = [s for s in SCENARIOS if args.scenario in (None, s.id)]
    if not chosen:
        raise SystemExit(f"no scenario matches {args.scenario!r}")

    failures: list[str] = []
    for scenario in chosen:
        metrics = asyncio.run(_run_scenario(scenario, out_dir, args.mock))
        invariants = metrics["hard_invariants"]
        ok = isinstance(invariants, dict) and all(invariants.values())
        status = "ok" if ok else "HARD-INVARIANT VIOLATION"
        print(f"[eval] {scenario.id}: {metrics['persona_turns']} turns, {status}")
        if not ok:
            failures.append(scenario.id)

    print(f"[eval] transcripts written to {out_dir}")
    if failures:
        print(f"[eval] FAILED hard invariants: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
