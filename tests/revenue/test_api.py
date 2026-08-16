"""PRIORITY 4: GET /api/revenue endpoint (frozen contract), offline."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.goals.collect import eastern_yesterday
from omniagentos.revenue.store import RevenueFact, RevenueStore


def _seed(store: Any) -> None:
    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="AcmeUni",
            source="stripe",
            revenue_usd=500.0,
            payment_failures=2,
        )
    )
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="AcmeUni",
            source="meta",
            campaign_id="cmp_1",
            campaign_name="Prospecting",
            ad_spend_usd=100.0,
            roas=2.5,
            meta={"revenue_attributed_usd": 250.0},
        )
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_get_revenue_returns_frozen_shape(store: Any) -> None:
    _seed(store)
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/revenue?day=2026-07-10"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["day"] == "2026-07-10"
    assert body["timezone"] == "America/New_York"
    assert body["totals"]["revenue_usd"] == 500.0
    assert body["totals"]["ad_spend_usd"] == 100.0
    assert body["totals"]["gross_contribution_usd"] == 400.0
    assert "NOT net profit" in body["totals"]["note"]
    assert body["by_campaign"][0]["revenue_attributed_usd"] == 250.0
    assert body["by_campaign"][0]["roas"] == 2.5
    assert "not_wired" in body["coverage"]


def test_default_day_is_eastern_yesterday(store: Any) -> None:
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/revenue"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["day"] == eastern_yesterday().isoformat()


def test_bad_day_is_rejected(store: Any) -> None:
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/revenue?day=notadate"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
    assert response.status_code == 422
