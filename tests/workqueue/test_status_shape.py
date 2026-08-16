"""`status()` must satisfy contract.schema.json — it IS the GET /v1/status payload.

`wq status` and `wq status --json` are the whole observability deliverable (§6),
so the shape is a contract, not a convenience: the CLI, the client and any later
renderer all read this one payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from omniagentos.workqueue.schema import LEASE_TTL_S
from tests.workqueue.conftest import at, submit

CONTRACT = (
    Path(__file__).resolve().parents[2] / "omniagentos" / "workqueue" / "contract.schema.json"
)


@pytest.fixture(scope="module")
def status_schema():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return {"$defs": contract["$defs"], "$ref": "#/$defs/status_response"}


def test_empty_pool_validates(store, status_schema):
    jsonschema.validate(store.status(), status_schema)


def test_populated_pool_validates_and_counts(store, status_schema):
    store.enroll_machine(
        {
            "machine_id": "mac-studio",
            "hostname": "mac-studio.local",
            "os": "darwin",
            "labels": ["build", "gate"],
            "max_concurrent": 2,
            "ncpu": 24,
            "perf_cores": 16,
            "mem_gb": 64.0,
        }
    )
    store.enroll_machine(
        {
            "machine_id": "acmeuni",
            "hostname": "acmeuni",
            "os": "linux",
            "labels": ["linux", "build"],
            "max_concurrent": 2,
            "ncpu": 16,
            "perf_cores": 16,
            "mem_gb": 31.0,
            "ceiling_fraction": 0.6,
        }
    )

    store.enqueue(submit("done-unit"))
    store.enqueue(submit("waiting-unit"))
    store.enqueue(submit("browser-unit", labels=["browser"]))

    # Which of the two unlabelled units comes first is not asserted: they are
    # enqueued in the same second, so the tiebreak is ULID randomness (see
    # test_claim_policy). What matters is that one completes and one is running.
    claimed = store.claim("mac-studio", "w1", ["build", "gate"], now=at(0))
    assert claimed is not None
    done_id = claimed["unit"]["id"]
    store.record_result(
        done_id, "mac-studio:w1", claimed["lease_generation"], "pass", exit_code=0, now=at(30)
    )

    running = store.claim("mac-studio", "w2", ["build", "gate"], now=at(40))
    assert running is not None

    status = store.status(now=at(60))
    jsonschema.validate(status, status_schema)

    assert status["depth"]["done"] == 1
    assert status["depth"]["claimed"] == 1
    assert status["depth"]["queued"] == 1
    assert status["double_executions"] == 0
    assert status["capacity"] == {
        "total_cores": 40,
        "total_perf_cores": 32,
        "total_slots": 4,
        "free_slots": 3,
        "in_flight": 1,
    }

    # A queue that looks idle because nothing can run must never read as healthy.
    assert len(status["unclaimable"]) == 1
    unclaimable = status["unclaimable"][0]
    assert store.get_unit(unclaimable["unit_id"])["idempotency_key"] == "browser-unit"
    assert unclaimable["labels"] == ["browser"]
    assert unclaimable["reason"] == "unclaimable-no-capable-machine"

    assert status["oldest_unclaimed_s"] is not None
    machines = {row["machine_id"]: row for row in status["machines"]}
    assert machines["mac-studio"]["done_1h"] == 1
    assert machines["mac-studio"]["in_flight"] == 1
    assert machines["acmeuni"]["done_1h"] == 0


def test_refusal_share_is_measured_and_printed(store, status_schema):
    unit_id, _ = store.enqueue(submit("shares"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "instrument-error",
        exit_code=2,
        retryable=1,
        remedy="dirty workspace",
        now=at(10),
    )
    claimed = store.claim("mac-studio", "w1", [], now=at(2000))
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "candidate-defect",
        exit_code=1,
        now=at(2010),
    )

    status = store.status(now=at(2100))
    jsonschema.validate(status, status_schema)
    assert status["refusals_24h"]["instrument-error"] == 1
    assert status["refusals_24h"]["candidate-defect"] == 1
    assert status["refusals_24h"]["total"] == 2
    # the operator's baseline is 64/90 = 71% mechanics; watching this fall is how §4 is
    # proven to work, so it must be reported even when it is ugly.
    assert status["refusals_24h"]["instrument_share"] == 0.5


def test_oldest_unclaimed_reflects_the_headline_metric(store):
    unit_id, _ = store.enqueue(submit("old"))
    created_at = store.get_unit(unit_id)["created_at"]

    # Work sitting while capacity idles is the entire problem this queue exists
    # to fix, so this is the headline number (alert at >15m with idle capacity).
    eleven_minutes_later = at(11 * 60 + 4, base=created_at)
    assert store.status(now=eleven_minutes_later)["oldest_unclaimed_s"] == pytest.approx(664)

    store.claim("mac-studio", "w1", [], now=at(0, base=created_at))
    later = at(LEASE_TTL_S, base=created_at)
    assert store.status(now=later)["oldest_unclaimed_s"] is None
