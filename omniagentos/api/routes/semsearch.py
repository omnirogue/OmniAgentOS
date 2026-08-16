"""Authenticated HTTP route for unified semantic search."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Query

from omniagentos.api.routes.control import _authorized
from omniagentos.semsearch import search
from omniagentos.semsearch.constants import MAX_QUERY_LENGTH, MAX_RESULT_COUNT

router = APIRouter(
    prefix="/api/semsearch",
    tags=["semsearch"],
    dependencies=[Depends(_authorized)],
)


@router.get("")
def semantic_search(
    q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH),
    kind: Literal["skill", "tool", "capability", "all"] = Query(default="all"),
    limit: int = Query(default=10, ge=1, le=MAX_RESULT_COUNT),
) -> list[dict[str, object]]:
    """Return ranked hits; dependency outages transparently use lexical search."""
    return [asdict(hit) for hit in search(q, kind=kind, limit=min(limit, MAX_RESULT_COUNT))]
