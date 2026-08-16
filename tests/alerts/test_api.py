from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore


def _alert(steward: StewardStore, *, title: str, state: str = "open") -> dict[str, Any]:
    alert = steward.create_alert(
        {
            "rule": "test",
            "severity": "high",
            "title": title,
            "body": "body",
            "cooldown_key": title,
            "state": state,
        }
    )
    assert alert is not None
    return alert


def test_alert_list_count_and_idempotent_ack_with_operator(database: SqliteStore) -> None:
    steward = StewardStore(database)
    first = _alert(steward, title="first")
    _alert(steward, title="second")
    _alert(steward, title="closed", state="acked")
    acknowledged_by: list[str] = []
    original_ack = StewardStore.ack_alert

    def recording_ack(self: StewardStore, alert_id: int, by: str) -> dict[str, Any] | None:
        updated = original_ack(self, alert_id, by)
        if updated is not None:
            acknowledged_by.append(by)
        return updated

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                listed = await client.get("/api/alerts", params={"state": "open", "limit": 1})
                assert listed.status_code == 200
                assert len(listed.json()) == 1
                assert listed.json()[0]["state"] == "open"
                assert (await client.get("/api/alerts/count")).json() == {"open": 2}

                first_ack = await client.post(
                    f"/api/alerts/{first['id']}/ack", json={"by": "alice"}
                )
                second_ack = await client.post(
                    f"/api/alerts/{first['id']}/ack", json={"by": "alice"}
                )
                assert first_ack.status_code == second_ack.status_code == 200
                assert first_ack.json() == second_ack.json()
                assert first_ack.json()["state"] == "acked"
                assert (await client.get("/api/alerts/count")).json() == {"open": 1}
                assert (
                    await client.post("/api/alerts/999999/ack", json={"by": "alice"})
                ).status_code == 404
        finally:
            app.dependency_overrides.clear()

    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(StewardStore, "ack_alert", recording_ack)
        asyncio.run(request())

    assert acknowledged_by == ["alice"]


def test_ack_succeeds_for_an_alert_outside_the_first_500_rows(database: SqliteStore) -> None:
    """Acking must not depend on an alert's RANK in a recency-ordered page.

    The route used to resolve ids by scanning list_alerts(limit=500) and 404ing
    on a miss. Measured on live state, ZERO of the 56 open money/reliability
    alerts fell inside that window (1,806 rows stored, first non-flood row at
    rank 1,714), so every one of them was mechanically un-ackable while sitting
    in the table -- and so was any alert older than the newest 500, whatever
    its rule or severity.
    """
    steward = StewardStore(database)
    oldest = steward.create_alert(
        {
            "rule": "payment_failures",
            "severity": "high",
            "title": "Oldest money case",
            "body": "body",
            "cooldown_key": "oldest",
            "cooldown_minutes": 240,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    assert oldest is not None
    for index in range(600):
        assert (
            steward.create_alert(
                {
                    "rule": "flood_rule",
                    "severity": "medium",
                    "title": f"Filler {index}",
                    "cooldown_key": f"filler-{index}",
                    "cooldown_minutes": 240,
                    "created_at": f"2026-02-{1 + index // 300:02d}T{index % 24:02d}:"
                    f"{index % 60:02d}:00Z",
                }
            )
            is not None
        )
    # Precondition, asserted rather than assumed: the target really is outside
    # the 500-row window the old lookup could see.
    window = {row["id"] for row in steward.list_alerts(limit=500)}
    assert oldest["id"] not in window

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                acked = await client.post(f"/api/alerts/{oldest['id']}/ack", json={"by": "owner"})
                assert acked.status_code == 200, acked.text
                assert acked.json()["state"] == "acked"
                assert acked.json()["id"] == oldest["id"]
                # A genuinely missing id must still 404: the fix removes a rank
                # dependency, it does not make every id resolvable.
                missing = await client.post("/api/alerts/999999/ack", json={"by": "owner"})
                assert missing.status_code == 404
        finally:
            app.dependency_overrides.clear()

    asyncio.run(request())

    row = steward.get_alert(oldest["id"])
    assert row is not None and row["state"] == "acked"
