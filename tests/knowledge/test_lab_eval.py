"""Recall evaluation output is stable enough for lab consumers."""

from __future__ import annotations

from types import SimpleNamespace

from omniagentos.knowledge import lab_eval


def test_eval_suite_reports_expected_shape_and_precision(monkeypatch) -> None:
    def fake_recall(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            facts=[
                SimpleNamespace(fact=SimpleNamespace(id=1)),
                SimpleNamespace(fact=SimpleNamespace(id=3)),
            ]
        )

    monkeypatch.setattr(lab_eval, "recall", fake_recall)
    report = lab_eval.eval_suite(
        object(),
        cases=[{"id": "case-a", "input": {"prompt": "canary"}, "expected_fact_ids": [1, 2]}],
        k=2,
    )
    assert report["name"] == "knowledge-recall"
    assert report["version"] == "0.1.0"
    assert set(report) == {"name", "version", "metrics", "cases"}
    assert report["cases"][0]["metrics"]["precision_at_k"] == 0.5
    assert report["cases"][0]["metrics"]["recall_at_k"] == 0.5
    assert report["metrics"]["precision_at_k"] == 0.5
    assert report["metrics"]["recall_at_k"] == 0.5


def test_eval_suite_empty_case_list_is_unknown_not_zero(monkeypatch) -> None:
    """Zero cases is a non-result: macro rates must not look like measured 0.0.

    Counterfeit that must still fail: return 0.0 (or any float) while setting
    cases=0 / a side flag — optimizers that only read the rate would still learn.
    """
    monkeypatch.setattr(
        lab_eval,
        "recall",
        lambda *_a, **_k: SimpleNamespace(facts=[]),
    )
    report = lab_eval.eval_suite(object(), cases=[], k=5)
    assert report["metrics"]["cases"] == 0
    assert report["metrics"]["precision_at_k"] is None
    assert report["metrics"]["recall_at_k"] is None
    assert report["cases"] == []


def test_eval_suite_empty_retrieved_precision_is_unknown(monkeypatch) -> None:
    """Empty retrieval → precision@k denominator is empty → unknown, not 0.0."""

    monkeypatch.setattr(
        lab_eval,
        "recall",
        lambda *_a, **_k: SimpleNamespace(facts=[]),
    )
    report = lab_eval.eval_suite(
        object(),
        cases=[{"id": "empty-ret", "prompt": "q", "expected_fact_ids": [1]}],
        k=3,
    )
    case_metrics = report["cases"][0]["metrics"]
    assert case_metrics["precision_at_k"] is None
    # Recall is defined: expected non-empty, hits=0 → 0.0
    assert case_metrics["recall_at_k"] == 0.0
    # Macro precision has no defined cases → unknown; macro recall averages 0.0
    assert report["metrics"]["precision_at_k"] is None
    assert report["metrics"]["recall_at_k"] == 0.0


def test_eval_suite_empty_expected_recall_is_unknown(monkeypatch) -> None:
    """Empty gold set → recall@k denominator is empty → unknown, not 0.0."""

    monkeypatch.setattr(
        lab_eval,
        "recall",
        lambda *_a, **_k: SimpleNamespace(
            facts=[SimpleNamespace(fact=SimpleNamespace(id=9))]
        ),
    )
    report = lab_eval.eval_suite(
        object(),
        cases=[{"id": "no-gold", "prompt": "q", "expected_fact_ids": []}],
        k=3,
    )
    case_metrics = report["cases"][0]["metrics"]
    # Precision is defined: retrieved non-empty, hits=0 → 0.0
    assert case_metrics["precision_at_k"] == 0.0
    assert case_metrics["recall_at_k"] is None
    assert report["metrics"]["precision_at_k"] == 0.0
    assert report["metrics"]["recall_at_k"] is None
