"""Daily briefing API surface."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.briefing.run import run_briefing
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.store import StewardStore

router = APIRouter(prefix="/api/briefings", tags=["briefings"])


class GenerateRequest(BaseModel):
    dry_run: bool = False


@lru_cache(maxsize=1)
def get_steward_config() -> StewardConfig:
    return load_steward_config()


StewardConfigDep = Annotated[StewardConfig, Depends(get_steward_config)]


def _stores(store: StoreDep) -> tuple[SqliteStore, StewardStore]:
    database = cast(SqliteStore, store)
    return database, StewardStore(database)


@router.get("/latest")
def latest(store: StoreDep) -> dict[str, Any]:
    _, steward = _stores(store)
    row = steward.latest_briefing()
    if row is None:
        fail(404, "not_found", "briefing not found")
    return row


@router.get("")
def list_briefings(store: StoreDep, limit: int = Query(30, ge=1, le=500)) -> list[dict[str, Any]]:
    _, steward = _stores(store)
    return steward.list_briefings(limit=limit)


@router.post("/generate")
def generate(body: GenerateRequest, store: StoreDep, cfg: StewardConfigDep) -> dict[str, Any]:
    database, _ = _stores(store)
    return run_briefing(dry_run=body.dry_run, database=database, cfg=cfg)


@router.post("/{briefing_id}/ack")
def ack(briefing_id: str, store: StoreDep) -> dict[str, Any]:
    _, steward = _stores(store)
    row = steward.ack_briefing(briefing_id)
    if row is None:
        fail(404, "not_found", "briefing not found", {"id": briefing_id})
    return row


__all__ = ["router"]
