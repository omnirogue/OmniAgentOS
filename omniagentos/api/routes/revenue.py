"""Revenue / P&L dashboard HTTP surface (frozen contract).

GET /api/revenue?day=YYYY-MM-DD reads the dimensional ``revenue_facts`` store for
one Eastern calendar day (default: yesterday ET) and returns the frozen shape the
dashboard builds against. It is a pure read model — no external calls, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.goals.collect import eastern_yesterday
from omniagentos.revenue.collect import BRANDS
from omniagentos.revenue.report import _status_for_source, _vertical_sort_key, build_revenue_report
from omniagentos.revenue.store import RevenueStore

router = APIRouter(prefix="/api/revenue", tags=["revenue"])


class VerticalRevenue(BaseModel):
    """Collected-revenue totals; ``roas_collected`` uses collected revenue, not attributed value."""

    vertical: str
    revenue_usd: float
    ad_spend_usd: float
    roas_collected: float | None
    refunds_usd: float
    days_with_data: int


class RevenueSourceStatus(BaseModel):
    key: str
    status: str
    last_ok_at: str | None
    last_day: str | None
    consecutive_failures: int


class RevenueVerticalsResponse(BaseModel):
    days: int
    generated_at: str
    verticals: list[VerticalRevenue]
    sources: list[RevenueSourceStatus]


def _resolve_day(day: str | None) -> str:
    if day is None:
        return eastern_yesterday().isoformat()
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        # fail() is NoReturn — it raises ApiError, so control never falls through.
        fail(422, "validation", "day must be YYYY-MM-DD", {"day": day})


_MAX_WINDOW_DAYS = 366  # public route: an unbounded window is a resource-abuse


@dataclass
class _VerticalTotals:
    """Collected revenue (already net of refunds) accumulated per vertical."""

    revenue: float = 0.0
    ad_spend: float = 0.0
    refunds: float = 0.0
    days: set[str] = field(default_factory=set)


def _resolve_days(days: str) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        fail(422, "validation", "days must be a positive integer", {"days": days})
    if value <= 0:
        fail(422, "validation", "days must be a positive integer", {"days": days})
    if value > _MAX_WINDOW_DAYS:
        fail(422, "validation", f"days must be <= {_MAX_WINDOW_DAYS}", {"days": days})
    return value


@router.get("")
def get_revenue(
    store: StoreDep,
    day: Annotated[str | None, Query(description="Eastern calendar day YYYY-MM-DD")] = None,
) -> dict[str, Any]:
    target_day = _resolve_day(day)
    revenue_store = RevenueStore(cast(SqliteStore, store))
    facts = revenue_store.query_day(target_day)
    return build_revenue_report(target_day, facts, generated_at=utc_now_iso())


@router.get("/verticals", response_model=RevenueVerticalsResponse)
def get_revenue_verticals(
    store: StoreDep,
    days: str = Query(default="7", description="Number of completed Eastern calendar days"),
) -> RevenueVerticalsResponse:
    """Aggregate collected revenue (already net of refunds) for the ET day window."""
    window_days = _resolve_days(days)
    revenue_store = RevenueStore(cast(SqliteStore, store))
    ending_day = eastern_yesterday()
    starting_day = ending_day - timedelta(days=window_days - 1)
    totals: dict[str, _VerticalTotals] = {}

    for fact in revenue_store.aggregate_days(starting_day.isoformat(), ending_day.isoformat()):
        vertical = str(fact.get("vertical", ""))
        slot = totals.setdefault(vertical, _VerticalTotals())
        slot.revenue += float(fact.get("revenue_usd", 0.0) or 0.0)
        slot.ad_spend += float(fact.get("ad_spend_usd", 0.0) or 0.0)
        slot.refunds += float(fact.get("refunds_usd", 0.0) or 0.0)
        slot.days.add(str(fact.get("day", "")))

    verticals = []
    for vertical in sorted(totals, key=_vertical_sort_key):
        slot = totals[vertical]
        spend = slot.ad_spend
        revenue = slot.revenue
        verticals.append(
            VerticalRevenue(
                vertical=vertical,
                revenue_usd=round(revenue, 2),
                ad_spend_usd=round(spend, 2),
                # The numerator is collected revenue, NOT ad-platform-attributed value.
                roas_collected=None if spend == 0 else round(revenue / spend, 2),
                refunds_usd=round(slot.refunds, 2),
                days_with_data=len(slot.days),
            )
        )

    raw_statuses = revenue_store.source_status_snapshot()
    normalized_statuses = [{**row, "status": row.get("last_status")} for row in raw_statuses]
    source_pairs = {
        (
            str(row.get("vertical", "")),
            "meta"
            if str(row.get("source", "")).startswith("meta:")
            else str(row.get("source", "")),
        )
        for row in normalized_statuses
        if row.get("vertical") and row.get("source")
    }
    source_pairs.update(
        (brand.vertical, source) for brand in BRANDS for source in ("stripe", "meta")
    )
    sources = []
    for vertical, source in sorted(
        source_pairs, key=lambda pair: (_vertical_sort_key(pair[0]), pair[1])
    ):
        row = _status_for_source(vertical, source, normalized_statuses)
        if row is None:
            sources.append(
                RevenueSourceStatus(
                    key=f"{vertical}:{source}",
                    status="never_collected",
                    last_ok_at=None,
                    last_day=None,
                    consecutive_failures=0,
                )
            )
            continue
        status = str(row["status"])
        sources.append(
            RevenueSourceStatus(
                key=f"{vertical}:{source}",
                status=status,
                # The status table only retains its current outcome, so a failing
                # row has no trustworthy "last successful" timestamp to surface.
                last_ok_at=str(row["updated_at"]) if status == "ok" else None,
                last_day=str(row["last_day"]),
                consecutive_failures=max(0, int(row.get("consecutive_failures", 0) or 0)),
            )
        )
    return RevenueVerticalsResponse(
        days=window_days,
        generated_at=utc_now_iso(),
        verticals=verticals,
        sources=sources,
    )
