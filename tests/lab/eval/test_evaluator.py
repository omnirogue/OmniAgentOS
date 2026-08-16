from __future__ import annotations

import pytest

from omniagentos.lab.contracts import (
    Budgets,
    EvalCase,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    MetricSpec,
    SurfaceKind,
)
from omniagentos.lab.db import LabStore
from omniagentos.lab.eval.evaluator import JudgeView, ProtectedEvaluator
from omniagentos.lab.eval.grader import ProtectedGrader


def _suite_and_experiment(store: LabStore) -> tuple[EvalSuite, Experiment]:
    suite = EvalSuite(
        id="evs_1",
        discipline="coding",
        metrics=[
            MetricSpec(name="accuracy", role="primary", direction="maximize"),
            MetricSpec(name="safety", role="guardrail", direction="maximize", threshold=0.8),
            MetricSpec(name="complexity", role="regularizer", direction="minimize"),
            MetricSpec(name="latency_ms", role="efficiency", direction="minimize"),
        ],
        dataset_hash="ds-1",
    )
    store.create_eval_suite(suite)
    experiment = Experiment(
        id="exp_1",
        hypothesis="shorter prompt improves accuracy",
        discipline="coding",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_champ",
        challenger_surface_id="srf_chal",
        eval_suite_id=suite.id,
        primary_metric="accuracy",
        budgets=Budgets(replicates=1),
    )
    store.create_experiment(experiment)
    return suite, experiment


def _seed_cases(store: LabStore, grader: ProtectedGrader, suite_id: str) -> None:
    store.add_eval_case(
        EvalCase(
            id="evc_dev_1",
            suite=suite_id,
            split=EvalSplit.DEV,
            input={"q": 1},
            expected={"value": "a"},
            rubric="exact",
        )
    )
    store.add_eval_case(
        EvalCase(
            id="evc_dev_2",
            suite=suite_id,
            split=EvalSplit.DEV,
            input={"q": 2},
            expected={"value": "b"},
            rubric="exact",
        )
    )
    store.add_eval_case(
        EvalCase(
            id="evc_held_1",
            suite=suite_id,
            split=EvalSplit.HELD_OUT,
            input={"q": 3},
            rubric="exact",
        )
    )
    store.add_eval_case(
        EvalCase(
            id="evc_held_2",
            suite=suite_id,
            split=EvalSplit.HELD_OUT,
            input={"q": 4},
            rubric="exact",
        )
    )
    grader.put_expected("evc_dev_1", {"value": "a"})
    grader.put_expected("evc_dev_2", {"value": "b"})
    grader.put_expected("evc_held_1", {"value": "c"})
    grader.put_expected("evc_held_2", {"value": "d"})


def _record(
    store: LabStore,
    exp_id: str,
    arm: str,
    split: EvalSplit,
    metrics: dict[str, float],
    *,
    replicate: int = 0,
    per_case: dict[str, dict[str, float]] | None = None,
) -> None:
    store.record_eval_result(
        EvalResult(
            experiment_id=exp_id,
            arm=arm,
            suite_id="evs_1",
            suite_version=1,
            split=split,
            metrics=metrics,
            per_case=per_case or {},
            replicate=replicate,
        )
    )


# --- run_deterministic ------------------------------------------------------


def test_run_deterministic_delegates_to_the_grader_and_fills_suite_version() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)

    result = evaluator.run_deterministic(
        suite.id, "dev", "challenger", {"evc_dev_1": {"text": "a"}, "evc_dev_2": {"text": "wrong"}}
    )
    assert result.suite_version == suite.version
    assert result.experiment_id == ""  # the caller (L04-campaign) stamps this before persisting
    assert result.metrics["accuracy"] == 0.5


def test_run_deterministic_leaves_suite_version_at_placeholder_for_unknown_suite() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    grader.put_expected("evc_x", {"value": "y"})
    evaluator = ProtectedEvaluator(store, grader)
    result = evaluator.run_deterministic(
        "evs_unregistered", "dev", "champion", {"evc_x": {"text": "y"}}
    )
    assert result.suite_version == 0


# --- judge_blind --------------------------------------------------------------


def test_judge_blind_hides_arm_and_identity_from_judge_fn_then_attributes_after() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)

    seen_views: list[JudgeView] = []

    def spy_judge_fn(judge_lineage: str, view: JudgeView) -> tuple[float, str]:
        seen_views.append(view)
        return 1.0, "ok"

    case_outputs = {
        "evc_dev_1": {
            "champion": {"text": "answer variant one"},
            "challenger": {"text": "answer variant two"},
        },
    }
    pairs, seed = evaluator.make_blind_pairs(case_outputs, seed=7)
    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge-x"], judge_fn=spy_judge_fn)

    assert isinstance(seed, int)
    assert len(records) == len(pairs) == 2
    assert len(seen_views) == 2

    for view in seen_views:
        # exactly {input, rubric, output} — no `arm`, no candidate identity, no prior score.
        assert set(view.keys()) == {"input", "rubric", "output"}
        assert view["input"] == {"q": 1}
        assert view["rubric"] == "exact"
        assert "champion" not in str(view)
        assert "challenger" not in str(view)

    arms_seen = set()
    for record, view in zip(records, seen_views, strict=True):
        arms_seen.add(record.arm)
        assert case_outputs["evc_dev_1"][record.arm] == view["output"]
        assert record.case_id == "evc_dev_1"
        assert record.score == 1.0
        assert record.judge_lineage == "judge-x"
        assert record.rubric_dimension == "exact"
    assert arms_seen == {"champion", "challenger"}


def test_judge_blind_calls_every_judge_for_every_pair() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)

    case_outputs = {
        "evc_dev_1": {"champion": {"text": "a"}, "challenger": {"text": "b"}},
        "evc_dev_2": {"champion": {"text": "c"}, "challenger": {"text": "d"}},
    }
    pairs, _ = evaluator.make_blind_pairs(case_outputs)
    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge-a", "judge-b", "judge-c"])
    assert len(records) == len(pairs) * 3 == 12


def test_judge_blind_rejects_a_token_it_never_minted() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    forged_pair = [{"case_id": "evc_dev_1", "blind_token": "forged-token", "output": {"text": "x"}}]
    with pytest.raises(LookupError, match="not minted"):
        evaluator.judge_blind(suite.id, "dev", forged_pair, ["judge-x"])  # type: ignore[arg-type]


def test_judge_blind_rejects_a_case_not_in_candidate_cases() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    pairs, _ = evaluator.make_blind_pairs(
        {"evc_not_registered": {"champion": {"text": "x"}, "challenger": {"text": "y"}}}
    )
    with pytest.raises(LookupError, match="candidate_cases"):
        evaluator.judge_blind(suite.id, "dev", pairs, ["judge-x"])


def test_offline_heuristic_judge_default_scores_nonempty_output() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    case_outputs = {"evc_dev_1": {"champion": {"text": ""}, "challenger": {"text": "non-empty"}}}
    pairs, _ = evaluator.make_blind_pairs(case_outputs)
    records = evaluator.judge_blind(suite.id, "dev", pairs, ["default-judge"])
    scores_by_arm = {record.arm: record.score for record in records}
    assert scores_by_arm == {"champion": 0.0, "challenger": 1.0}


@pytest.mark.parametrize("mode", [None, "off", "unexpected"])
def test_rubric_validity_defaults_off(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv("OMNIAGENTOS_RUBRIC_VALIDITY_MODE", raising=False)
    else:
        monkeypatch.setenv("OMNIAGENTOS_RUBRIC_VALIDITY_MODE", mode)
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    pairs, _ = evaluator.make_blind_pairs(
        {"evc_dev_1": {"champion": {"text": "x"}, "challenger": {"text": "x" * 100}}}
    )

    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge"], judge_fn=_length_judge)

    assert len(records) == 2
    assert all("rubric_validity:" not in record.notes for record in records)


def _length_judge(judge_lineage: str, view: JudgeView) -> tuple[float, str]:
    del judge_lineage
    return float(len(str(view["output"].get("text", "")))), "judged"


def test_rubric_validity_shadow_records_finding_without_disqualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_RUBRIC_VALIDITY_MODE", "shadow")
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    pairs, _ = evaluator.make_blind_pairs(
        {"evc_dev_1": {"champion": {"text": "x"}, "challenger": {"text": "x" * 100}}}
    )

    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge"], judge_fn=_length_judge)

    assert len(records) == 2
    assert {record.score for record in records} == {1.0, 100.0}
    assert all(record.rubric_dimension == "exact" for record in records)
    assert all(
        "rubric_validity:length_correlation=1.000;limit=0.3;mode=shadow" in record.notes
        for record in records
    )


def test_rubric_validity_enforce_disqualifies_correlated_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_RUBRIC_VALIDITY_MODE", "enforce")
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    pairs, _ = evaluator.make_blind_pairs(
        {"evc_dev_1": {"champion": {"text": "x"}, "challenger": {"text": "x" * 100}}}
    )

    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge"], judge_fn=_length_judge)

    assert records == []


def test_rubric_validity_ignores_dimension_without_score_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_RUBRIC_VALIDITY_MODE", "enforce")
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, _ = _suite_and_experiment(store)
    _seed_cases(store, grader, suite.id)
    evaluator = ProtectedEvaluator(store, grader)
    pairs, _ = evaluator.make_blind_pairs(
        {"evc_dev_1": {"champion": {"text": "x"}, "challenger": {"text": "x" * 100}}}
    )

    records = evaluator.judge_blind(suite.id, "dev", pairs, ["judge"])

    assert len(records) == 2
    assert all(record.rubric_dimension == "exact" for record in records)


# --- score_experiment ---------------------------------------------------------


def test_score_experiment_dev_only_aggregates_and_computes_utility() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)

    _record(
        store,
        experiment.id,
        "champion",
        EvalSplit.DEV,
        {"accuracy": 0.5, "safety": 0.9, "complexity": 10.0, "latency_ms": 200.0},
    )
    _record(
        store,
        experiment.id,
        "challenger",
        EvalSplit.DEV,
        {"accuracy": 0.6, "safety": 0.85, "complexity": 12.0, "latency_ms": 250.0},
    )

    scorecard = evaluator.score_experiment(experiment.id, dev_only=True)

    assert scorecard.champion["accuracy"] == 0.5
    assert scorecard.challenger["accuracy"] == 0.6
    assert scorecard.primary_delta == pytest.approx(0.1)
    assert scorecard.safety_regression is False  # 0.85 still clears the 0.8 threshold
    assert scorecard.complexity_delta == pytest.approx(2.0)
    assert scorecard.audit_flags == []
    assert scorecard.confidence_interval is None  # single replicate: no variance to estimate
    # utility = quality_gain(0.1) - cost_penalty(50, from latency_ms) - 0
    #           - complexity_penalty(2.0) - risk_penalty(0)
    assert scorecard.utility == pytest.approx(0.1 - 50.0 - 2.0)


def test_score_experiment_flags_a_hard_constraint_regression() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)
    _record(store, experiment.id, "champion", EvalSplit.DEV, {"accuracy": 0.5, "safety": 0.9})
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.7, "safety": 0.5})

    scorecard = evaluator.score_experiment(experiment.id, dev_only=True)
    assert scorecard.safety_regression is True  # 0.5 breaches the 0.8 threshold


def test_score_experiment_requires_dev_results_for_both_arms_first() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)
    with pytest.raises(ValueError, match="dev-split"):
        evaluator.score_experiment(experiment.id, dev_only=True)


def test_score_experiment_held_out_is_gated_behind_a_completed_held_out_phase() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)
    _record(store, experiment.id, "champion", EvalSplit.DEV, {"accuracy": 0.5})
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.6})

    with pytest.raises(ValueError, match="held-out gate"):
        evaluator.score_experiment(experiment.id, dev_only=False)


def test_score_experiment_unknown_experiment_raises_lookup_error() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    evaluator = ProtectedEvaluator(store, grader)
    with pytest.raises(LookupError):
        evaluator.score_experiment("exp_missing", dev_only=True)


def test_score_experiment_held_out_leak_shaped_jump_is_flagged() -> None:
    """The central reward-hacking-crux assertion: a challenger that
    mysteriously nails EVERY held-out case (as if it had read the protected
    expected somehow) is caught by `audit_metric_jump` and surfaces as
    non-empty `Scorecard.audit_flags` — which is what forces HUMAN_REVIEW in
    L04-campaign's disposition gate regardless of how good the numbers
    otherwise look."""
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)

    _record(store, experiment.id, "champion", EvalSplit.DEV, {"accuracy": 0.5})
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.55})
    _record(store, experiment.id, "champion", EvalSplit.HELD_OUT, {"accuracy": 0.48})
    _record(
        store,
        experiment.id,
        "challenger",
        EvalSplit.HELD_OUT,
        {"accuracy": 1.0},
        per_case={f"evc_held_{i}": {"correct": 1.0} for i in range(4)},
    )

    scorecard = evaluator.score_experiment(experiment.id, dev_only=False)

    assert scorecard.audit_flags  # non-empty -> forces HUMAN_REVIEW downstream
    assert any("suspicious_perfect" in flag for flag in scorecard.audit_flags)
    assert any("zero_variance_perfect" in flag for flag in scorecard.audit_flags)
    assert scorecard.primary_delta == pytest.approx(0.52)
    # a non-empty audit taxes utility directly too (defense in depth on top
    # of the disposition-level HUMAN_REVIEW force).
    assert scorecard.utility is not None
    assert scorecard.utility < 0
    assert scorecard.utility == pytest.approx(0.52 - len(scorecard.audit_flags))


def test_score_experiment_flags_calibrated_held_out_only_gain() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)
    _record(store, experiment.id, "champion", EvalSplit.DEV, {"accuracy": 0.55})
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.58})
    _record(store, experiment.id, "champion", EvalSplit.HELD_OUT, {"accuracy": 0.55})
    _record(store, experiment.id, "challenger", EvalSplit.HELD_OUT, {"accuracy": 0.80})

    scorecard = evaluator.score_experiment(experiment.id, dev_only=False)

    assert any("generalization_gap_inversion:accuracy" in flag for flag in scorecard.audit_flags)


def test_score_experiment_confidence_interval_uses_challenger_replicates() -> None:
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    _, experiment = _suite_and_experiment(store)
    evaluator = ProtectedEvaluator(store, grader)
    _record(store, experiment.id, "champion", EvalSplit.DEV, {"accuracy": 0.5})
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.60}, replicate=0)
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.64}, replicate=1)
    _record(store, experiment.id, "challenger", EvalSplit.DEV, {"accuracy": 0.62}, replicate=2)

    scorecard = evaluator.score_experiment(experiment.id, dev_only=True)
    assert scorecard.confidence_interval is not None
    low, high = scorecard.confidence_interval
    assert low < 0.62 < high
    assert scorecard.challenger["accuracy"] == pytest.approx((0.60 + 0.64 + 0.62) / 3)
