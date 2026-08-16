"""GET /api/banking endpoint, offline."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.banking.store import BankAccount, BankFact, BankingStore, BankTransaction
from omniagentos.goals.collect import eastern_yesterday


def _seed(store: Any) -> None:
    bs = BankingStore(store)
    bs.upsert_account(
        BankAccount(
            id="slash:a1",
            provider="slash",
            brand="AcmeUni",
            name="Operating",
            mask="5555",
            kind="checking",
        )
    )
    bs.upsert_fact(
        BankFact(
            day="2026-07-10",
            account_id="slash:a1",
            provider="slash",
            balance_usd=12000.0,
            deposits_usd=100.0,
            expenses_usd=250.0,
            net_flow_usd=-150.0,
            txn_count=2,
        )
    )
    bs.upsert_transaction(
        BankTransaction(
            day="2026-07-10",
            account_id="slash:a1",
            posted_at="2026-07-10T15:00:00Z",
            amount_usd=-250.0,
            description="AWS invoice",
            category=None,
            external_id="slash:a1:tx2",
        )
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_get_banking_returns_shape(store: Any) -> None:
    _seed(store)
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/banking?day=2026-07-10"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["day"] == "2026-07-10"
    assert body["timezone"] == "America/New_York"
    assert body["totals"]["cash_balance_usd"] == 12000.0
    assert body["totals"]["money_in_usd"] == 100.0
    assert body["totals"]["money_out_usd"] == 250.0
    assert body["totals"]["net_flow_usd"] == -150.0
    assert "true cash expenses" in body["totals"]["note"]
    assert body["month_to_date"]["money_out_usd"] == 250.0
    assert body["by_account"][0]["name"] == "Operating"
    assert body["largest_transactions"][0]["amount_usd"] == -250.0
    assert "not_wired" in body["coverage"]
    assert any("Teller" in n["message"] for n in body["data_quality"])


def test_default_day_is_eastern_yesterday(store: Any) -> None:
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/banking"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["day"] == eastern_yesterday().isoformat()


def test_bad_day_is_rejected(store: Any) -> None:
    app.dependency_overrides[get_store] = lambda: store
    client = _client()
    try:
        response = asyncio.run(client.get("/api/banking?day=notadate"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
    assert response.status_code == 422
