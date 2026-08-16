"""HttpQueueClient against a real wq-server socket.

The interchangeability claim ("Phase 1 upgrades to Phase 2 without rewriting the
claim logic") is only worth something if it is exercised end to end: TestClient
would bypass the transport this class actually uses. So this starts uvicorn on an
ephemeral port and drives the same method surface the worker will call.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn

from omniagentos.workqueue.client import HttpQueueClient, QueueServerError
from omniagentos.workqueue.schema import LeaseLost
from omniagentos.workqueue.server import create_app
from tests.workqueue.conftest import submit

TOKEN = "9f2b7c1d5e3a4b6c8d0e2f4a6b8c0d1e"


@pytest.fixture
def server_url(store):
    app = create_app(store=store, token=TOKEN, reaper=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise AssertionError("wq-server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def client(server_url):
    return HttpQueueClient(server_url, token=TOKEN)


def test_full_lifecycle_over_http(client):
    unit_id, deduped = client.enqueue(submit("http-lifecycle"))
    assert deduped is False
    assert client.enqueue(submit("http-lifecycle"))[1] is True

    claimed = client.claim("mac-studio", "w1", [])
    assert claimed is not None
    assert claimed["unit"]["id"] == unit_id
    assert claimed["attempt"] == 1

    client.heartbeat(unit_id, "mac-studio:w1", claimed["lease_generation"])
    out = client.record_result(
        unit_id, "mac-studio:w1", claimed["lease_generation"], "pass", exit_code=0
    )
    assert out["unit"]["state"] == "done"

    assert client.get_unit(unit_id)["state"] == "done"
    assert [row["outcome"] for row in client.list_attempts(unit_id)] == ["pass"]
    assert client.get_unit("wq_missing") is None

    status = client.status()
    assert status["depth"]["done"] == 1
    assert status["double_executions"] == 0


def test_nothing_claimable_is_none_not_an_error(client):
    assert client.claim("mac-studio", "w1", []) is None


def test_409_becomes_lease_lost_on_the_worker_side(client):
    unit_id, _ = client.enqueue(submit("http-fence"))
    claimed = client.claim("mac-studio", "w1", [])
    stale = claimed["lease_generation"] - 1

    # The worker's reaction (kill the child tree, write nothing) must not depend
    # on whether it holds a store or a client.
    with pytest.raises(LeaseLost):
        client.heartbeat(unit_id, "mac-studio:w1", stale)
    with pytest.raises(LeaseLost):
        client.record_result(unit_id, "mac-studio:w1", stale, "pass", exit_code=0)
    assert client.get_unit(unit_id)["state"] == "claimed"


def test_bad_token_is_an_error_not_a_silent_none(server_url):
    anonymous = HttpQueueClient(server_url, token="")
    with pytest.raises(QueueServerError) as caught:
        anonymous.status()
    assert caught.value.status == 401
    assert caught.value.code == "unauthorized"


def test_machines_refusals_and_alerts_round_trip(client):
    client.enroll_machine(
        {
            "machine_id": "mw0002",
            "hostname": "mw0002.local",
            "os": "darwin",
            "labels": ["build", "gate"],
            "max_concurrent": 3,
            "ncpu": 16,
            "perf_cores": 16,
        }
    )
    assert [row["machine_id"] for row in client.list_machines()] == ["mw0002"]
    assert (
        client.machine_beat("mw0002", {"load1": 2.0, "worker_id": "mw0002:1:abcd1234", "pid": 1})[
            "drain"
        ]
        == 0
    )
    client.set_drain("mw0002", True)
    assert client.machine_beat("mw0002", {"load1": 2.0})["drain"] == 1
    client.set_drain("mw0002", False)
    assert client.machine_beat("mw0002", {"load1": 2.0})["drain"] == 0

    key = "d" * 64
    assert client.refusal_check(key, "unit-acceptance") is None
    row = client.refusal_record(key, "unit-acceptance", "instrument-error", 1, "clean the worktree")
    assert row["count"] == 1
    assert client.refusal_check(key, "unit-acceptance")["refusal_class"] == "instrument-error"
    client.refusal_clear(key, "unit-acceptance")
    assert client.refusal_check(key, "unit-acceptance") is None

    assert client.alerts() == []


def test_requeue_over_http_is_soft_parks_only(client, store):
    """The two verbs stay separate on the wire, or a remote resubmit re-opens the
    busy-loop the ledger exists to close (§4.5): requeue keeps the refusal row,
    and a TERMINAL park refuses with the envelope rather than quietly re-queueing."""
    unit_id, _ = client.enqueue(submit("http-requeue"))
    claimed = client.claim("mac-studio", "w1", [])
    client.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "unchanged-retry",
        retryable=0,
        remedy="nothing changed",
    )
    assert client.get_unit(unit_id)["terminal_reason"] is None, "a soft park"

    client.requeue(unit_id)
    assert client.get_unit(unit_id)["state"] == "queued"

    store.park(unit_id, "storm-parked", "change the input")
    with pytest.raises(QueueServerError) as caught:
        client.requeue(unit_id)
    # Status and code are the contract a client branches on; the store's own
    # message stays server-side (it names ids, paths and remedies). The refusal
    # is what matters here — the unit did NOT quietly re-queue.
    assert caught.value.status == 400
    assert caught.value.code == "bad-request"
    assert unit_id not in caught.value.message, "the store's text must not ride the wire"
    assert client.get_unit(unit_id)["state"] == "parked"

    with pytest.raises(QueueServerError) as missing:
        client.requeue("wq_nope")
    assert missing.value.status == 404


def test_reaper_is_not_a_client_capability(client):
    # One reaper per pool, in the server process (§3.4). A client-side reaper
    # would be a second opinion about liveness, which is the fail-open bug.
    with pytest.raises(NotImplementedError):
        client.reap_expired()
