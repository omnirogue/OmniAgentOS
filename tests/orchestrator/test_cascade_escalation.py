"""Verification-gated tier escalation (OMNIAGENTOS_CASCADE=1) in ``_execute_task``.

Flag OFF: the retry loop is byte-identical to the pre-cascade behavior -- the tier
never moves and a ``status == "error"`` result fails immediately with no retry, and no
trace is written. Flag ON: a reviewer DENY or an objectively-errored attempt escalates
one rung (CHEAP -> FUSION -> FUSION_ULTRA, capped) and retries, and every attempt
records a win/loss JSONL trace the learner can mine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator.contracts import (
    ExecutorRequest,
    ExecutorResult,
    ExecutorTier,
    ReviewVerdict,
)

_SIMPLE_GOAL = "Add a docstring to the greet function"


def _plan_llm(complexity: str = "simple") -> Any:
    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        return {
            "project_name": "Greeter",
            "description": "Improve the greeter",
            "complexity": complexity,
            "tasks": [
                {"title": "Task one", "description": "do it", "acceptance_criteria": ["works"]}
            ],
        }

    return _llm


@dataclass
class _SeqRunner:
    """Returns ``results[i]`` for the i-th spawn; records every request."""

    results: list[ExecutorResult]
    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[idx]

    @property
    def tiers(self) -> list[ExecutorTier]:
        return [c.tier for c in self.calls]


@dataclass
class _SeqReviewer:
    verdicts: list[str]
    feedback: str = "address criterion 'works'"
    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return ReviewVerdict(verdict=verdict, feedback=self.feedback, reviewer="mock")


def _orch(
    runner: _SeqRunner, reviewer: _SeqReviewer, tmp_path: Path, *, trace: Path | None = None
) -> Orchestrator:
    # Isolate the trace path per-test even when the caller doesn't pin one: the learner
    # (wired into ``_execute_task`` under cascade) reads the trace to pick a start tier,
    # so a shared default trace file would make these tests non-deterministic.
    resolved_trace = trace if trace is not None else tmp_path / "cascade-traces.jsonl"
    return Orchestrator(
        planner_llm=_plan_llm(),
        reviewer=reviewer,
        executor_runner=runner,
        vault_dir=str(tmp_path),
        cascade_trace_path=str(resolved_trace),
    )


def _ok(text: str = "did the work") -> ExecutorResult:
    return ExecutorResult(status="ok", output_text=text, session_id="ses")


def _err(msg: str = "test suite failed: 1 failed") -> ExecutorResult:
    return ExecutorResult(
        status="error", error=msg, output_text="pytest output\nFAILED", session_id="ses"
    )


# --- flag OFF: unchanged behavior -----------------------------------------


def test_flag_off_tier_never_changes_on_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CASCADE", raising=False)
    runner = _SeqRunner(results=[_ok(), _ok()])
    reviewer = _SeqReviewer(verdicts=["deny", "confirm"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    # Corrective retry happens (balanced caps at 1) but the tier does NOT escalate.
    assert runner.tiers == [ExecutorTier.CHEAP, ExecutorTier.CHEAP]
    assert result.tasks[0].tier is ExecutorTier.CHEAP
    assert result.tasks[0].status == "done"


def test_flag_off_error_fails_immediately_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CASCADE", raising=False)
    trace = tmp_path / "traces.jsonl"
    runner = _SeqRunner(results=[_err(), _ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    result = _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert len(runner.calls) == 1  # no retry on error
    assert result.tasks[0].status == "failed"
    assert result.tasks[0].tier is ExecutorTier.CHEAP
    assert not trace.exists()  # no trace written with the flag off


# --- flag ON: escalation ---------------------------------------------------


def test_deny_then_confirm_escalates_cheap_to_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _SeqRunner(results=[_ok(), _ok()])
    reviewer = _SeqReviewer(verdicts=["deny", "confirm"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert runner.tiers == [ExecutorTier.CHEAP, ExecutorTier.FUSION]
    assert result.tasks[0].tier is ExecutorTier.FUSION
    assert result.tasks[0].status == "done"
    assert result.tasks[0].attempts == 2


def test_error_then_success_escalates_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _SeqRunner(results=[_err(), _ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    # The objectively-errored cheap attempt escalates to FUSION and retries.
    assert runner.tiers == [ExecutorTier.CHEAP, ExecutorTier.FUSION]
    assert result.tasks[0].status == "done"
    assert result.tasks[0].tier is ExecutorTier.FUSION


def test_fusion_ultra_is_the_escalation_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _SeqRunner(results=[_ok(), _ok(), _ok()])
    reviewer = _SeqReviewer(verdicts=["deny", "deny", "deny"])
    # quality starts at FUSION_ULTRA with max_retries=2.
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="quality", working_dir=str(tmp_path)
    )
    assert runner.tiers == [
        ExecutorTier.FUSION_ULTRA,
        ExecutorTier.FUSION_ULTRA,
        ExecutorTier.FUSION_ULTRA,
    ]
    assert result.tasks[0].status == "denied"
    assert result.tasks[0].tier is ExecutorTier.FUSION_ULTRA


def test_traces_record_win_and_loss_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "sub" / "traces.jsonl"
    runner = _SeqRunner(results=[_ok(), _ok()])
    reviewer = _SeqReviewer(verdicts=["deny", "confirm"])
    _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    rows = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    # Attempt 1: cheap tier, verified failure (deny), reached with no escalation.
    assert rows[0]["tier_name"] == "cheap"
    assert rows[0]["verified"] is False
    assert rows[0]["error"] is False
    assert rows[0]["escalated_from"] is None
    assert rows[0]["task_class"] == "orch:superfast:simple"
    # Attempt 2: fusion tier, verified WIN, escalated from cheap.
    assert rows[1]["tier_name"] == "fusion"
    assert rows[1]["verified"] is True
    assert rows[1]["escalated_from"] == "cheap"


def test_error_attempt_records_error_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "traces.jsonl"
    runner = _SeqRunner(results=[_err(), _ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    rows = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["error"] is True and rows[0]["verified"] is False
    assert rows[1]["verified"] is True and rows[1]["error"] is False


# --- FIX 4a: the learner mines traces to pick the START tier -----------------


def _seed_trace(path: Path, task_class: str, tier_name: str, *, verified: bool, count: int) -> None:
    """Append ``count`` synthetic trace rows for one tier of one class."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for _ in range(count):
            handle.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "task_class": task_class,
                        "tier_name": tier_name,
                        "adapter": tier_name,
                        "model": None,
                        "verified": verified,
                        "seconds": 0.1,
                        "error": False,
                        "escalated_from": None,
                    }
                )
                + "\n"
            )


def test_learner_starts_at_fusion_when_cheap_reliably_loses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "seeded.jsonl"
    # Legacy lane history (orch:superfast): the cheap tier loses 9/9 while the fusion
    # tier wins 8/8 -> the learner should skip straight to FUSION (min_samples cleared).
    _seed_trace(trace, "orch:superfast", "cheap", verified=False, count=9)
    _seed_trace(trace, "orch:superfast", "fusion", verified=True, count=8)

    runner = _SeqRunner(results=[_ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    result = _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    # The FIRST (and only) attempt starts at FUSION, not the resolved CHEAP tier.
    assert runner.tiers[0] is ExecutorTier.FUSION
    assert result.tasks[0].tier is ExecutorTier.FUSION
    assert result.tasks[0].status == "done"


def test_learner_stays_at_resolved_tier_below_min_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "seeded.jsonl"
    # Only 3 observations (< min_samples=5): not enough evidence to move off cheap-first.
    _seed_trace(trace, "orch:superfast", "cheap", verified=False, count=3)

    runner = _SeqRunner(results=[_ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert runner.tiers[0] is ExecutorTier.CHEAP  # resolved tier, unchanged


def test_route_learning_min_samples_env_is_plumbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    monkeypatch.setenv("OMNIAGENTOS_ROUTE_LEARN_MIN_SAMPLES", "3")
    observed: list[int] = []

    def _recommend(*args: Any, min_samples: int, **kwargs: Any) -> int:
        observed.append(min_samples)
        return 0

    monkeypatch.setattr("omniagentos.routing.learn.recommend_start_tier", _recommend)
    runner = _SeqRunner(results=[_ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert observed == [3]


def test_learner_falls_back_from_fine_class_to_lane_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "seeded.jsonl"
    _seed_trace(trace, "orch:superfast", "cheap", verified=False, count=20)
    _seed_trace(trace, "orch:superfast", "fusion", verified=True, count=30)

    runner = _SeqRunner(results=[_ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert runner.tiers[0] is ExecutorTier.FUSION


def test_learner_falls_back_from_lane_to_global_orch_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    trace = tmp_path / "seeded.jsonl"
    _seed_trace(trace, "orch:fusion:complex", "cheap", verified=False, count=20)
    _seed_trace(trace, "orch:fusion:complex", "fusion", verified=True, count=30)

    runner = _SeqRunner(results=[_ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    _orch(runner, reviewer, tmp_path, trace=trace).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )
    assert runner.tiers[0] is ExecutorTier.FUSION


# --- FIX 6: an escalated request drops the operator's stale lane/model pins ---


def test_escalated_request_drops_lane_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _SeqRunner(results=[_err(), _ok()])
    reviewer = _SeqReviewer(verdicts=["confirm"])
    # Pin an executor model on the resolved (cheap) tier; the escalated FUSION attempt
    # must NOT carry it (nor the superfast lane) -- the fusion tier's defaults take over.
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        pins={"executor_model_or_lineage": "pinned-model"},
    )
    assert runner.tiers == [ExecutorTier.CHEAP, ExecutorTier.FUSION]
    # First attempt (resolved tier): pins preserved exactly.
    assert runner.calls[0].model == "pinned-model"
    assert runner.calls[0].lane == "superfast"
    # Escalated attempt: no stale lane/model pin.
    assert runner.calls[1].model is None
    assert runner.calls[1].lane is None
    assert result.tasks[0].status == "done"
