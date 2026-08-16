"""Deterministic North Star metric calculations.

The certification runner supplies timestamps and persisted cost records.  This
module deliberately contains no database access so the C-17/C-31/C-39/C-41
calculations stay reproducible from a frozen receipt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import fsum

PHASES: tuple[str, ...] = (
    "accepted",
    "planned",
    "first_useful_work",
    "lanes_complete",
    "integrated",
    "gated",
    "reviewed",
    "completed",
)


@dataclass(frozen=True)
class PhaseLatency:
    """C-17 latency shares reconstructed from monotonic event anchors."""

    total_seconds: float
    phase_seconds: Mapping[str, float]

    @property
    def shares(self) -> dict[str, float]:
        if self.total_seconds == 0:
            return {phase: 0.0 for phase in self.phase_seconds}
        return {
            phase: duration / self.total_seconds for phase, duration in self.phase_seconds.items()
        }


def decompose_phase_latency(anchors: Mapping[str, float]) -> PhaseLatency:
    """Return each adjacent phase duration, rejecting absent or reversed anchors."""
    missing = [phase for phase in PHASES if phase not in anchors]
    if missing:
        raise ValueError(f"missing phase anchors: {', '.join(missing)}")
    values = [float(anchors[phase]) for phase in PHASES]
    if any(later < earlier for earlier, later in zip(values[:-1], values[1:], strict=True)):
        raise ValueError("phase anchors must be monotonic")
    durations = {
        f"{start}_to_{end}": end_at - start_at
        for start, end, start_at, end_at in zip(
            PHASES[:-1], PHASES[1:], values[:-1], values[1:], strict=True
        )
    }
    return PhaseLatency(total_seconds=values[-1] - values[0], phase_seconds=durations)


@dataclass(frozen=True)
class IdleBreakdown:
    """C-41 idle-time attribution and critical-path measurement."""

    makespan_seconds: float
    critical_path_lower_bound_seconds: float
    critical_path_ratio: float
    idle_seconds: Mapping[str, float]

    @property
    def unknown_idle_ratio(self) -> float:
        total_idle = fsum(self.idle_seconds.values())
        return 0.0 if total_idle == 0 else self.idle_seconds.get("unknown", 0.0) / total_idle


def decompose_idle_time(
    *,
    makespan_seconds: float,
    critical_path_lower_bound_seconds: float,
    idle_intervals: Mapping[str, Sequence[float]],
) -> IdleBreakdown:
    """Classify C-41 idle intervals without silently dropping unknown time."""
    if makespan_seconds < 0 or critical_path_lower_bound_seconds <= 0:
        raise ValueError("makespan must be non-negative and critical path must be positive")
    allowed = {"queue", "dependency", "gate", "permission", "unknown"}
    unexpected = sorted(set(idle_intervals) - allowed)
    if unexpected:
        raise ValueError(f"unknown idle classifications: {', '.join(unexpected)}")
    idle = {kind: fsum(float(value) for value in idle_intervals.get(kind, ())) for kind in allowed}
    return IdleBreakdown(
        makespan_seconds=makespan_seconds,
        critical_path_lower_bound_seconds=critical_path_lower_bound_seconds,
        critical_path_ratio=makespan_seconds / critical_path_lower_bound_seconds,
        idle_seconds=idle,
    )


@dataclass(frozen=True)
class FairnessResult:
    jain_index: float
    min_progress: float
    passes: bool


def jain_fairness(
    progress_by_project: Mapping[str, float], *, min_progress_floor: float
) -> FairnessResult:
    """Apply the ratified Jain >= .8 and per-project progress-floor rule."""
    values = [float(value) for value in progress_by_project.values()]
    if not values:
        raise ValueError("at least one project progress value is required")
    if any(value < 0 for value in values):
        raise ValueError("progress cannot be negative")
    denominator = len(values) * sum(value * value for value in values)
    index = 0.0 if denominator == 0 else sum(values) ** 2 / denominator
    minimum = min(values)
    return FairnessResult(index, minimum, index >= 0.8 and minimum >= min_progress_floor)


@dataclass(frozen=True)
class ConfigurationCost:
    """C-31 comparison key across formation selections, attempts, and sessions."""

    config_id: str
    formation_selections: int
    swarm_attempts: int
    session_cost_usd: float
    completion_seconds: float
    quality_score: float

    def __post_init__(self) -> None:
        if not self.config_id:
            raise ValueError("config_id is required")
        if (
            min(
                self.formation_selections,
                self.swarm_attempts,
                self.session_cost_usd,
                self.completion_seconds,
            )
            < 0
        ):
            raise ValueError("configuration costs cannot be negative")


def dominates(left: ConfigurationCost, right: ConfigurationCost) -> bool:
    """True when left is no worse on every cost axis and quality, and better on one."""
    no_worse = (
        left.formation_selections <= right.formation_selections
        and left.swarm_attempts <= right.swarm_attempts
        and left.session_cost_usd <= right.session_cost_usd
        and left.completion_seconds <= right.completion_seconds
        and left.quality_score >= right.quality_score
    )
    strictly_better = (
        left.formation_selections < right.formation_selections
        or left.swarm_attempts < right.swarm_attempts
        or left.session_cost_usd < right.session_cost_usd
        or left.completion_seconds < right.completion_seconds
        or left.quality_score > right.quality_score
    )
    return no_worse and strictly_better


def dominated_configurations(configurations: Sequence[ConfigurationCost]) -> set[str]:
    """Return C-31 configurations outside the Pareto frontier."""
    ids = [config.config_id for config in configurations]
    if len(ids) != len(set(ids)):
        raise ValueError("configuration ids must be unique")
    return {
        candidate.config_id
        for candidate in configurations
        if any(
            other.config_id != candidate.config_id and dominates(other, candidate)
            for other in configurations
        )
    }
