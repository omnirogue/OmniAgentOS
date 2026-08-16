from __future__ import annotations

from typing import Any

import pytest

from omniagentos.lab.contracts import EvalResult
from omniagentos.lab.eval import scorecard
from omniagentos.lab.stats import IntervalEstimate


def _result(value: float | None, replicate: int = 0) -> EvalResult:
    return EvalResult(
        experiment_id="exp_stats",
        arm="challenger",
        suite_id="suite_stats",
        suite_version=1,
        split="dev",
        metrics={} if value is None else {"quality": value},
        per_case={},
        replicate=replicate,
    )


def test_scorecard_interval_delegates_to_lab_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_interval(
        values: list[float],
        *,
        confidence: float,
    ) -> IntervalEstimate:
        seen["values"] = values
        seen["confidence"] = confidence
        return IntervalEstimate(bounds=(0.1, 0.9), observations=len(values), stable=True)

    monkeypatch.setattr(scorecard.lab_stats, "mean_confidence_interval", fake_interval)

    assert scorecard.confidence_interval_95(
        "quality", [_result(0.2), _result(None), _result(0.8, replicate=1)]
    ) == [0.1, 0.9]
    assert seen == {"values": [0.2, 0.8], "confidence": 0.95}


@pytest.mark.parametrize("results", [[], [_result(0.75)]])
def test_scorecard_reports_undersampled_interval_as_explicitly_unstable(
    results: list[EvalResult],
) -> None:
    estimate = scorecard.confidence_interval_estimate("quality", results)
    assert estimate.stable is False
    assert estimate.reason == "fewer_than_two_observations"
    assert estimate.observations == len(results)
    assert scorecard.confidence_interval_95("quality", results) is None


def test_scorecard_returns_interval_for_two_or_more_observations() -> None:
    interval = scorecard.confidence_interval_95(
        "quality",
        [_result(0.4), _result(0.6, replicate=1)],
    )
    assert interval is not None
    assert interval[0] < 0.5 < interval[1]
