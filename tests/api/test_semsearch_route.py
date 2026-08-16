"""Authenticated JSON shape for GET /api/semsearch."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import semsearch as semsearch_route
from omniagentos.semsearch.constants import MAX_QUERY_LENGTH, MAX_RESULT_COUNT
from omniagentos.semsearch.search import SemHit


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_semsearch_route_requires_auth(client: httpx.AsyncClient) -> None:
    response = _run(client.get("/api/semsearch", params={"q": "release"}))
    assert response.status_code == 401


def test_semsearch_route_returns_ranked_hit_shape(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_search(query: str, *, kind: str, limit: int) -> list[SemHit]:
        seen.update(query=query, kind=kind, limit=limit)
        return [SemHit("skill", "release", "Release Service", 0.93, "semantic")]

    monkeypatch.setattr(semsearch_route, "search", fake_search)
    response = _run(
        client.get(
            "/api/semsearch",
            params={"q": "ship to prod", "kind": "skill", "limit": 4},
            headers=auth_headers,
        )
    )

    assert response.status_code == 200
    assert seen == {"query": "ship to prod", "kind": "skill", "limit": 4}
    assert response.json() == [
        {
            "kind": "skill",
            "ref_id": "release",
            "title": "Release Service",
            "score": 0.93,
            "source": "semantic",
        }
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * (MAX_QUERY_LENGTH + 1)},
        {"q": "release", "limit": MAX_RESULT_COUNT + 1},
    ],
)
def test_semsearch_route_rejects_requests_above_shared_bounds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    params: dict[str, object],
) -> None:
    response = _run(client.get("/api/semsearch", params=params, headers=auth_headers))

    assert response.status_code == 422
