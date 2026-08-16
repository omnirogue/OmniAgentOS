from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.goals.seed import seed
from omniagentos.goals.service import goal_detail, goal_summary
from omniagentos.steward.store import StewardStore


def test_seed_is_idempotent_and_summary_has_latest_and_trend(store: SqliteStore) -> None:
    first = seed(store)
    second = seed(store)
    assert [goal["id"] for goal in first] == ["increase-revenue", "improve-ad-roi"]
    assert [goal["id"] for goal in second] == ["increase-revenue", "improve-ad-roi"]
    assert {row["id"] for row in store.list_disciplines()} >= {"revenue", "ad-roi"}

    steward = StewardStore(store)
    steward.insert_metric_snapshot(
        {
            "goal_id": "increase-revenue",
            "source": "stripe",
            "metric": "net_revenue_usd",
            "value": 12.0,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    summary = goal_summary(steward, steward.get_goal("increase-revenue") or {})
    assert summary["latest"]["net_revenue_usd"]["value"] == 12.0
    assert summary["trend"]["net_revenue_usd"][0]["value"] == 12.0


def test_detail_degrades_facts_when_knowledge_disabled(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store)
    steward = StewardStore(store)
    steward.link_fact_to_goal("increase-revenue", 77, "operator")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_ENABLED", "0")
    detail = goal_detail(steward, steward.get_goal("increase-revenue") or {}, store)
    assert detail["facts"] == [{"fact_id": 77, "statement": "(fact unavailable)"}]


def test_goal_routes_list_detail_404_and_idempotent_fact_link(store: SqliteStore) -> None:
    seed(store)
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        listed = asyncio.run(client.get("/api/goals"))
        missing = asyncio.run(client.get("/api/goals/missing"))
        linked = asyncio.run(client.post("/api/goals/increase-revenue/facts", json={"fact_id": 8}))
        repeated = asyncio.run(
            client.post("/api/goals/increase-revenue/facts", json={"fact_id": 8})
        )
        detail = asyncio.run(client.get("/api/goals/increase-revenue"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert listed.status_code == 200 and len(listed.json()) == 2
    assert missing.status_code == 404
    assert linked.status_code == 200 and repeated.status_code == 200
    assert detail.json()["facts"] == [{"fact_id": 8, "statement": "(fact unavailable)"}]
