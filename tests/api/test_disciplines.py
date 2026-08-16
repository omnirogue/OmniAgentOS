from __future__ import annotations

import asyncio

import httpx


def test_create_discipline_rejects_duplicate(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = asyncio.run(
        asgi_client.post(
            "/api/disciplines", headers=auth_headers, json={"id": "ops", "name": "Ops"}
        )
    )
    duplicate = asyncio.run(
        asgi_client.post(
            "/api/disciplines", headers=auth_headers, json={"id": "ops", "name": "Ops"}
        )
    )

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


def test_create_discipline_requires_local_token(asgi_client: httpx.AsyncClient) -> None:
    # fix6: mutating control-plane routes are token-gated (audit of all POST/PUT
    # mutations, matching the already-gated decide_approval pattern).
    response = asyncio.run(asgi_client.post("/api/disciplines", json={"id": "ops", "name": "Ops"}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
