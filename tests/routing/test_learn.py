"""Tests for omniagentos.routing.learn -- pure trace-mining arithmetic. All
traces here are synthetic dicts (no cascade run needed); read_traces is also
exercised against a real JSONL file to cover its file-reading/filtering
behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.routing.cascade import CascadeTier
from omniagentos.routing.learn import (
    read_trace_hierarchy,
    read_traces,
    recommend_start_tier,
    task_class_hierarchy,
    wilson_lower_bound,
)

TIER0 = CascadeTier(name="tier0", adapter="cli-codex", cost_rank=5.0)
TIER1 = CascadeTier(name="tier1", adapter="cli-claude", cost_rank=6.0)
TIER2 = CascadeTier(name="tier2", adapter="cli-claude", cost_rank=7.0)


def _rows(tier_name: str, *, n: int, wins: int) -> list[dict]:
    assert 0 <= wins <= n
    return [{"tier_name": tier_name, "verified": i < wins} for i in range(n)]


# ---------------------------------------------------------------------------
# wilson_lower_bound
# ---------------------------------------------------------------------------


def test_wilson_lower_bound_zero_samples_is_zero() -> None:
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_lower_bound_known_values() -> None:
    # Hand-verified against the closed-form Wilson score formula.
    assert wilson_lower_bound(10, 10) == pytest.approx(0.7225, abs=1e-3)
    assert wilson_lower_bound(8, 10) == pytest.approx(0.4901, abs=1e-3)
    assert wilson_lower_bound(80, 100) == pytest.approx(0.7113, abs=1e-3)


def test_wilson_lower_bound_more_samples_same_rate_is_more_confident() -> None:
    # Same 80% raw rate, but 100 samples vs 10: more evidence -> a tighter
    # (higher) lower bound, never a looser one.
    assert wilson_lower_bound(80, 100) > wilson_lower_bound(8, 10)


# ---------------------------------------------------------------------------
# read_traces
# ---------------------------------------------------------------------------


def test_read_traces_filters_by_class_and_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    lines = [
        json.dumps({"task_class": "a", "tier_name": "tier0", "verified": True}),
        json.dumps({"task_class": "b", "tier_name": "tier0", "verified": False}),
        "not-json-at-all",
        "",
        json.dumps({"task_class": "a", "tier_name": "tier1", "verified": True}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = read_traces(str(path), "a")
    assert len(rows) == 2
    assert {row["tier_name"] for row in rows} == {"tier0", "tier1"}
    assert all(row["task_class"] == "a" for row in rows)


def test_read_traces_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert read_traces(str(tmp_path / "does-not-exist.jsonl"), "any-class") == []


def test_read_traces_respects_window(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    rows = [{"task_class": "a", "tier_name": "tier0", "verified": True, "n": i} for i in range(10)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    windowed = read_traces(str(path), "a", window=3)
    assert len(windowed) == 3
    # Keeps the LAST `window` rows (most recent), not the first.
    assert [row["n"] for row in windowed] == [7, 8, 9]


def test_orchestrator_trace_hierarchy_is_fine_lane_global(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    rows = [
        {"task_class": "orch:superfast:simple", "tier_name": "tier0"},
        {"task_class": "orch:superfast:complex", "tier_name": "tier0"},
        {"task_class": "orch:fusion:complex", "tier_name": "tier1"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    assert task_class_hierarchy("orch:superfast:simple") == (
        "orch:superfast:simple",
        "orch:superfast",
        "orch",
    )
    fine, lane, global_rows = read_trace_hierarchy(str(path), "orch:superfast:simple")
    assert [row["task_class"] for row in fine] == ["orch:superfast:simple"]
    assert [row["task_class"] for row in lane] == [
        "orch:superfast:simple",
        "orch:superfast:complex",
    ]
    assert len(global_rows) == 3


# ---------------------------------------------------------------------------
# recommend_start_tier
# ---------------------------------------------------------------------------


def test_recommend_start_tier_high_cheap_win_rate_stays_at_zero() -> None:
    traces = _rows("tier0", n=20, wins=18)  # 90% raw, Wilson lower ~0.70
    assert recommend_start_tier(traces, [TIER0, TIER1]) == 0


def test_recommend_start_tier_low_cheap_high_mid_recommends_mid() -> None:
    traces = _rows("tier0", n=20, wins=2) + _rows("tier1", n=30, wins=27)
    # tier0 Wilson lower bound is nowhere near 0.6; tier1's (90% raw over 30
    # samples) clears it comfortably once min_samples is met.
    assert recommend_start_tier(traces, [TIER0, TIER1]) == 1


def test_recommend_start_tier_below_min_samples_defaults_to_zero() -> None:
    # Only 3 total observations for the class -- well below min_samples=5 --
    # even though every single one of them was a win.
    traces = _rows("tier0", n=3, wins=3)
    assert recommend_start_tier(traces, [TIER0, TIER1]) == 0


def test_recommend_start_tier_empty_ladder_is_zero() -> None:
    assert recommend_start_tier([{"tier_name": "x", "verified": True}], []) == 0


def test_recommend_start_tier_expected_cost_fallback_skips_to_reliable_tier() -> None:
    # Nothing clears target_win_rate=0.6 (verified below via the Wilson
    # bounds), so recommend_start_tier falls back to minimizing expected
    # chained cost. tier0 essentially never wins (0/20) yet still costs
    # almost as much as the other tiers, so the expected cost of starting at
    # tier0 (pay its cost AND still likely have to pay for what's next) is
    # higher than skipping straight to the most reliable, final tier.
    traces = (
        _rows("tier0", n=20, wins=0) + _rows("tier1", n=15, wins=9) + _rows("tier2", n=5, wins=4)
    )
    ladder = [TIER0, TIER1, TIER2]

    # Sanity: confirm none of the tiers actually clears the target so this
    # test is exercising the fallback branch, not the Wilson-bound branch.
    for tier_name, n, wins in (("tier0", 20, 0), ("tier1", 15, 9), ("tier2", 5, 4)):
        assert wilson_lower_bound(wins, n) < 0.6, tier_name

    assert recommend_start_tier(traces, ladder) == 2
