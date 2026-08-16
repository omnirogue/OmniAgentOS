from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

import omniagentos.lab.campaign as campaign
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    Experiment,
    PromotionThreshold,
    Scorecard,
    SurfaceKind,
)
from omniagentos.lab.eval.evaluator import JudgeView


def _passing_card() -> Scorecard:
    return Scorecard(
        primary_delta=0.2,
        utility=0.2,
        cost_delta=0.01,
        complexity_delta=0.0,
    )


def _experiment(*, replicates: int = 2) -> Experiment:
    return Experiment(
        id="exp-e4",
        hypothesis="controlled hypothesis",
        discipline="writing",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="champion",
        challenger_surface_id="challenger",
        eval_suite_id="suite",
        primary_metric="quality",
        budgets=Budgets(replicates=replicates),
    )


def test_interval_delegates_to_stats_and_one_observation_is_unstable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[float]] = []
    stats = ModuleType("omniagentos.lab.stats")

    @dataclass
    class Estimate:
        values: list[float]

        def as_list(self) -> list[float] | None:
            return None if len(self.values) < 2 else [0.1, 0.3]

    def mean_confidence_interval(values: list[float]) -> Estimate:
        calls.append(values)
        return Estimate(values)

    stats.mean_confidence_interval = mean_confidence_interval  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.stats", stats)

    assert campaign._confidence_interval([0.2, 0.2]) == [0.1, 0.3]
    assert calls == [[0.2, 0.2]]
    assert campaign._confidence_interval([0.2]) is None
    assert campaign._stable([0.2], None) is False


@pytest.mark.parametrize("mode", [None, "off", "invalid", "shadow"])
def test_validity_gate_defaults_off_and_shadow_does_not_enforce(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv(campaign.LAB_VALIDITY_ENV, raising=False)
    else:
        monkeypatch.setenv(campaign.LAB_VALIDITY_ENV, mode)

    assert (
        campaign._meets_numeric_gate(
            _passing_card(),
            PromotionThreshold(),
            agreement=0.1,
            validity=0.1,
        )
        is True
    )


def test_validity_gate_enforces_agreement_and_validity_floors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.LAB_VALIDITY_ENV, "enforce")
    threshold = PromotionThreshold()

    assert (
        campaign._meets_numeric_gate(_passing_card(), threshold, agreement=0.59, validity=1.0)
        is False
    )
    assert (
        campaign._meets_numeric_gate(_passing_card(), threshold, agreement=1.0, validity=0.79)
        is False
    )
    assert (
        campaign._meets_numeric_gate(_passing_card(), threshold, agreement=0.60, validity=0.80)
        is True
    )


class FlaggingReviewer:
    def __call__(self, experiment: Experiment, suite: Mapping[str, Any]) -> Mapping[str, Any]:
        del experiment, suite
        return {"audit_flags": ["methodology:blinding-leak"]}


class FinishStore:
    def __init__(self, provenance: dict[str, float] | None = None) -> None:
        self.updated: dict[str, Any] = {}
        self.provenance = provenance

    def get_eval_suite(self, _suite_id: str) -> dict[str, Any]:
        return {"metrics": [{"name": "quality"}]}

    def get_surface(self, surface_id: str) -> dict[str, Any]:
        return {
            "id": surface_id,
            "kind": SurfaceKind.PROMPT,
            "status": "champion" if surface_id == "champion" else "challenger",
        }

    def get_champion(self, _discipline: str, _kind: str) -> dict[str, Any]:
        return {"surface_id": "champion"}

    def get_verdict_provenance(self, _experiment_id: str) -> dict[str, float] | None:
        return self.provenance

    def update_experiment(self, _exp_id: str, fields: dict[str, Any]) -> None:
        self.updated = fields


@pytest.mark.parametrize("mode", [None, "off", "unexpected", "shadow"])
def test_methodology_off_and_shadow_do_not_change_disposition_flags(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv(campaign.METHODOLOGY_REVIEW_ENV, raising=False)
    else:
        monkeypatch.setenv(campaign.METHODOLOGY_REVIEW_ENV, mode)
    card = _passing_card()

    campaign._apply_methodology_review(
        card, _experiment(), {"metrics": [{"name": "quality"}]}, FlaggingReviewer()
    )

    assert card.audit_flags == []


def test_methodology_enforce_flags_route_to_human_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.METHODOLOGY_REVIEW_ENV, "enforce")
    card = _passing_card()
    experiment = _experiment()

    store = FinishStore()
    reviewer_token = campaign._methodology_reviewer_context.set(FlaggingReviewer())
    try:
        disposition = campaign._finish(store, experiment, card, True)
    finally:
        campaign._methodology_reviewer_context.reset(reviewer_token)

    assert card.audit_flags == ["methodology:blinding-leak"]
    assert disposition == Disposition.HUMAN_REVIEW
    assert store.updated["disposition"] == Disposition.HUMAN_REVIEW


def test_model_judge_enforce_keeps_unconditional_evidence_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.LAB_VALIDITY_ENV, "off")
    card = _passing_card()
    card.audit_flags = [campaign.OFFLINE_HEURISTIC_FLAG]
    store = FinishStore()

    disposition = campaign._finish(
        store,
        _experiment(),
        card,
        True,
        used_model_judge=True,
        model_judge_enforced=True,
    )

    assert campaign.OFFLINE_HEURISTIC_FLAG not in card.audit_flags
    assert campaign.PROMOTION_FLOOR_FLAG in card.audit_flags
    assert disposition == Disposition.HUMAN_REVIEW
    assert store.updated["disposition"] != Disposition.PROMOTE


def test_model_judge_enforce_can_promote_with_real_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.LAB_VALIDITY_ENV, "off")
    card = _passing_card()
    card.audit_flags = [campaign.OFFLINE_HEURISTIC_FLAG]
    store = FinishStore(
        {
            "agreement": campaign.MIN_JUDGE_AGREEMENT,
            "validity": campaign.MIN_JUDGE_VALIDITY,
        }
    )

    disposition = campaign._finish(
        store,
        _experiment(),
        card,
        True,
        used_model_judge=True,
        model_judge_enforced=True,
    )

    assert card.audit_flags == []
    assert disposition == Disposition.PROMOTE


class FakeRecord:
    def __init__(self) -> None:
        self.experiment_id = ""

    def model_copy(self, *, update: dict[str, Any]) -> FakeRecord:
        copied = FakeRecord()
        copied.experiment_id = str(update["experiment_id"])
        return copied


class JudgeStore:
    def __init__(self) -> None:
        self.records: list[FakeRecord] = []
        self.updated: dict[str, Any] = {}

    def record_judge(self, record: FakeRecord) -> None:
        self.records.append(record)

    def update_experiment(self, _exp_id: str, fields: dict[str, Any]) -> None:
        self.updated = fields


class JudgeEvaluator:
    def __init__(self) -> None:
        self.received_fn: Any = None
        self.received_fns: list[Any] = []
        self.judges: list[str] = []

    def make_blind_pairs(self, cases: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        assert set(cases["case-1"]) == {"champion", "challenger"}
        return [
            {
                "case_id": "case-1",
                "blind_token": "blind-a",
                "output": cases["case-1"]["champion"],
            },
            {
                "case_id": "case-1",
                "blind_token": "blind-b",
                "output": cases["case-1"]["challenger"],
            },
        ], 17

    def judge_blind(
        self,
        _suite: str,
        _split: str,
        _pairs: list[dict[str, Any]],
        judges: list[str],
        *,
        judge_fn: Any = None,
    ) -> list[FakeRecord]:
        self.received_fns.append(judge_fn)
        if judge_fn is not None:
            self.received_fn = judge_fn
        self.judges = judges
        return [FakeRecord()]


class FakePairwiseJudge:
    judge_identity = "model-backed:cross-lineage-test-transport"

    def __init__(self) -> None:
        self.registered_pairs: list[dict[str, Any]] = []

    def register_pairs(self, pairs: Sequence[Mapping[str, Any]]) -> None:
        self.registered_pairs = [dict(pair) for pair in pairs]

    def __call__(self, judge_lineage: str, view: JudgeView) -> tuple[float, str]:
        del judge_lineage, view
        return 1.0, "model-backed forced pairwise judgment"


def test_model_judge_enforce_threads_real_cross_lineage_judge_fn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.MODEL_JUDGE_ENV, "enforce")
    store = JudgeStore()
    evaluator = JudgeEvaluator()
    judge = FakePairwiseJudge()

    campaign._record_blind_judgments(
        store,
        evaluator,
        _experiment(),
        {"case-1": {"text": "champion"}},
        {"case-1": {"text": "challenger"}},
        ["grok-4", "claude-opus-5"],
        judge_fn=judge,
    )

    assert evaluator.received_fn is judge
    assert evaluator.judges == ["grok-4", "claude-opus-5"]
    assert {pair["blind_token"] for pair in judge.registered_pairs} == {
        "blind-a",
        "blind-b",
    }
    assert store.records[0].experiment_id == "exp-e4"


@pytest.mark.parametrize("mode", [None, "off", "unexpected"])
def test_model_judge_defaults_off_and_does_not_prime(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv(campaign.MODEL_JUDGE_ENV, raising=False)
    else:
        monkeypatch.setenv(campaign.MODEL_JUDGE_ENV, mode)
    evaluator = JudgeEvaluator()
    judge = FakePairwiseJudge()

    campaign._record_blind_judgments(
        JudgeStore(),
        evaluator,
        _experiment(),
        {"case-1": {"text": "champion"}},
        {"case-1": {"text": "challenger"}},
        ["grok-4", "claude-opus-5"],
        judge_fn=judge,
    )

    assert evaluator.received_fn is None
    assert judge.registered_pairs == []


def test_model_judge_shadow_uses_real_pairwise_judge_without_enforcing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(campaign.MODEL_JUDGE_ENV, "shadow")
    evaluator = JudgeEvaluator()
    judge = FakePairwiseJudge()

    outcome = campaign._record_blind_judgments(
        JudgeStore(),
        evaluator,
        _experiment(),
        {"case-1": {"text": "champion"}},
        {"case-1": {"text": "challenger"}},
        ["grok-4", "claude-opus-5"],
        judge_fn=judge,
    )

    assert evaluator.received_fn is judge
    assert evaluator.received_fns == [judge, None]
    assert len(judge.registered_pairs) == 2
    assert outcome.used_model_judge is True
    assert outcome.used_offline_heuristic is True


@pytest.mark.parametrize(
    ("judge_fn", "judges"),
    [
        (lambda _lineage, _view: (1.0, "not pairwise"), ["grok-4", "claude-opus-5"]),
        (FakePairwiseJudge(), ["grok-4"]),
        (FakePairwiseJudge(), ["claude-opus-5", "claude-sonnet-5"]),
    ],
)
def test_model_judge_enforce_rejects_fake_or_single_lineage_panels(
    monkeypatch: pytest.MonkeyPatch,
    judge_fn: Any,
    judges: list[str],
) -> None:
    monkeypatch.setenv(campaign.MODEL_JUDGE_ENV, "enforce")
    store = JudgeStore()
    evaluator = JudgeEvaluator()
    experiment = _experiment()

    outcome = campaign._record_blind_judgments(
        store,
        evaluator,
        experiment,
        {"case-1": {"text": "champion"}},
        {"case-1": {"text": "challenger"}},
        judges,
        judge_fn=judge_fn,
    )

    assert outcome.used_model_judge is False
    assert outcome.used_offline_heuristic is False
    assert outcome.ineligible_reason is not None
    assert "model-backed forced-pairwise" in outcome.ineligible_reason
    assert store.records == []
    assert evaluator.received_fn is None

    disposition = campaign._decide_invalid(
        store,
        experiment,
        outcome.ineligible_reason,
        Scorecard(
            primary_delta=1.0,
            utility=1.0,
            cost_delta=0.0,
            complexity_delta=0.0,
        ),
    )
    assert disposition == Disposition.INVALID
    assert store.updated["disposition"] == Disposition.INVALID
    assert store.updated["disposition"] != Disposition.PROMOTE
