"""wq-server: health is open, everything else needs the bearer token, 409 is lease-lost.

The server is a transport, so these tests check the three things that only exist
at the edge: the auth boundary, the error envelope, and that LeaseLost survives
the wire as 409 (a worker must react identically whether it holds a store or a
client).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omniagentos.workqueue.server import create_app
from tests.workqueue.conftest import submit

TOKEN = "b6f0c8de0b0c4d9f8b2a1c3e5d7f9a1b"


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setenv("WQ_TOKEN", TOKEN)
    app = create_app(store=store, reaper=False)
    with TestClient(app) as client:
        yield client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_is_open(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/status"),
        ("GET", "/v1/machines"),
        ("GET", "/v1/alerts"),
        ("POST", "/v1/claim"),
        ("POST", "/v1/units"),
    ],
)
def test_every_other_route_is_401_without_the_token(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_token_and_wrong_scheme_are_both_401(client):
    assert (
        client.get("/v1/status", headers={"Authorization": f"Bearer {'x' * 32}"}).status_code == 401
    )
    assert client.get("/v1/status", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401


def test_unset_server_token_fails_closed(store, monkeypatch):
    monkeypatch.delenv("WQ_TOKEN", raising=False)
    with TestClient(create_app(store=store, reaper=False)) as client:
        # A queue that accepts anonymous claims on the LAN is worse than one
        # that is down, so an unconfigured token refuses everything.
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/status").status_code == 401
        assert client.get("/v1/status", headers=auth()).status_code == 401


def test_enqueue_claim_and_result_round_trip(client):
    created = client.post("/v1/units", json=submit("http-1"), headers=auth())
    assert created.status_code == 200
    unit_id = created.json()["id"]
    assert created.json()["deduped"] is False
    assert client.post("/v1/units", json=submit("http-1"), headers=auth()).json()["deduped"] is True

    claimed = client.post(
        "/v1/claim",
        json={"machine_id": "mw0001-owner", "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert claimed.status_code == 200
    body = claimed.json()
    assert body["unit"]["id"] == unit_id
    generation = body["lease_generation"]

    beat = client.post(
        f"/v1/units/{unit_id}/heartbeat",
        json={"owner": "mw0001-owner:w1", "lease_generation": generation},
        headers=auth(),
    )
    assert beat.status_code == 200

    result = client.post(
        f"/v1/units/{unit_id}/result",
        json={
            "owner": "mw0001-owner:w1",
            "lease_generation": generation,
            "outcome": "pass",
            "exit_code": 0,
        },
        headers=auth(),
    )
    assert result.status_code == 200
    assert result.json()["unit"]["state"] == "done"

    fetched = client.get(f"/v1/units/{unit_id}", headers=auth())
    assert fetched.json()["unit"]["state"] == "done"
    assert len(fetched.json()["attempts"]) == 1


def test_nothing_claimable_is_204_not_an_error(client):
    response = client.post(
        "/v1/claim",
        json={"machine_id": "mac-studio", "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 204
    assert response.content == b""


def test_stale_generation_is_409_over_http(client):
    unit_id = client.post("/v1/units", json=submit("http-fence"), headers=auth()).json()["id"]
    claimed = client.post(
        "/v1/claim",
        json={"machine_id": "mac-studio", "worker_id": "w1", "labels": []},
        headers=auth(),
    ).json()
    stale = claimed["lease_generation"] - 1

    beat = client.post(
        f"/v1/units/{unit_id}/heartbeat",
        json={"owner": "mac-studio:w1", "lease_generation": stale},
        headers=auth(),
    )
    assert beat.status_code == 409
    assert beat.json()["error"]["code"] == "lease-lost"

    result = client.post(
        f"/v1/units/{unit_id}/result",
        json={"owner": "mac-studio:w1", "lease_generation": stale, "outcome": "pass"},
        headers=auth(),
    )
    assert result.status_code == 409
    # The zombie wrote nothing: the unit is still leased to its real holder.
    assert client.get(f"/v1/units/{unit_id}", headers=auth()).json()["unit"]["state"] == "claimed"


def test_unknown_unit_is_404_with_an_envelope(client):
    response = client.get("/v1/units/wq_nope", headers=auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not-found"


def test_bad_submit_is_400_with_an_envelope(client):
    response = client.post("/v1/units", json=submit("bad", base_sha="main"), headers=auth())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad-request"


def test_a_store_error_never_relays_its_own_text_to_the_client(client, capsys):
    """The status code is the contract; the store's prose is not.

    Store messages are written for an operator standing next to the DB and name
    unit ids, absolute paths, schema constraints and remedies. Echoing them to
    whoever can reach the port turns every 400 into a free description of the
    primary's internals, so the wire gets a stable generic line and the LOG gets
    the whole thing — which is also where an operator would look for it anyway.
    """
    bad = client.post("/v1/units", json=submit("leak", base_sha="main"), headers=auth())
    body = bad.text
    assert bad.status_code == 400
    assert "40-hex" not in body and "base_sha" not in body, f"store text leaked: {body}"

    missing = client.post(
        "/v1/units/wq_definitely_not_here/requeue", headers=auth()
    )  # store raises KeyError
    assert missing.status_code == 404
    assert "wq_definitely_not_here" not in missing.text, f"store text leaked: {missing.text}"

    # ...and none of it was thrown away: the operator-facing copy is on stderr.
    logged = capsys.readouterr().err
    assert "base_sha must be 40-hex" in logged
    assert "wq_definitely_not_here" in logged


def test_machines_beat_and_drain_over_http(client):
    client.post(
        "/v1/machines",
        json={
            "machine_id": "acmeuni",
            "hostname": "acmeuni",
            "os": "linux",
            "labels": ["linux"],
            "max_concurrent": 2,
            "ncpu": 16,
        },
        headers=auth(),
    )
    machines = client.get("/v1/machines", headers=auth()).json()["machines"]
    assert [row["machine_id"] for row in machines] == ["acmeuni"]

    assert client.post("/v1/machines/acmeuni/beat", json={"load1": 0.4}, headers=auth()).json() == {
        "drain": 0
    }
    client.post("/v1/machines/acmeuni/drain", json={}, headers=auth())
    assert client.post("/v1/machines/acmeuni/beat", json={"load1": 0.4}, headers=auth()).json() == {
        "drain": 1
    }
    client.post("/v1/machines/acmeuni/drain", json={"undo": True}, headers=auth())
    assert client.post("/v1/machines/acmeuni/beat", json={"load1": 0.4}, headers=auth()).json() == {
        "drain": 0
    }


def test_refusal_routes_are_pool_wide(client):
    key = "c" * 64
    assert (
        client.get(f"/v1/refusals/{key}", params={"gate": "g"}, headers=auth()).status_code == 404
    )
    recorded = client.post(
        "/v1/refusals",
        json={
            "input_key": key,
            "gate": "g",
            "refusal_class": "instrument-error",
            "retryable": 1,
            "remedy": "the exemption is not on main yet",
        },
        headers=auth(),
    )
    assert recorded.status_code == 200
    assert recorded.json()["count"] == 1

    fetched = client.get(f"/v1/refusals/{key}", params={"gate": "g"}, headers=auth())
    assert fetched.json()["refusal_class"] == "instrument-error"

    client.post(f"/v1/refusals/{key}/clear", params={"gate": "g"}, headers=auth())
    assert (
        client.get(f"/v1/refusals/{key}", params={"gate": "g"}, headers=auth()).status_code == 404
    )
