"""Reflexion on gate failure (OMNIAGENTOS_REFLEXION=1) in the corrective retry.

Flag OFF: the retry carries the reviewer's raw feedback UNCHANGED. Flag ON: a
one-paragraph Reflexion reflection (arXiv:2303.11366) is built over the failure
evidence, persisted to its OWN JSONL store, and prepended above the raw feedback so it
reaches ``build_executor_prompt``'s ``review_feedback`` on the next attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator.contracts import ExecutorRequest, ExecutorResult, ReviewVerdict
from omniagentos.selfimprove.reflexion import Reflection

_SIMPLE_GOAL = "Add a docstring to the greet function"
_MARKER = "REFLECTION_MARKER: reproduce the failing assertion first"


def _plan_llm() -> Any:
    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        return {
            "project_name": "Greeter",
            "description": "Improve the greeter",
            "complexity": "simple",
            "tasks": [
                {"title": "Task one", "description": "do it", "acceptance_criteria": ["works"]}
            ],
        }

    return _llm


@dataclass
class _Runner:
    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        return ExecutorResult(status="ok", output_text="the wrong output", session_id="ses")


@dataclass
class _Reviewer:
    verdicts: list[str]
    feedback: str = "address criterion 'works'"
    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return ReviewVerdict(verdict=verdict, feedback=self.feedback, reviewer="mock")


@dataclass
class _CapturingReflector:
    """Records the (task_summary, evidence) it was called with, returns a marker."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, task_summary: str, evidence: str) -> Reflection:
        self.calls.append((task_summary, evidence))
        return Reflection(paragraph=_MARKER, source="template")


def test_reflexion_off_keeps_raw_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_REFLEXION", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_CASCADE", raising=False)
    runner = _Runner()
    reflector = _CapturingReflector()
    orch = Orchestrator(
        planner_llm=_plan_llm(),
        reviewer=_Reviewer(verdicts=["deny", "confirm"]),
        executor_runner=runner,
        vault_dir=str(tmp_path),
        reflector=reflector,
        reflexion_store_dir=str(tmp_path / "reflexion"),
    )
    orch.run(_SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path))

    assert len(runner.calls) == 2
    retry_prompt = runner.calls[1].prompt
    assert "address criterion 'works'" in retry_prompt
    assert _MARKER not in retry_prompt
    assert reflector.calls == []  # reflector never invoked with the flag off
    assert not (tmp_path / "reflexion" / "reflections.jsonl").exists()


def test_reflexion_on_prepends_reflection_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_REFLEXION", "1")
    monkeypatch.delenv("OMNIAGENTOS_CASCADE", raising=False)
    runner = _Runner()
    reflector = _CapturingReflector()
    store_dir = tmp_path / "reflexion"
    orch = Orchestrator(
        planner_llm=_plan_llm(),
        reviewer=_Reviewer(verdicts=["deny", "confirm"]),
        executor_runner=runner,
        vault_dir=str(tmp_path),
        reflector=reflector,
        reflexion_store_dir=str(store_dir),
    )
    orch.run(_SIMPLE_GOAL, priority="balanced", working_dir=str(tmp_path))

    assert len(runner.calls) == 2
    retry_prompt = runner.calls[1].prompt
    # The reflection paragraph reached the corrective prompt, ABOVE the raw feedback.
    assert _MARKER in retry_prompt
    assert "address criterion 'works'" in retry_prompt
    # Evidence carried the reviewer feedback + the prior attempt's output tail.
    assert reflector.calls, "reflector must be invoked on the DENY retry"
    _summary, evidence = reflector.calls[0]
    assert "address criterion 'works'" in evidence
    assert "the wrong output" in evidence
    # Persisted to its OWN store.
    persisted = store_dir / "reflections.jsonl"
    assert persisted.exists()
    assert _MARKER in persisted.read_text()
