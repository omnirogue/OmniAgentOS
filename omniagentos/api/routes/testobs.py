"""Test Observatory HTTP surface — three token-gated reads over var/ test evidence.

  ``GET /api/testobs/overview``    — one card per category (northstar, memory,
                                     diagnostics, suite)
  ``GET /api/testobs/series``      — per-category time series, oldest-first
  ``GET /api/testobs/weakspots``   — ranked failing/unproven items, capped

Read-side only: no store dependency, no writes. A category whose source is
absent answers ``{"available": false, "reason": ...}`` rather than zeros, so
the dashboard can style UNKNOWN as unknown instead of as success.

``weakspots[].status`` is a closed vocabulary, worst-first as returned:
``FAIL``, ``ERR``, ``ABORT`` (the run never happened), ``MISS`` (declared paths
absent), ``EMPTY`` (ran, measured nothing), ``STALE`` (stopped running),
``VOID`` and ``NOT_EVALUABLE`` (northstar mechanics). It is exported as
``omniagentos.testobs.WEAKSPOT_STATUSES`` so the UI union has one source.

The same rule applies WITHIN a category, so several overview numbers are
``number | null`` and the UI must render null as "—", never as 0:
``suite.runs_7d``/``pass_rate_7d`` are null when no merge-gate receipt exists at
all (a lane-JUnit-only suite measured no gate runs; a store WITH history and an
empty window keeps an honest 0), and every northstar rate is null until it has
been measured.

GATED (SEC-005): every handler carries ``_authorized``. These reads name
failing check ids, failure messages and test node ids — internal work records
of the same class as ``/api/team/*``, not a public status page. The dashboard
reaches them through ``proxyRead`` (the ``testobs`` entry in
``AUTHORIZED_READ_PREFIXES``, ``dashboard/src/app/api/[...path]/route.ts``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from omniagentos.api.routes.control import _authorized, fail
from omniagentos.contracts import utc_now_iso
from omniagentos.testobs import CATEGORIES, OVERVIEW_DAYS, snapshot, weakspot_rank

router = APIRouter(prefix="/api/testobs", tags=["testobs"])

WEAKSPOT_LIMIT = 100


class Point(BaseModel):
    date: str
    value: float


class Series(BaseModel):
    metric: str
    label: str
    unit: str
    points: list[Point]


class SeriesResponse(BaseModel):
    category: str
    days: int
    available: bool
    reason: str | None = None
    series: list[Series] = []


class WeakSpot(BaseModel):
    category: str
    id: str
    label: str
    status: str
    detail: str | None = None
    gate: bool | None = None
    since: str | None = None


class SourceState(BaseModel):
    available: bool
    reason: str | None = None


class WeakSpotsResponse(BaseModel):
    generated_at: str
    sources: dict[str, SourceState]
    items: list[WeakSpot]


class OverviewResponse(BaseModel):
    generated_at: str
    northstar: dict[str, Any]
    memory: dict[str, Any]
    diagnostics: dict[str, Any]
    suite: dict[str, Any]


@router.get("/overview")
def get_overview(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """One summary card per category over the standard overview window."""
    del _
    result: dict[str, Any] = {"generated_at": utc_now_iso()}
    for category in CATEGORIES:
        result[category] = snapshot(category, OVERVIEW_DAYS)["overview"]
    return result


@router.get("/series")
def get_series(
    _: Annotated[None, Depends(_authorized)] = None,
    # Deliberately not required: a REQUIRED query param makes an unauthenticated
    # request without it answer 422 instead of 401, which reads as "not gated" to
    # tests/api/test_auth_posture.py and to the dashboard read-gate mirror. The
    # gate must be the first thing a caller meets, so the arity check moves into
    # the handler body.
    category: str = Query(default="", description="northstar | memory | diagnostics | suite"),
    days: int = Query(default=90, ge=1, le=365, description="Look-back window in days"),
) -> dict[str, Any]:
    """Time-series for one category, oldest-first, over the trailing ``days``."""
    del _
    if category not in CATEGORIES:
        fail(
            422,
            "validation",
            f"unknown category '{category}'",
            {"category": category, "known": list(CATEGORIES)},
        )

    data = snapshot(category, days)
    return {
        "category": category,
        "days": days,
        "available": bool(data["available"]),
        "reason": data["reason"],
        "series": data["series"],
    }


@router.get("/weakspots")
def get_weakspots(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """Failing and unproven items across every available category, ranked.

    ``sources`` carries each category's availability so an empty ``items`` list
    is distinguishable from "nothing was measured" — the two look identical in
    ``items`` alone, and only one of them is good news.
    """
    del _
    items: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    for category in CATEGORIES:
        data = snapshot(category, OVERVIEW_DAYS)
        items.extend(data["weakspots"])
        sources[category] = {"available": bool(data["available"]), "reason": data["reason"]}
    items.sort(key=weakspot_rank)
    return {
        "generated_at": utc_now_iso(),
        "sources": sources,
        "items": items[:WEAKSPOT_LIMIT],
    }


__all__ = ["router"]
