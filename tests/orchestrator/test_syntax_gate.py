from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator.contracts import (
    ExecutorRequest,
    ExecutorResult,
    ReviewVerdict,
)
from omniagentos.verify import verify_working_dir

_SIMPLE_GOAL = "Fix the syntax"


def _plan_llm(complexity: str = "simple") -> Any:
    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        return {
            "project_name": "Greeter",
            "description": "Fix the python file",
            "complexity": complexity,
            "tasks": [
                {"title": "Task one", "description": "do it", "acceptance_criteria": ["works"]}
            ],
        }

    return _llm


@dataclass
class _SeqRunner:
    results: list[ExecutorResult]
    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        res = self.results[idx]
        if res.working_dir is None:
            res.working_dir = request.working_dir
        return res


@dataclass
class _SpyReviewer:
    verdicts: list[str]
    calls: int = 0
    feedback: str = "review feedback"

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        if verdict == "assert_not_called":
            raise AssertionError("Reviewer should not have been called!")
        return ReviewVerdict(verdict=verdict, feedback=self.feedback, reviewer="mock")


def _orch(runner: _SeqRunner, reviewer: _SpyReviewer, tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        planner_llm=_plan_llm(),
        reviewer=reviewer,
        executor_runner=runner,
        vault_dir=str(tmp_path),
        syntax_gate=verify_working_dir,
    )


def _setup_broken_py(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n", encoding="utf-8")


def test_flag_off_reviewer_called_despite_broken_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_VERIFY_GATE", raising=False)
    _setup_broken_py(tmp_path)

    runner = _SeqRunner(results=[ExecutorResult(status="ok", output_text="did work")])
    reviewer = _SpyReviewer(verdicts=["confirm"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )

    assert reviewer.calls == 1
    assert result.tasks[0].status == "done"


def test_enforce_reviewer_not_called_and_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VERIFY_GATE", "enforce")
    _setup_broken_py(tmp_path)

    runner = _SeqRunner(results=[ExecutorResult(status="ok", output_text="did work")])
    # The reviewer should not be called at all
    reviewer = _SpyReviewer(verdicts=["assert_not_called"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )

    assert reviewer.calls == 0
    assert result.tasks[0].status == "denied"
    assert "SyntaxError" in result.tasks[0].output_text or (
        result.tasks[0].review and "SyntaxError" in result.tasks[0].review.feedback
    )
    if result.tasks[0].review:
        assert result.tasks[0].review.reviewer == "mechanical:syntax"


def test_enforce_retry_contains_compiler_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VERIFY_GATE", "enforce")
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    _setup_broken_py(tmp_path)

    # We supply multiple results so it retries
    runner = _SeqRunner(
        results=[
            ExecutorResult(status="ok", output_text="attempt 1"),
            ExecutorResult(status="ok", output_text="attempt 2"),
        ]
    )
    reviewer = _SpyReviewer(verdicts=["assert_not_called", "assert_not_called"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )

    # Should be denied eventually because broken.py is never fixed in this test
    assert result.tasks[0].status == "denied"

    # Assert second attempt has syntax error in prompt
    assert len(runner.calls) >= 2
    prompt2 = runner.calls[1].prompt
    assert "SyntaxError" in prompt2


def test_shadow_reviewer_called_despite_broken_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VERIFY_GATE", "shadow")
    _setup_broken_py(tmp_path)

    runner = _SeqRunner(results=[ExecutorResult(status="ok", output_text="did work")])
    reviewer = _SpyReviewer(verdicts=["confirm"])
    result = _orch(runner, reviewer, tmp_path).run(
        _SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path)
    )

    assert reviewer.calls == 1
    assert result.tasks[0].status == "done"
