"""Every route the server serves is described in contract.schema.json, and vice versa.

The contract is the file two people read instead of each other's code (Alice owns
the queue core, Bob the worker side). A route that exists but is undescribed
is the failure mode this catches: the refusal WRITE routes were implemented and
left out of the contract, which is exactly how a second machine ends up keeping
its refusals to itself — the pool-wide ledger is the whole multi-machine point
of §4.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from omniagentos.workqueue.server import create_app

CONTRACT = (
    Path(__file__).resolve().parents[2] / "omniagentos" / "workqueue" / "contract.schema.json"
)
TOKEN = "b6f0c8de0b0c4d9f8b2a1c3e5d7f9a1b"


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setenv("WQ_TOKEN", TOKEN)
    with TestClient(create_app(store=store, reaper=False)) as client:
        yield client


def _served(app) -> set[str]:
    routes: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/v1"):
            continue
        # The contract writes the resource path param as {id}; FastAPI uses the
        # handler's own parameter name. {input_key} is spelled the same in both.
        for old in ("{unit_id}", "{machine_id}"):
            path = path.replace(old, "{id}")
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            routes.add(f"{method} {path}")
    return routes


def test_every_served_route_is_in_the_contract(client, contract) -> None:
    described = set(contract["routes"])
    missing = _served(client.app) - described
    assert not missing, f"routes served but undescribed in contract.schema.json: {sorted(missing)}"


def test_every_contract_route_is_served(client, contract) -> None:
    extra = set(contract["routes"]) - _served(client.app)
    assert not extra, f"routes described but not served: {sorted(extra)}"


def test_refusal_record_body_matches_the_contract_and_the_store(client, contract) -> None:
    body = {
        "input_key": "k" * 16,
        "gate": "unit-acceptance",
        "refusal_class": "instrument-error",
        "retryable": 1,
        "remedy": "dirty workspace — commit or stash, then re-gate",
        "unit_id": None,
    }
    jsonschema.validate(body, {"$defs": contract["$defs"], "$ref": "#/$defs/refusal_record"})
    response = client.post("/v1/refusals", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    row = response.json()
    assert row["count"] == 1
    jsonschema.validate(row, {"$defs": contract["$defs"], "$ref": "#/$defs/refusal_row"})

    # ...and the clear route takes the gate as a QUERY parameter, no body.
    cleared = client.post(
        f"/v1/refusals/{body['input_key']}/clear",
        params={"gate": "unit-acceptance"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert cleared.status_code == 200 and cleared.json() == {"ok": True}
    assert (
        client.get(
            f"/v1/refusals/{body['input_key']}",
            params={"gate": "unit-acceptance"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code
        == 404
    )


def test_the_refusal_write_routes_are_authed(client) -> None:
    assert client.post("/v1/refusals", json={}).status_code == 401
    assert client.post("/v1/refusals/abc/clear", params={"gate": "g"}).status_code == 401
