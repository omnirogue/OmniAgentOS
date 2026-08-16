from __future__ import annotations

from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Any

import pytest

from omniagentos.intake.planner import PlannedTask
from omniagentos.orchestrator.contracts import ExecutorResult
from omniagentos.orchestrator.review import CrossLineageReviewer, _run_validation
from omniagentos.swarm.plan_safety import VerifierCommandError


@dataclass
class _CountingAdapter:
    calls: int = 0

    def run(self, input: Any) -> Any:
        self.calls += 1
        raise AssertionError("mechanical DENY must not call the LLM adapter")


def _review(
    task: PlannedTask,
    result: ExecutorResult,
    *,
    validation_runner: Any = None,
) -> tuple[Any, _CountingAdapter]:
    adapter = _CountingAdapter()
    reviewer = CrossLineageReviewer(
        adapter=adapter,
        validation_runner=validation_runner,
    )
    return reviewer.review(task=task, spec_markdown="spec", result=result), adapter


def test_empty_output_denies_without_llm() -> None:
    verdict, adapter = _review(
        PlannedTask(title="Task"),
        ExecutorResult(status="ok", output_text=" \n"),
    )
    assert verdict.verdict == "deny"
    assert "empty" in verdict.feedback
    assert adapter.calls == 0


def test_missing_expected_commit_denies_without_llm() -> None:
    verdict, adapter = _review(
        PlannedTask(title="Task", commits_expected=True),
        ExecutorResult(status="ok", output_text="Implemented the requested change."),
    )
    assert verdict.verdict == "deny"
    assert "no commit" in verdict.feedback
    assert adapter.calls == 0


def test_failing_validation_denies_without_llm() -> None:
    calls: list[tuple[str, str | None]] = []

    def _fail(command: str, cwd: str | None) -> tuple[bool, str]:
        calls.append((command, cwd))
        return False, "1 failed"

    verdict, adapter = _review(
        PlannedTask(title="Task", validation_command="pytest -q"),
        ExecutorResult(
            status="ok",
            output_text="Implemented in commit abcdef123456.",
            working_dir="/tmp/project",
        ),
        validation_runner=_fail,
    )
    assert verdict.verdict == "deny"
    assert "validation command failed" in verdict.feedback
    assert "1 failed" in verdict.feedback
    assert calls == [("pytest -q", "/tmp/project")]
    assert adapter.calls == 0


def test_default_validation_executes_immutable_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(argv: object, **kwargs: object) -> CompletedProcess[str]:
        calls.append((argv, kwargs))
        return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("omniagentos.orchestrator.review.subprocess.run", fake_run)

    result = _run_validation("pytest -q tests/orchestrator", "/tmp/project")

    assert result.returncode == 0
    assert calls == [
        (
            ("pytest", "-q", "tests/orchestrator"),
            {
                "cwd": "/tmp/project",
                "capture_output": True,
                "text": True,
                "timeout": 300,
                "check": False,
            },
        )
    ]


def test_default_validation_rejects_model_authored_shell_syntax() -> None:
    with pytest.raises(VerifierCommandError, match="forbidden shell syntax"):
        _run_validation("true; touch PWNED", "/tmp/project")
