"""Alert read and acknowledgement HTTP surface."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AckRequest(BaseModel):
    by: str | None = None


def _steward(store: StoreDep) -> StewardStore:
    return StewardStore(cast(SqliteStore, store))


@router.get("")
def list_alerts(
    store: StoreDep,
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _steward(store).list_alerts(state=state, limit=limit)


@router.get("/count")
def alert_count(store: StoreDep) -> dict[str, int]:
    return {"open": _steward(store).open_alert_count()}


@router.post("/{alert_id}/ack")
def acknowledge_alert(
    alert_id: int, store: StoreDep, body: AckRequest | None = None
) -> dict[str, Any]:
    steward = _steward(store)
    # An indexed by-id read, NOT a scan of the newest 500 rows. The scan made
    # ack a function of an alert's RANK: with 1,806 rows stored, zero of the 56
    # open money/reliability alerts fell inside that window, so every one of
    # them answered 404 "not found" while sitting in the table. Any alert older
    # than the newest 500 was un-ackable, whatever its rule or severity.
    current = steward.get_alert(alert_id)
    if current is None:
        fail(404, "not_found", "alert not found", {"id": alert_id})
    if current["state"] == "acked":
        return current
    updated = steward.ack_alert(alert_id, body.by if body and body.by else "operator")
    return updated if updated is not None else current
