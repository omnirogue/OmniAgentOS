"""GET /api/revenue/verticals aggregates the completed Eastern-day window."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.goals.collect import eastern_yesterday
from omniagentos.revenue.store import RevenueFact, RevenueStore


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_get_revenue_verticals_aggregates_facts_and_surfaces_failed_sources(store: Any) -> None:
    revenue_store = RevenueStore(store)
    yesterday = eastern_yesterday()
    previous_day = yesterday - timedelta(days=1)

    revenue_store.upsert_revenue_fact(
        RevenueFact(
            day=yesterday.isoformat(),
            vertical="acmeuni",
            source="stripe",
            revenue_usd=100.0,
            refunds_usd=5.0,
        )
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(
            day=previous_day.isoformat(),
            vertical="acmeuni",
            source="stripe",
            revenue_usd=50.0,
            refunds_usd=2.0,
        )
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(day=yesterday.isoformat(), vertical="acmeuni", source="meta", ad_spend_usd=25.0)
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(day=previous_day.isoformat(), vertical="acmeuni", source="meta", ad_spend_usd=25.0)
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(
            day=yesterday.isoformat(),
            vertical="globex",
            source="stripe",
            revenue_usd=70.0,
            ad_spend_usd=0.0,
        )
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(
            day=previous_day.isoformat(),
            vertical="globex",
            source="stripe",
            revenue_usd=30.0,
            ad_spend_usd=0.0,
        )
    )
    revenue_store.record_source_outcome(
        day=yesterday.isoformat(),
        vertical="acmeuni",
        source="meta:account-ok",
        status="ok",
    )
    revenue_store.record_source_outcome(
        day=yesterday.isoformat(),
        vertical="acmeuni",
        source="meta:account-failed",
        status="failed",
        message="collector unavailable",
    )

    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/revenue/verticals?days=2"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 2
    verticals = {row["vertical"]: row for row in body["verticals"]}
    assert verticals["acmeuni"] == {
        "vertical": "acmeuni",
        "revenue_usd": 150.0,
        "ad_spend_usd": 50.0,
        "roas_collected": 3.0,
        "refunds_usd": 7.0,
        "days_with_data": 2,
    }
    assert verticals["globex"]["revenue_usd"] == 100.0
    assert verticals["globex"]["ad_spend_usd"] == 0.0
    assert verticals["globex"]["roas_collected"] is None
    assert verticals["globex"]["days_with_data"] == 2
    sources = {row["key"]: row for row in body["sources"]}
    assert sources["acmeuni:meta"]["status"] == "failed"
    assert sources["acmeuni:meta"]["last_ok_at"] is None
    assert sources["acmeuni:meta"]["last_day"] == yesterday.isoformat()
    assert sources["acmeuni:meta"]["consecutive_failures"] == 1


def test_verticals_days_upper_bound_is_enforced(store: Any) -> None:
    """A public route must not accept an unbounded window (resource abuse)."""
    revenue_store = RevenueStore(store)
    yesterday = eastern_yesterday()
    revenue_store.upsert_revenue_fact(
        RevenueFact(day=yesterday.isoformat(), vertical="acmeuni", source="stripe", revenue_usd=100.0)
    )
    revenue_store.upsert_revenue_fact(
        RevenueFact(
            day=(yesterday - timedelta(days=365)).isoformat(),
            vertical="acmeuni",
            source="meta",
            ad_spend_usd=25.0,
        )
    )
    app.dependency_overrides[get_store] = lambda: store

    async def _run() -> tuple[int, int, dict[str, Any]]:
        try:
            async with _client() as client:
                over = await client.get("/api/revenue/verticals", params={"days": "999999"})
                at_cap = await client.get("/api/revenue/verticals", params={"days": "366"})
                return over.status_code, at_cap.status_code, at_cap.json()
        finally:
            app.dependency_overrides.clear()

    over_code, at_cap_code, body = asyncio.run(_run())
    assert over_code == 422
    assert at_cap_code == 200
    assert body["verticals"] == [
        {
            "vertical": "acmeuni",
            "revenue_usd": 100.0,
            "ad_spend_usd": 25.0,
            "roas_collected": 4.0,
            "refunds_usd": 0.0,
            "days_with_data": 2,
        }
    ]
