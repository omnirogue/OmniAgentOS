"""Pure aggregation math for building a ``Scorecard`` from already-persisted
``EvalResult`` rows. No I/O; fully unit-testable in isolation.

Deliberately reads real ``MetricSpec`` role/direction/threshold metadata
(instead of hard-coding metric names) so ``Scorecard.safety_regression`` /
``complexity_delta`` / ``utility`` reflect the suite's own declared contract.
Operates ONLY on ``metrics``/``per_case`` numbers already produced by
``ProtectedGrader`` — there is no ``expected`` anywhere in this module.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from omniagentos.lab import stats as lab_stats
from omniagentos.lab.contracts import EvalResult, MetricSpec


def eval_result_from_row(row: dict[str, Any]) -> EvalResult:
    """Reconstruct an ``EvalResult`` from a ``LabStore.eval_results`` row
    (parses the ``*_json`` columns). The ``eval_results`` table has no
    ``expected`` column (HD-001) — there is nothing sensitive to parse here."""
    return EvalResult(
        id=str(row["id"]),
        experiment_id=str(row["experiment_id"]),
        arm=str(row["arm"]),
        suite_id=str(row["suite_id"]),
        suite_version=int(row["suite_version"]),
        split=row["split"],
        metrics=json.loads(row["metrics_json"]),
        per_case=json.loads(row["per_case_json"]),
        replicate=int(row["replicate"]),
        deterministic_passed=bool(row["deterministic_passed"]),
        created_at=str(row["created_at"]),
    )


def parse_metric_specs(suite_row: dict[str, Any] | None) -> list[MetricSpec]:
    if suite_row is None:
        return []
    return [MetricSpec(**item) for item in json.loads(suite_row["metrics_json"])]


def primary_metric_name(specs: list[MetricSpec], fallback: str = "") -> str:
    for spec in specs:
        if spec.role == "primary":
            return spec.name
    return fallback


def _direction(specs: list[MetricSpec], name: str) -> str:
    for spec in specs:
        if spec.name == name:
            return spec.direction
    return "maximize"


def aggregate_replicates(results: list[EvalResult]) -> dict[str, float]:
    """Mean of every metric key across replicate ``EvalResult`` rows for one
    arm+split (equal-weighted; a single replicate is the common case)."""
    keys: set[str] = set()
    for result in results:
        keys.update(result.metrics)
    return {
        key: statistics.fmean(result.metrics.get(key, 0.0) for result in results)
        for key in sorted(keys)
    }


def signed_delta(
    name: str,
    specs: list[MetricSpec],
    champion: dict[str, float],
    challenger: dict[str, float],
) -> float | None:
    """``challenger - champion`` for ``name``, sign-flipped when the metric's
    ``direction`` is ``"minimize"`` so a POSITIVE delta always means "the
    challenger is better". ``None`` if the metric is missing from either arm."""
    if not name or name not in champion or name not in challenger:
        return None
    raw = challenger[name] - champion[name]
    return -raw if _direction(specs, name) == "minimize" else raw


def safety_regression(
    specs: list[MetricSpec], champion: dict[str, float], challenger: dict[str, float]
) -> bool:
    """True if any guardrail/hard_constraint metric got worse: a threshold
    breach if the metric declares one, else any regression vs the champion
    baseline (Section 11.7 invariant #4 — zero hard-constraint regression)."""
    for spec in specs:
        if spec.role not in ("guardrail", "hard_constraint") or spec.name not in challenger:
            continue
        challenger_value = challenger[spec.name]
        minimize = spec.direction == "minimize"

        breaches_threshold = spec.threshold is not None and (
            challenger_value > spec.threshold if minimize else challenger_value < spec.threshold
        )
        champion_value = champion.get(spec.name)
        worse_than_champion = champion_value is not None and (
            challenger_value > champion_value if minimize else challenger_value < champion_value
        )
        if breaches_threshold or (spec.threshold is None and worse_than_champion):
            return True
    return False


def regularizer_delta(
    specs: list[MetricSpec], champion: dict[str, float], challenger: dict[str, float]
) -> float | None:
    """``challenger - champion`` for the (first) ``role="regularizer"``
    metric, clamped to >= 0 — only GROWTH is penalized; a simpler challenger
    is never punished. A suite SHOULD declare at most one regularizer metric
    (e.g. named ``"complexity"``); only the first is used if more exist."""
    for spec in specs:
        if spec.role == "regularizer" and spec.name in champion and spec.name in challenger:
            return max(0.0, challenger[spec.name] - champion[spec.name])
    return None


def efficiency_penalty(
    specs: list[MetricSpec], champion: dict[str, float], challenger: dict[str, float]
) -> float:
    """Sum, over every ``role="efficiency"`` metric, of how much WORSE
    (direction-aware) the challenger is than the champion, clamped to >= 0
    per metric (an efficiency IMPROVEMENT never offsets a regression
    elsewhere — Section 11.6's utility is a regularizer, not a trade fund)."""
    total = 0.0
    for spec in specs:
        if spec.role != "efficiency" or spec.name not in champion or spec.name not in challenger:
            continue
        delta = challenger[spec.name] - champion[spec.name]
        total += max(0.0, delta if spec.direction == "minimize" else -delta)
    return total


def latest(results: list[EvalResult]) -> EvalResult:
    """The most recently created replicate — ``audit_metric_jump`` wants ONE
    representative ``EvalResult`` per arm, not an aggregate."""
    return max(results, key=lambda result: (result.created_at, result.replicate))


def confidence_interval_estimate(
    name: str, results: list[EvalResult]
) -> lab_stats.IntervalEstimate:
    """Return interval bounds plus explicit replicate-stability metadata."""
    values = [result.metrics[name] for result in results if name in result.metrics]
    return lab_stats.mean_confidence_interval(values, confidence=0.95)


def confidence_interval_95(name: str, results: list[EvalResult]) -> list[float] | None:
    """Return JSON-compatible 95% bounds for metric ``name``.

    The statistical calculation delegates to :mod:`omniagentos.lab.stats`.
    Fewer than two observations are explicitly unstable in the underlying
    :class:`~omniagentos.lab.stats.IntervalEstimate` and serialize as ``None``
    rather than a misleading zero-width interval.
    """
    return confidence_interval_estimate(name, results).as_list()
