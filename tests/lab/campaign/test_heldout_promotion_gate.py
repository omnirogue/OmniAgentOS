from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from omniagentos.lab.campaign import run_experiment
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    MetricSpec,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
)


class _Store:
    def __init__(self) -> None:
        self.experiment = Experiment(
            id="exp-heldout",
            hypothesis="dev-only improvement must not promote",
            discipline="coding",
            mutable_surface_kind=SurfaceKind.PROMPT,
            champion_surface_id="champion",
            challenger_surface_id="challenger",
            eval_suite_id="suite",
            primary_metric="quality",
            budgets=Budgets(replicates=2),
        )
        self.suite = EvalSuite(
            id="suite",
            discipline="coding",
            metrics=[MetricSpec(name="quality", role="primary")],
        ).model_dump(mode="json")
        self.surfaces = {
            arm: Surface(
                id=arm,
                kind=SurfaceKind.PROMPT,
                discipline="coding",
                path=f"{arm}.md",
                content_hash=arm,
                status=(
                    SurfaceStatus.CHAMPION if arm == "champion" else SurfaceStatus.CHALLENGER
                ),
            ).model_dump(mode="json")
            for arm in ("champion", "challenger")
        }
        self.results: list[EvalResult] = []
        self.updates: list[dict[str, Any]] = []

    def get_experiment(self, _id: str) -> Experiment:
        return self.experiment

    def get_surface(self, surface_id: str) -> dict[str, Any]:
        return self.surfaces[surface_id]

    def get_eval_suite(self, _id: str) -> dict[str, Any]:
        return self.suite

    def record_eval_result(self, result: EvalResult) -> None:
        self.results.append(result)

    def update_experiment(self, _id: str, fields: dict[str, Any]) -> None:
        self.updates.append(fields)

    def get_champion(self, _discipline: str, _kind: str) -> dict[str, Any]:
        return {"surface_id": "champion", "rollback_to_surface_id": None}


class _WinsDevLosesHeldout:
    def run_deterministic(
        self,
        suite_id: str,
        split: EvalSplit,
        arm: str,
        outputs: dict[str, Any],
    ) -> EvalResult:
        del outputs
        if split == EvalSplit.DEV:
            score = 0.80 if arm == "champion" else 0.90
        else:
            score = 0.90 if arm == "champion" else 0.70
        return EvalResult(
            experiment_id="ignored",
            arm=arm,
            suite_id=suite_id,
            suite_version=1,
            split=split,
            metrics={"quality": score},
        )

    def score_experiment(self, _id: str, *, dev_only: bool) -> Scorecard:
        del dev_only
        return Scorecard()


def test_heldout_loss_rejects_and_both_splits_counterbalance_order(monkeypatch: Any) -> None:
    store = _Store()
    calls: list[tuple[str, str]] = []
    executor = ModuleType("omniagentos.lab.executor")

    def run_surface_over_cases(
        _store: Any,
        surface: dict[str, Any],
        _suite_id: str,
        split: str,
        _budgets: Budgets,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((str(split), str(surface["id"])))
        return {"outputs": {"case": {"text": "safe output"}}, "manifests": []}

    executor.run_surface_over_cases = run_surface_over_cases  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.executor", executor)

    disposition = run_experiment(store, _WinsDevLosesHeldout(), "exp-heldout", dry_run=True)

    assert disposition == Disposition.REJECT
    assert store.updates[-1]["scorecard"].primary_delta == pytest.approx(-0.20)
    assert [(split, arm) for split, arm in calls[:4]] == [
        ("dev", "champion"),
        ("dev", "challenger"),
        ("dev", "challenger"),
        ("dev", "champion"),
    ]
    assert [(split, arm) for split, arm in calls[4:]] == [
        ("held_out", "champion"),
        ("held_out", "challenger"),
        ("held_out", "challenger"),
        ("held_out", "champion"),
    ]
    assert [(result.split, result.replicate) for result in store.results] == [
        (EvalSplit.DEV, 0),
        (EvalSplit.DEV, 0),
        (EvalSplit.DEV, 1),
        (EvalSplit.DEV, 1),
        (EvalSplit.HELD_OUT, 0),
        (EvalSplit.HELD_OUT, 0),
        (EvalSplit.HELD_OUT, 1),
        (EvalSplit.HELD_OUT, 1),
    ]
