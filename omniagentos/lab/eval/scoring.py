"""Deterministic (non-model) case scoring.

No I/O; pure functions only, fully unit-testable in isolation from the
protected store. This module never sees the protected SQLite file — it is
handed an ``expected`` dict and an ``output`` dict by the caller
(``ProtectedGrader.score_outputs``, the only place ``expected`` is read from
disk) and returns plain floats. Runs OUTSIDE the optimization target
(blueprint Section 11.7): deterministic validators before any model judge.

Expected-value convention (owned by whoever seeds ``ProtectedGrader.put_expected``)::

    {
        "value": <any>,              # required: canonical expected value
        "field": "text" | "json",    # which output field to compare (default "text")
        "match": "exact" | "contains" | "set_equal" | "numeric_tol",  # default "exact"
        "tol": <float>,              # required when match == "numeric_tol"
    }

Output convention matches L04x-executor's ``run_surface_over_cases`` return
shape (contracts/lab-interfaces.md §L04x-executor): ``{"text": str, "json": Any}``.
"""

from __future__ import annotations

from typing import Any

DEFAULT_FIELD = "text"
DEFAULT_MATCH = "exact"


def _as_comparable_set(value: Any) -> frozenset[str]:
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(str(item) for item in value)
    return frozenset({str(value)})


def score_case(expected: dict[str, Any], output: dict[str, Any]) -> dict[str, float]:
    """Deterministically score one case.

    Returns ``{"correct": 0.0|1.0, "score": float in [0, 1]}`` — ``correct``
    is a hard pass/fail (drives the "accuracy" aggregate metric); ``score``
    allows partial credit (currently only ``numeric_tol`` uses it; every
    other match mode sets ``score == correct``).
    """
    if "value" not in expected:
        raise ValueError("expected case is missing the required 'value' key")
    field = expected.get("field", DEFAULT_FIELD)
    match = expected.get("match", DEFAULT_MATCH)
    target = expected["value"]
    actual = output.get(field)

    if match == "exact":
        correct = actual == target
        score = 1.0 if correct else 0.0
    elif match == "contains":
        haystack = "" if actual is None else str(actual)
        correct = str(target) in haystack
        score = 1.0 if correct else 0.0
    elif match == "set_equal":
        correct = _as_comparable_set(actual) == _as_comparable_set(target)
        score = 1.0 if correct else 0.0
    elif match == "numeric_tol":
        tol = float(expected.get("tol", 0.0))
        try:
            actual_number = float(actual)  # type: ignore[arg-type]
            target_number = float(target)
        except (TypeError, ValueError):
            correct, score = False, 0.0
        else:
            distance = abs(actual_number - target_number)
            correct = distance <= tol
            # Partial credit decays linearly to 0 by 4x tolerance so a near
            # miss is not scored identically to a wildly wrong answer.
            score = max(0.0, 1.0 - distance / (tol * 4)) if tol > 0 else (1.0 if correct else 0.0)
    else:
        raise ValueError(f"unknown match mode: {match!r}")

    return {"correct": 1.0 if correct else 0.0, "score": float(score)}


def aggregate_case_scores(per_case: dict[str, dict[str, float]]) -> dict[str, float]:
    """Equal-weighted mean of every metric key present across all cases.

    Weighting by ``EvalCase.weight`` is deliberately NOT done here:
    ``ProtectedGrader`` never sees case metadata, only ``{case_id: output}``
    and ``expected`` — a weighted aggregate is a downstream (L04-campaign)
    concern with access to the (candidate-safe) case records.
    """
    if not per_case:
        return {}
    keys: set[str] = set()
    for metrics in per_case.values():
        keys.update(metrics)
    count = len(per_case)
    return {
        key: sum(metrics.get(key, 0.0) for metrics in per_case.values()) / count
        for key in sorted(keys)
    }
