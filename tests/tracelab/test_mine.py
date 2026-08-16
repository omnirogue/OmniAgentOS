"""Corpus aggregation must not invent flattering numbers for empty samples."""

from __future__ import annotations

import pytest

from omniagentos.tracelab.mine import MiningResult, _summarize, mine_traces
from omniagentos.tracelab.report import render_report


def test_empty_metric_summary_does_not_report_zero_means() -> None:
    """n=0 is not a measured mean of 0.0 — that reads as "no errors / no cost".

    Counterfeit: return mean/median/max = 0.0 whenever the value list is empty.
    """
    summary = _summarize([])
    assert summary["n"] == 0.0
    assert summary["mean"] is None
    assert summary["median"] is None
    assert summary["max"] is None

    result = mine_traces([])
    for name, stats in result.metric_summary.items():
        assert stats["n"] == 0.0, name
        assert stats["mean"] is None, name
        assert stats["median"] is None, name
        assert stats["max"] is None, name


def test_report_refuses_an_unknown_metric_count() -> None:
    result = MiningResult(
        metric_summary={
            "tool_calls": {
                "n": None,
                "mean": None,
                "median": None,
                "max": None,
            }
        }
    )

    with pytest.raises(TypeError, match="count must be measured"):
        render_report(result, [])
