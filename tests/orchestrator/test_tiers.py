"""Tier + model selection — the knobs DRIVE it.

``priority`` (fast|balanced|quality) and ``pins`` decide the planner effort, the
executor tier, and whether the quality gate runs; a valid pin WINS over the mode
default and an impossible pin is dropped with a surfaced reason.
"""

from __future__ import annotations

from omniagentos.orchestrator.contracts import ExecutorTier, OverridePins
from omniagentos.orchestrator.tiers import resolve_execution


def test_balanced_tiers_by_complexity() -> None:
    # The core brief: simple -> cheap single adapter; non-trivial -> Fable-led fusion.
    simple = resolve_execution("balanced", "simple", OverridePins())
    assert simple.executor_tier is ExecutorTier.CHEAP
    assert simple.run_quality_gate is True
    assert simple.max_retries == 1
    assert simple.planner_effort == "high"

    complex_ = resolve_execution("balanced", "complex", OverridePins())
    assert complex_.executor_tier is ExecutorTier.FUSION
    assert complex_.planner_effort == "high"


def test_fast_forces_cheap_and_skips_gate() -> None:
    r = resolve_execution("fast", "complex", OverridePins())
    assert r.executor_tier is ExecutorTier.CHEAP
    assert r.run_quality_gate is False
    assert r.max_retries == 0
    # fast is medium/high: high only for a complex goal.
    assert r.planner_effort == "high"
    assert resolve_execution("fast", "simple", OverridePins()).planner_effort == "medium"


def test_quality_forces_fusion_ultra_and_iterates() -> None:
    r = resolve_execution("quality", "simple", OverridePins())
    assert r.executor_tier is ExecutorTier.FUSION_ULTRA
    assert r.run_quality_gate is True
    assert r.max_retries == 2
    assert r.planner_effort == "max"


def test_planner_model_pin_overrides_mode_default() -> None:
    r = resolve_execution("balanced", "simple", OverridePins(planner_model="opus"))
    assert r.planner_model == "opus"
    assert r.pin_warnings == []


def test_explicit_planner_level_overrides_complexity_estimate() -> None:
    # A "fast" simple goal defaults to medium; an explicit level pin wins.
    r = resolve_execution("fast", "simple", OverridePins(planner_effort="max"))
    assert r.planner_effort == "max"
    assert r.pin_warnings == []


def test_executor_lane_pin_moves_tier() -> None:
    r = resolve_execution("balanced", "simple", OverridePins(executor_effort="ultrabuild"))
    assert r.executor_tier is ExecutorTier.FUSION_ULTRA
    assert r.executor_lane == "ultrabuild"

    r2 = resolve_execution("quality", "complex", OverridePins(executor_effort="superfast"))
    assert r2.executor_tier is ExecutorTier.CHEAP
    assert r2.executor_lane == "superfast"


def test_impossible_pin_is_dropped_with_a_surfaced_reason() -> None:
    r = resolve_execution("balanced", "simple", OverridePins(planner_effort="turbo"))
    assert r.planner_effort == "high"  # kept the mode default
    assert r.pin_warnings and "turbo" in r.pin_warnings[0]

    r2 = resolve_execution("balanced", "simple", OverridePins(executor_effort="warp"))
    assert r2.executor_lane == "superfast"  # cheap default lane, unchanged
    assert r2.pin_warnings and "warp" in r2.pin_warnings[0]


def test_pins_coerce_from_mapping() -> None:
    pins = OverridePins.coerce(
        {"planner_model": "opus", "planner_effort": "max", "executor_effort": "fusion"}
    )
    assert pins.planner_model == "opus"
    assert pins.planner_effort == "max"
    assert pins.executor_effort == "fusion"
    assert OverridePins.coerce(None).planner_model is None
