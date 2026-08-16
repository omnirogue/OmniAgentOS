from __future__ import annotations

import asyncio

import httpx


def test_health_reports_store_and_worker(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.get("/api/health"))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["db"] is True
