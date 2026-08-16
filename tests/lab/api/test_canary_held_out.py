"""Binding regression test: protected expected values never leave the lab API."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

CANARY = "CANARY_HELD_OUT_EXPECTED"


def _walk(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [value, *[item for child in value.values() for item in _walk(child)]]
    if isinstance(value, list):
        return [item for child in value for item in _walk(child)]
    return [value]


def test_canary_held_out_expected_never_leaks(asgi_client: httpx.AsyncClient) -> None:
    paths = [
        "/api/lab/disciplines",
        "/api/lab/experiments",
        "/api/lab/experiments/exp_seed",
        "/api/lab/tournaments",
        "/api/lab/tournaments/tnm_seed",
        "/api/lab/leaderboard",
        "/api/lab/playbook",
        "/api/lab/champions",
        "/api/lab/surfaces",
    ]
    for path in paths:
        response = asyncio.run(asgi_client.get(path))
        assert response.status_code == 200
        values = _walk(response.json())
        assert CANARY not in {str(value) for value in values}
        assert not any(
            isinstance(value, dict)
            and value.get("split") == "held_out"
            and value.get("expected") is not None
            for value in values
        )
