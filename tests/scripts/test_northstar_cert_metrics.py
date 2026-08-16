from __future__ import annotations

import pytest

from scripts.northstar_cert.metrics import (
    ConfigurationCost,
    decompose_idle_time,
    decompose_phase_latency,
    dominated_configurations,
    jain_fairness,
)


def test_c17_phase_latency_decomposition_is_complete_and_monotonic() -> None:
    result = decompose_phase_latency(
        {
            "accepted": 0,
            "planned": 2,
            "first_useful_work": 5,
            "lanes_complete": 9,
            "integrated": 11,
            "gated": 12,
            "reviewed": 14,
            "completed": 15,
        }
    )
    assert result.total_seconds == 15
    assert sum(result.phase_seconds.values()) == result.total_seconds
    assert result.phase_seconds["planned_to_first_useful_work"] == 3
    assert sum(result.shares.values()) == 1


def test_c17_rejects_missing_or_reversed_anchors() -> None:
    with pytest.raises(ValueError, match="missing phase anchors"):
        decompose_phase_latency({"accepted": 0})
    anchors = {
        phase: index
        for index, phase in enumerate(
            (
                "accepted",
                "planned",
                "first_useful_work",
                "lanes_complete",
                "integrated",
                "gated",
                "reviewed",
                "completed",
            )
        )
    }
    anchors["gated"] = 1
    with pytest.raises(ValueError, match="monotonic"):
        decompose_phase_latency(anchors)


def test_c41_idle_and_critical_path_ratio_reconcile() -> None:
    result = decompose_idle_time(
        makespan_seconds=15,
        critical_path_lower_bound_seconds=10,
        idle_intervals={
            "queue": [2],
            "dependency": [1],
            "gate": [1],
            "permission": [],
            "unknown": [0.5],
        },
    )
    assert result.critical_path_ratio == 1.5
    assert result.unknown_idle_ratio == pytest.approx(0.5 / 4.5)
    with pytest.raises(ValueError, match="unknown idle"):
        decompose_idle_time(
            makespan_seconds=1, critical_path_lower_bound_seconds=1, idle_intervals={"mystery": [1]}
        )


def test_c19_and_c39_jain_fairness_requires_floor_as_well_as_index() -> None:
    fair = jain_fairness({"a": 10, "b": 9, "c": 8}, min_progress_floor=8)
    assert fair.jain_index >= 0.8 and fair.passes
    floor_breach = jain_fairness({"a": 10, "b": 10, "c": 5}, min_progress_floor=6)
    assert floor_breach.jain_index >= 0.8
    assert not floor_breach.passes


def test_c31_pareto_frontier_uses_all_persisted_cost_axes() -> None:
    frontier = ConfigurationCost("frontier", 1, 2, 0.10, 8, 0.9)
    dominated = ConfigurationCost("dominated", 2, 3, 0.20, 9, 0.8)
    tradeoff = ConfigurationCost("tradeoff", 1, 1, 0.05, 12, 0.95)
    assert dominated_configurations([frontier, dominated, tradeoff]) == {"dominated"}
    with pytest.raises(ValueError, match="unique"):
        dominated_configurations([frontier, frontier])
