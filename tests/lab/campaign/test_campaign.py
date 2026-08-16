from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from omniagentos.lab.campaign import (
    _meets_numeric_gate,
    propose_experiments,
    run_experiment,
    stall_check,
)
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    ExperimentStatus,
    MetricSpec,
    PromotionThreshold,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
)


class FakeStore:
    def __init__(self, experiment: Experiment, *, safety_relevant: bool = False) -> None:
        self.experiment = experiment
        self.updates: list[dict[str, Any]] = []
        self.results: list[EvalResult] = []
        self.created: list[Experiment] = []
        self.suite = EvalSuite(
            id=experiment.eval_suite_id,
            discipline=experiment.discipline,
            metrics=[MetricSpec(name="quality", role="primary")],
        ).model_dump(mode="json")
        self.surfaces = {
            experiment.champion_surface_id: Surface(
                id=experiment.champion_surface_id,
                kind=SurfaceKind.PROMPT,
                discipline=experiment.discipline,
                path="vault/prompts/worker/v1.md",
                content_hash="champion",
                status=SurfaceStatus.CHAMPION,
            ).model_dump(mode="json"),
            experiment.challenger_surface_id: Surface(
                id=experiment.challenger_surface_id,
                kind=SurfaceKind.PROMPT,
                discipline=experiment.discipline,
                path="vault/prompts/worker/v2.md",
                content_hash="challenger",
                safety_relevant=safety_relevant,
                status=SurfaceStatus.CHALLENGER,
            ).model_dump(mode="json"),
        }

    def get_experiment(self, _exp_id: str) -> Experiment:
        return self.experiment

    def update_experiment(self, _exp_id: str, fields: dict[str, Any]) -> None:
        self.updates.append(fields)

    def get_surface(self, surface_id: str) -> dict[str, Any] | None:
        return self.surfaces.get(surface_id)

    def get_eval_suite(self, _suite_id: str) -> dict[str, Any]:
        return self.suite

    def record_eval_result(self, result: EvalResult) -> None:
        self.results.append(result)

    def record_judge(self, _record: Any) -> None:
        pass

    def get_champion(self, _discipline: str, _kind: str) -> dict[str, Any]:
        return {"surface_id": self.experiment.champion_surface_id, "rollback_to_surface_id": None}

    def list_experiments(
        self, discipline: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        del discipline, status, limit
        return []

    def list_eval_suites(self, _discipline: str) -> list[dict[str, Any]]:
        return [self.suite]

    def create_experiment(self, experiment: Experiment) -> None:
        self.created.append(experiment)


class FakeEvaluator:
    def __init__(self, *, flags: list[str] | None = None) -> None:
        self.flags = flags or []
        self.held_out_calls = 0

    def run_deterministic(
        self, suite_id: str, split: EvalSplit, arm: str, outputs: dict[str, Any]
    ) -> EvalResult:
        del outputs
        return EvalResult(
            experiment_id="ignored",
            arm=arm,
            suite_id=suite_id,
            suite_version=1,
            split=split,
            metrics={"quality": 0.80 if arm == "champion" else 0.86},
        )

    def score_experiment(self, _exp_id: str, *, dev_only: bool) -> Scorecard:
        if not dev_only:
            self.held_out_calls += 1
        return Scorecard(audit_flags=self.flags if dev_only else [])


def _experiment() -> Experiment:
    return Experiment(
        id="exp-campaign",
        hypothesis="test",
        discipline="writing",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf-champion",
        challenger_surface_id="srf-challenger",
        eval_suite_id="suite-writing",
        primary_metric="quality",
        budgets=Budgets(replicates=2),
    )


def _install_executor(monkeypatch: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    executor = ModuleType("omniagentos.lab.executor")

    def run_surface_over_cases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"outputs": {"case-1": {"text": "candidate-safe output"}}, "manifests": []}

    executor.run_surface_over_cases = run_surface_over_cases  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.executor", executor)
    return calls


def test_run_experiment_promotes_and_scrubs_executor(monkeypatch: Any) -> None:
    calls = _install_executor(monkeypatch)
    store = FakeStore(_experiment())

    assert (
        run_experiment(store, FakeEvaluator(), "exp-campaign", dry_run=True) == Disposition.PROMOTE
    )
    assert len(store.results) == 8
    assert all(call["env_scrub"] is True and call["dry_run"] is True for call in calls)
    assert store.updates[-1]["disposition"] == Disposition.PROMOTE


def test_safety_and_audit_flags_force_human_review(monkeypatch: Any) -> None:
    _install_executor(monkeypatch)
    assert (
        run_experiment(
            FakeStore(_experiment(), safety_relevant=True), FakeEvaluator(), "exp-campaign"
        )
        == Disposition.HUMAN_REVIEW
    )
    assert (
        run_experiment(
            FakeStore(_experiment()), FakeEvaluator(flags=["metric jump"]), "exp-campaign"
        )
        == Disposition.HUMAN_REVIEW
    )


def test_numeric_gate_honors_cost_and_configurable_safety_caps() -> None:
    card = Scorecard(
        primary_delta=0.2,
        utility=0.2,
        cost_delta=0.15,
        complexity_delta=0.0,
        safety_regression=True,
    )
    threshold = PromotionThreshold(cost_increase_max=0.10)

    assert _meets_numeric_gate(card, threshold) is False
    assert (
        _meets_numeric_gate(
            card.model_copy(update={"cost_delta": 0.05}),
            threshold.model_copy(update={"no_safety_regressions": False}),
        )
        is True
    )


def test_proposal_mix_and_stall_detection(monkeypatch: Any) -> None:
    store = FakeStore(_experiment())
    import omniagentos.lab.surfaces as surfaces

    number = 0

    def version_prompt(*args: Any, **kwargs: Any) -> Surface:
        nonlocal number
        number += 1
        return Surface(
            id=f"srf-candidate-{number}",
            kind=SurfaceKind.PROMPT,
            discipline="writing",
            path=f"vault/prompts/worker/v{number + 2}.md",
            content_hash=str(number),
        )

    monkeypatch.setattr(surfaces, "version_prompt", version_prompt, raising=False)
    proposals = propose_experiments(store, "writing")
    assert Counter(item.explore_policy for item in proposals) == {
        "exploit": 12,
        "explore": 5,
        "simplify": 2,
        "red_team": 1,
    }
    assert stall_check(store, "writing") is True

    store.list_experiments = lambda **_kwargs: [  # type: ignore[method-assign]
        {"disposition": Disposition.PROMOTE, "status": ExperimentStatus.DECIDED}
    ]
    assert stall_check(store, "writing") is False


def test_propose_experiments_is_non_empty_on_a_real_seeded_labstore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 regression: exercise production storage, not FakeStore's extra API."""
    from omniagentos.lab import surfaces
    from omniagentos.lab.db import LabStore
    from omniagentos.lab.eval.grader import ProtectedGrader
    from omniagentos.lab.seed import DEMO_DISCIPLINE, ensure_demo_seeded

    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    store = LabStore(":memory:")
    with ProtectedGrader(":memory:") as grader:
        ensure_demo_seeded(store, grader)

    assert hasattr(store, "list_eval_suites")
    assert store.list_eval_suites(DEMO_DISCIPLINE)
    proposals = propose_experiments(store, DEMO_DISCIPLINE)
    assert proposals
