"""Tier + model selection — the token economy the knobs DRIVE.

Two user-controllable knobs decide how a run spends reasoning budget:

* ``priority`` (``fast`` | ``balanced`` | ``quality``, default ``balanced``) — the
  master knob. It sets the planner effort, the executor tier, and whether the
  quality gate runs at all.
* ``pins`` (:class:`~omniagentos.orchestrator.contracts.OverridePins`) — optional
  operator overrides honoured OVER the mode defaults (fusion ``--pin`` semantics: a
  valid pin wins; an impossible pin is dropped with a surfaced reason).

Within ``balanced``, the executor tier still follows the cheap complexity estimate
(simple → a cheap single adapter; non-trivial → Fable-led fusion), exactly as the
core brief specifies. ``fast`` forces the cheap lane; ``quality`` forces fusion at
the ultrabuild tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omniagentos.orchestrator.contracts import (
    _VALID_EFFORTS,
    _VALID_LANES,
    ExecutorTier,
    FusionLane,
    OverridePins,
    Priority,
    ResolvedExecution,
)

if TYPE_CHECKING:
    from omniagentos.intake.fable import Effort
    from omniagentos.intake.planner import Complexity

# Per-priority planner effort floor. ``fast`` bumps to high only for a complex goal
# (the "medium/high" band); ``quality`` always earns max — decomposing well is where
# the reasoning budget pays off.
_QUALITY_GATE: dict[Priority, bool] = {"fast": False, "balanced": True, "quality": True}
_MAX_RETRIES: dict[Priority, int] = {"fast": 0, "balanced": 1, "quality": 2}


def _planner_effort_for(priority: Priority, complexity: Complexity) -> Effort:
    if priority == "quality":
        return "max"
    if priority == "fast":
        return "high" if complexity == "complex" else "medium"
    return "high"  # balanced


def _executor_tier_for(priority: Priority, complexity: Complexity) -> ExecutorTier:
    if priority == "fast":
        return ExecutorTier.CHEAP
    if priority == "quality":
        return ExecutorTier.FUSION_ULTRA
    # balanced: cheap for a simple bounded task, Fable-led fusion for anything bigger.
    return ExecutorTier.CHEAP if complexity == "simple" else ExecutorTier.FUSION


def _default_lane(tier: ExecutorTier) -> FusionLane:
    if tier is ExecutorTier.CHEAP:
        return "superfast"
    if tier is ExecutorTier.FUSION_ULTRA:
        return "ultrabuild"
    return "fusion"


def resolve_execution(
    priority: Priority,
    complexity: Complexity,
    pins: OverridePins,
) -> ResolvedExecution:
    """Resolve knobs + complexity into a concrete, auditable execution plan.

    Pins win over the mode defaults; an impossible pin (an unknown effort/lane) is
    dropped and its reason recorded in ``pin_warnings`` so the caller can surface it.
    """
    warnings: list[str] = []

    # --- planner level: a pinned effort is the "explicit level request" that
    # overrides the cheap complexity estimate; otherwise the priority mode decides.
    planner_effort: Effort = _planner_effort_for(priority, complexity)
    if pins.planner_effort is not None:
        pinned = str(pins.planner_effort).strip().lower()
        if pinned in _VALID_EFFORTS:
            planner_effort = pinned  # type: ignore[assignment]
        else:
            warnings.append(
                f"planner_effort pin {pins.planner_effort!r} is not one of "
                f"{sorted(_VALID_EFFORTS)}; kept mode default {planner_effort!r}"
            )
    planner_model = pins.planner_model  # a model pin always wins (free-form).

    # --- executor tier + lane + model.
    tier = _executor_tier_for(priority, complexity)
    lane: FusionLane = _default_lane(tier)
    if pins.executor_effort is not None:
        pinned_lane = str(pins.executor_effort).strip().lower()
        if pinned_lane in _VALID_LANES:
            lane = pinned_lane  # type: ignore[assignment]
            # A lane pin can move a cheap task onto the fusion machinery and back.
            if pinned_lane == "superfast":
                tier = ExecutorTier.CHEAP
            elif pinned_lane == "ultrabuild":
                tier = ExecutorTier.FUSION_ULTRA
            else:
                tier = ExecutorTier.FUSION
        else:
            warnings.append(
                f"executor_effort pin {pins.executor_effort!r} is not one of "
                f"{sorted(_VALID_LANES)}; kept mode default {lane!r}"
            )
    executor_model = pins.executor_model_or_lineage  # a lineage/model pin always wins.

    return ResolvedExecution(
        priority=priority,
        complexity=complexity,
        planner_effort=planner_effort,
        planner_model=planner_model,
        executor_tier=tier,
        executor_model=executor_model,
        executor_lane=lane,
        run_quality_gate=_QUALITY_GATE[priority],
        max_retries=_MAX_RETRIES[priority],
        pin_warnings=warnings,
    )


__all__ = ["resolve_execution"]
