"""Observatory pulse API — serves Observatory /pulse tiles + cockpit sparklines.

One endpoint:

  ``GET /api/pulse/series?metric=skills.total&days=30``

Returns the pinned shape from docs/ui-redesign/FINAL-PLAN.md section B:

  ``{ "metric": str, "points": [{ "date": "YYYY-MM-DD", "value": number }] }``

Seed-on-empty: the very first dashboard load against a freshly migrated DB
would otherwise render every chart blank. On an empty series, this
handler runs the aggregator inline to populate today's row — every
subsequent visit is a straight ``SELECT``.

Known metric names (mirrored from :data:`omniagentos.pulse.METRICS`):
``skills.total``, ``skills.versions``, ``improvements.applied``,
``loops.fires``, ``loops.acceptance``, ``memory.facts``, ``reliability.score``.
Unknown metrics return 422 with the canonical list in the error detail.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.db.store import SqliteStore
from omniagentos.pulse import METRICS, PulseStore, snapshot

router = APIRouter(prefix="/api/pulse", tags=["pulse"])


def _pulse(store: StoreDep) -> PulseStore:
    return PulseStore(cast(SqliteStore, store))


class Point(BaseModel):
    date: str
    value: float


class SeriesResponse(BaseModel):
    metric: str
    points: list[Point]
    error: str | None = None


@router.get("/series")
def get_series(
    store: StoreDep,
    metric: str = Query(..., description="Dotted metric name, e.g. skills.total"),
    days: int = Query(default=30, ge=1, le=365, description="Look-back window in days"),
) -> dict[str, Any]:
    """Time-series for one metric, oldest-first, capped at ``days`` points."""
    if metric not in METRICS:
        fail(
            422,
            "validation",
            f"unknown metric '{metric}'",
            {"metric": metric, "known": list(METRICS)},
        )

    pulse = _pulse(store)
    metric_error: str | None = None

    # Seed-on-empty: first visit to the Observatory must not render blank charts.
    # Run the aggregator once for today if the table has no rows at all; this is
    # cheaper than per-metric probing and populates every tile in one shot.
    if not pulse.has_any():
        try:
            from omniagentos.pulse.aggregator import compute_metrics
            values, errors = compute_metrics(cast(SqliteStore, store))
            # Check if this specific metric failed during seeding
            metric_error = errors.get(metric)
            # Persist values using snapshot (pass pre-computed values to avoid double compute)
            snapshot(cast(SqliteStore, store), values=values)
        except Exception as exc:  # noqa: BLE001 — a seeding failure must not block the API
            # Capture the error for this metric
            metric_error = str(exc)

    points = pulse.series(metric=metric, days=days)
    result: dict[str, Any] = {
        "metric": metric,
        "points": [{"date": p["date"], "value": p["value"]} for p in points],
    }
    # Surface per-metric error if one occurred during seeding
    if metric_error:
        result["error"] = metric_error
    return result


@router.get("/metrics")
def list_metrics(store: StoreDep) -> dict[str, Any]:
    """Enumerate metric names that have at least one point in ``pulse_series``."""
    return {"metrics": _pulse(store).metrics()}


__all__ = ["router"]
