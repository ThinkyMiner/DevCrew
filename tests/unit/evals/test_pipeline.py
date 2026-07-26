"""The Tier-3 eval pipeline must itself be tested (zero tokens), or it rots:
run_live --mock produces transcripts/metrics; judge grades them via a stubbed
judge command and writes reports; parsing is robust; gating logic gates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evals.judge import Verdict, _parse_verdict, grade_run
from evals.judge import main as judge_main
from evals.run_live import main as run_live_main
from evals.scenarios import SCENARIOS, by_id


def _stub_judge_cmd(tmp_path: Path, payload: str) -> tuple[str, ...]:
    """A tiny executable that ignores stdin and prints a canned verdict."""
    script = tmp_path / "stub_judge.py"
    script.write_text(f"import sys; sys.stdin.read(); print({payload!r})")
    return (sys.executable, str(script))


def test_scenarios_are_well_formed() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    for s in SCENARIOS:
        assert s.post and s.acceptance and s.personas
        assert by_id(s.id) is s
        # mock scripts must cover every persona the mock run will invoke via
        # the post's direct targets, so --mock runs never fall to default noise
        for handle in s.mock_scripts:
            assert handle in s.personas


def test_run_live_mock_writes_transcripts_and_metrics(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rc = run_live_main(["--mock", "--out", str(out), "--scenario", "storage-tradeoff"])
    assert rc == 0
    transcript = json.loads((out / "storage-tradeoff" / "transcript.json").read_text())
    metrics = json.loads((out / "storage-tradeoff" / "metrics.json").read_text())
    assert transcript["messages"][0]["author"] == "operator"
    assert any(m["author"] == "@architect" for m in transcript["messages"])
    assert metrics["hard_invariants"]["turns_within_caps"] is True
    assert metrics["persona_turns"] >= 2


def test_judge_grades_mock_run_with_stub_and_writes_reports(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_live_main(["--mock", "--out", str(out)]) == 0
    cmd = _stub_judge_cmd(
        tmp_path,
        '{"score": 4, "rationale": "solid", "cited_messages": [1], "unknown": false}',
    )
    report = grade_run(out, cmd, trials=2)
    assert set(report["scenarios"]) == {s.id for s in SCENARIOS}
    # planted-error dimension only graded where a planted error exists
    graded = report["scenarios"]["planted-error-challenge"]["dimensions"]
    ungraded = report["scenarios"]["storage-tradeoff"]["dimensions"]
    assert "epistemic_independence" in graded
    assert "epistemic_independence" not in ungraded
    means = report["dimension_means"]
    assert means["decision_quality"] == 4.0


def test_judge_cli_writes_report_files_and_is_nonblocking_by_default(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_live_main(["--mock", "--out", str(out), "--scenario", "lane-discipline"]) == 0
    cmd = _stub_judge_cmd(tmp_path, '{"score": 2, "rationale": "meh", "unknown": false}')
    rc = judge_main([str(out), "--judge-cmd", *cmd])
    assert rc == 0  # report-only by default even for low scores
    assert (out / "report.json").exists()
    assert (out / "report.md").exists()


def test_judge_gate_fails_on_baseline_regression(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_live_main(["--mock", "--out", str(out), "--scenario", "lane-discipline"]) == 0
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"decision_quality": 4.5}))
    cmd = _stub_judge_cmd(tmp_path, '{"score": 1, "rationale": "bad", "unknown": false}')
    rc = judge_main([str(out), "--judge-cmd", *cmd, "--gate", "--baseline", str(baseline)])
    assert rc == 1


def test_verdict_parsing_is_robust_and_fails_loud() -> None:
    ok = _parse_verdict('{"score": 3, "rationale": "r", "cited_messages": [], "unknown": false}')
    assert ok.score == 3 and not ok.parse_error
    fenced = _parse_verdict('```json\n{"score": 5, "rationale": "r"}\n```')
    assert fenced.score == 5
    prosey = _parse_verdict('Here is my verdict: {"score": 2, "rationale": "r"} thanks!')
    assert prosey.score == 2
    garbage = _parse_verdict("I think it deserves a 7 out of 10")
    assert garbage.parse_error
    out_of_range = _parse_verdict('{"score": 9, "rationale": "r"}')
    assert out_of_range.parse_error
    assert Verdict().score is None
