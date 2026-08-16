"""H2 — the orchestrator's quality gate must FAIL CLOSED.

``CrossLineageReviewer`` is the default reviewer for EVERY orchestration
(``orchestrator/core.py`` constructs it when no reviewer is injected). Three sites
used to return ``verdict="confirm"`` with "passed by default":

  * the adapter raised            (review.py, ``review()``)
  * the payload was unparseable   (review.py, ``_parse_verdict``)
  * the verdict string was junk   (review.py, ``_parse_verdict``)

so a logged-out or quota-dead reviewer CLI silently approved everything it was
asked to judge. Commit ``1e8a93`` closed the SWARM seat and left this one open.

Each of the three modes is forced below, and the loop-level contract is pinned:
the reviewer is retried EXACTLY once, the task lands ``blocked_on_review``, the
run never reports success, and the executor is NOT re-spawned — a reviewer
infrastructure failure is not the executor's fault and must not consume its
retry budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.intake.planner import PlannedTask
from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator.contracts import ExecutorRequest, ExecutorResult
from omniagentos.orchestrator.review import CrossLineageReviewer, _parse_verdict

# --- the three infrastructure failure modes, at the reviewer -----------------


class _RaisingAdapter:
    """Mode 1 — the adapter itself is down (logged out, missing binary, timeout)."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, input: Any) -> Any:
        self.calls += 1
        raise RuntimeError("Not signed in. Run `grok login`.")


class _UnparseableAdapter:
    """Mode 2 — the reviewer answered, but not with a verdict envelope."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, input: Any) -> Any:
        self.calls += 1

        class _Out:
            output_json = None
            output_text = "Sure! I'd be happy to review that for you."

        return _Out()


class _JunkVerdictAdapter:
    """Mode 3 — a verdict field that is not one of the two legal values."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, input: Any) -> Any:
        self.calls += 1

        class _Out:
            output_json = {"verdict": "maybe", "feedback": "shrug"}
            output_text = ""

        return _Out()


def _review_with(adapter: Any) -> Any:
    reviewer = CrossLineageReviewer(adapter=adapter)
    return reviewer.review(
        task=PlannedTask(title="h2 task", description="x", commits_expected=False),
        spec_markdown="# spec\n",
        result=ExecutorResult(status="ok", output_text="worker produced output"),
    )


@pytest.mark.parametrize(
    ("adapter_cls", "expected_feedback"),
    [
        (_RaisingAdapter, "reviewer adapter failed"),
        (_UnparseableAdapter, "no parseable verdict"),
        (_JunkVerdictAdapter, "unrecognised verdict"),
    ],
)
def test_each_infrastructure_failure_mode_is_error_never_confirm(
    adapter_cls: type, expected_feedback: str
) -> None:
    verdict = _review_with(adapter_cls())
    assert verdict.verdict == "error", "reviewer infrastructure failure must never CONFIRM"
    assert verdict.verdict != "deny", "infra failure is not a judgement on the executor"
    assert expected_feedback in verdict.feedback


def test_parse_verdict_never_coerces_an_unknown_string_to_confirm() -> None:
    class _Out:
        output_json = {"verdict": "APPROVED-ish", "feedback": "looks fine"}
        output_text = ""

    assert _parse_verdict(_Out()).verdict == "error"


def test_legitimate_verdicts_are_untouched() -> None:
    """A reviewer that actually answers must still confirm/deny normally."""

    class _Confirm:
        output_json = {"verdict": "confirm", "feedback": "meets criteria"}
        output_text = ""

    class _Deny:
        output_json = None
        output_text = '{"verdict": "deny", "feedback": "criterion 2 unmet"}'

    assert _parse_verdict(_Confirm()).verdict == "confirm"
    denied = _parse_verdict(_Deny())
    assert denied.verdict == "deny"
    assert denied.feedback == "criterion 2 unmet"


# --- the loop-level contract -------------------------------------------------


@dataclass
class _CountingRunner:
    """Records every executor spawn so a consumed retry is visible."""

    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        return ExecutorResult(
            status="ok",
            output_text="executor did the work",
            session_id=f"ses{len(self.calls)}",
        )


@dataclass
class _ErroringReviewer:
    """A reviewer whose infrastructure is down for every call."""

    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> Any:
        self.calls += 1
        from omniagentos.orchestrator.contracts import ReviewVerdict

        return ReviewVerdict(
            verdict="error", feedback="reviewer adapter failed: RuntimeError: down", reviewer="x"
        )


@dataclass
class _FlakyReviewer:
    """Down once, then healthy — the transient blip the single retry exists for."""

    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> Any:
        from omniagentos.orchestrator.contracts import ReviewVerdict

        self.calls += 1
        if self.calls == 1:
            return ReviewVerdict(verdict="error", feedback="transient", reviewer="x")
        return ReviewVerdict(verdict="confirm", feedback="ok", reviewer="x")


_PLAN = {
    "project_name": "H2",
    "description": "d",
    "complexity": "simple",
    "tasks": [{"title": "Task one", "description": "do it", "acceptance_criteria": ["works"]}],
}


def _orchestrator(reviewer: Any, runner: _CountingRunner, vault_dir: Path) -> Orchestrator:
    return Orchestrator(
        planner_llm=lambda *_args, **_kw: dict(_PLAN),
        reviewer=reviewer,
        executor_runner=runner,
        vault_dir=str(vault_dir),
    )


def test_reviewer_infra_failure_blocks_and_retries_exactly_once(tmp_path: Path) -> None:
    reviewer = _ErroringReviewer()
    runner = _CountingRunner()
    result = _orchestrator(reviewer, runner, tmp_path).run(
        "Add a docstring to the greet function",
        priority="balanced",
        working_dir=str(tmp_path),
    )

    outcome = result.tasks[0]
    assert outcome.status == "blocked_on_review", "a dead reviewer must block, never confirm"
    assert outcome.status != "done"
    assert outcome.review is not None
    assert outcome.review.verdict == "error"

    # EXACTLY one retry of the reviewer: two calls total, not one and not three.
    assert reviewer.calls == 2

    # And the executor was spawned ONCE. Re-spawning would charge the task's retry
    # budget for the reviewer being down — the failure was never the executor's.
    assert len(runner.calls) == 1
    assert outcome.attempts == 1

    # The run must not aggregate a blocked task as success.
    assert result.status != "done"


def test_a_transient_reviewer_failure_recovers_on_the_single_retry(tmp_path: Path) -> None:
    reviewer = _FlakyReviewer()
    runner = _CountingRunner()
    result = _orchestrator(reviewer, runner, tmp_path).run(
        "Add a docstring to the greet function",
        priority="balanced",
        working_dir=str(tmp_path),
    )

    assert result.tasks[0].status == "done"
    assert reviewer.calls == 2
    assert len(runner.calls) == 1  # still no executor retry consumed


def test_blocked_on_review_is_not_reported_as_a_deny(tmp_path: Path) -> None:
    """A DENY re-spawns a corrective executor; a blocked review must not.

    Conflating the two is what would let a reviewer outage burn a task's whole
    retry ladder and then report ``denied`` — a verdict nothing ever rendered.
    """
    reviewer = _ErroringReviewer()
    runner = _CountingRunner()
    result = _orchestrator(reviewer, runner, tmp_path).run(
        "Add a docstring to the greet function",
        priority="balanced",
        working_dir=str(tmp_path),
    )
    assert result.tasks[0].status != "denied"
    assert len(runner.calls) == 1
