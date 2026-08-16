from __future__ import annotations

import asyncio

import httpx

from tests.api.fake_store import FakeStore


def test_pause_change_is_persisted_and_emitted(
    asgi_client: httpx.AsyncClient, store: FakeStore, auth_headers: dict[str, str]
) -> None:
    response = asyncio.run(
        asgi_client.put(
            "/api/pause", headers=auth_headers, json={"paused": True, "reason": "maintenance"}
        )
    )

    assert response.status_code == 200
    assert response.json()["paused"] is True
    assert store.events[-1]["type"] == "pause.changed"


def test_pause_change_requires_local_token(asgi_client: httpx.AsyncClient) -> None:
    # fix6: an agent must not be able to un-pause a safety-paused system — the
    # mutating PUT is token-gated (the GET stays open for status reads).
    response = asyncio.run(asgi_client.put("/api/pause", json={"paused": False}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
