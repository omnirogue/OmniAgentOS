from __future__ import annotations

import asyncio

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.briefings import get_steward_config
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import BriefingConfig, StewardConfig
from omniagentos.steward.store import StewardStore


def test_route_contract(sqlite_store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "0")
    cfg = StewardConfig(briefing=BriefingConfig(deliver_slack=False, voice_provider=None))
    app.dependency_overrides[get_store] = lambda: sqlite_store
    app.dependency_overrides[get_steward_config] = lambda: cfg
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        missing = asyncio.run(client.get("/api/briefings/latest"))
        row = StewardStore(sqlite_store).insert_briefing(
            {"briefing_date": "2026-07-12", "summary": {"headline": "Existing"}}
        )
        latest = asyncio.run(client.get("/api/briefings/latest"))
        listed = asyncio.run(client.get("/api/briefings", params={"limit": 1}))
        generated = asyncio.run(client.post("/api/briefings/generate", json={"dry_run": True}))
        acked = asyncio.run(client.post(f"/api/briefings/{row['id']}/ack"))
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert latest.status_code == 200 and latest.json()["id"] == row["id"]
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert generated.status_code == 200
    assert generated.json()["headline"] == "Nothing to report"
    assert acked.status_code == 200 and acked.json()["acked_at"]
