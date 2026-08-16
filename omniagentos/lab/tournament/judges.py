"""Fail-closed tournament judging contracts.

Live tournaments may use a task-declared deterministic metric, or a blinded
panel with independently identified model lineages and explicit validity
evidence. Free-form output text is never treated as a quality signal.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Protocol

MIN_PANEL_AGREEMENT = 0.60
MIN_JUDGE_VALIDITY = 0.80


class TournamentJudge(Protocol):
    """One independently identified member of a blind tournament panel."""

    judge_lineage: str

    def judge_blind(
        self,
        *,
        output_a: dict[str, Any],
        output_b: dict[str, Any],
        arena_task: dict[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class JudgedScores:
    score_a: float
    score_b: float
    notes: str


def deterministic_task_scores(
    output_a: Mapping[str, Any],
    output_b: Mapping[str, Any],
    arena_task: Mapping[str, Any],
) -> JudgedScores | None:
    """Read only a metric explicitly declared by the task.

    Returning ``None`` means the task did not declare a deterministic metric;
    missing or malformed values on a declared metric are integrity failures.
    """

    metric = arena_task.get("deterministic_metric")
    if metric is None:
        return None
    if not isinstance(metric, str) or not metric.strip():
        raise ValueError("arena_task.deterministic_metric must be a non-empty string")
    name = metric.strip()
    score_a = _declared_metric(output_a, name)
    score_b = _declared_metric(output_b, name)
    return JudgedScores(
        score_a=score_a,
        score_b=score_b,
        notes=f"deterministic task metric {name}: {score_a:.3f} vs {score_b:.3f}",
    )


def blinded_panel_scores(
    panel: Sequence[TournamentJudge],
    output_a: dict[str, Any],
    output_b: dict[str, Any],
    arena_task: Mapping[str, Any],
) -> JudgedScores:
    """Aggregate a valid, agreeing panel containing at least two lineages."""

    if len(panel) < 2:
        raise ValueError(
            "live tournament requires a deterministic task metric or at least two blind judges"
        )
    safe_task = {
        str(key): value
        for key, value in arena_task.items()
        if key not in {"mock_outputs", "config_ids", "configuration_ids"}
    }
    lineages: set[str] = set()
    scores_a: list[float] = []
    scores_b: list[float] = []
    preferences: list[int] = []
    validities: list[float] = []

    for judge in panel:
        lineage = str(getattr(judge, "judge_lineage", "")).strip()
        if not lineage:
            raise ValueError("every tournament panel judge must declare judge_lineage")
        if lineage in lineages:
            raise ValueError(f"tournament panel repeats judge lineage {lineage!r}")
        lineages.add(lineage)
        result = judge.judge_blind(
            output_a=output_a,
            output_b=output_b,
            arena_task=dict(safe_task),
        )
        if not isinstance(result, Mapping):
            raise ValueError(f"judge {lineage!r} returned no structured evidence")
        score_a = _finite_number(result.get("score_a"), f"{lineage}.score_a")
        score_b = _finite_number(result.get("score_b"), f"{lineage}.score_b")
        validity = _finite_number(result.get("validity"), f"{lineage}.validity")
        if not 0.0 <= validity <= 1.0:
            raise ValueError(f"judge {lineage!r} validity must be between 0 and 1")
        scores_a.append(score_a)
        scores_b.append(score_b)
        validities.append(validity)
        preferences.append(1 if score_a > score_b else -1 if score_a < score_b else 0)

    agreement = Counter(preferences).most_common(1)[0][1] / len(preferences)
    minimum_validity = min(validities)
    if minimum_validity < MIN_JUDGE_VALIDITY:
        raise ValueError(
            "tournament panel validity below floor: "
            f"{minimum_validity:.3f} < {MIN_JUDGE_VALIDITY:.3f}"
        )
    if agreement < MIN_PANEL_AGREEMENT:
        raise ValueError(
            f"tournament panel agreement below floor: {agreement:.3f} < "
            f"{MIN_PANEL_AGREEMENT:.3f}"
        )

    score_a = fmean(scores_a)
    score_b = fmean(scores_b)
    return JudgedScores(
        score_a=score_a,
        score_b=score_b,
        notes=(
            f"blind cross-lineage panel: lineages={len(lineages)}; "
            f"agreement={agreement:.3f}; validity_min={minimum_validity:.3f}; "
            f"scores={score_a:.3f} vs {score_b:.3f}"
        ),
    )


def _declared_metric(payload: Mapping[str, Any], name: str) -> float:
    value: Any = payload.get("output", payload)
    if not isinstance(value, Mapping):
        raise ValueError(f"tournament output has no structured metric {name!r}")
    structured = value.get("json")
    metrics = value.get("metrics")
    if isinstance(metrics, Mapping) and name in metrics:
        candidate = metrics.get(name)
    elif name in value:
        candidate = value.get(name)
    elif isinstance(structured, Mapping):
        structured_metrics = structured.get("metrics")
        candidate = (
            structured_metrics.get(name)
            if isinstance(structured_metrics, Mapping) and name in structured_metrics
            else structured.get(name)
        )
    else:
        candidate = None
    return _finite_number(candidate, f"deterministic metric {name!r}")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


__all__ = [
    "JudgedScores",
    "MIN_JUDGE_VALIDITY",
    "MIN_PANEL_AGREEMENT",
    "TournamentJudge",
    "blinded_panel_scores",
    "deterministic_task_scores",
]
