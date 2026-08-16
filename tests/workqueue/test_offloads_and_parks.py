"""Two status() facts the operator asked for by name, and one invariant they must not break.

* ``offloads`` — "we should be aware when one of us is offloading or has a
  pending job on a computer" (ROUTING-DECISIONS §2). Per person: pending,
  in-flight, and WHICH box it is on.
* ``parks`` counts TERMINAL parks only. A soft park (``unchanged-retry``) is
  deliberately silent — nothing ran, nothing was spent, and no one is woken —
  so counting it would make §6's ``alerts_sent == parks`` ratio read as a
  permanent deficit and train the operator to ignore the one number that
  detects a park nobody was told about. Soft parks stay in ``depth.parked``.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from omniagentos.workqueue.schema import REFUSAL_STORM_CAP
from tests.workqueue.conftest import at, submit

CONTRACT = (
    Path(__file__).resolve().parents[2] / "omniagentos" / "workqueue" / "contract.schema.json"
)


@pytest.fixture(scope="module")
def status_schema():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return {"$defs": contract["$defs"], "$ref": "#/$defs/status_response"}


def _by_person(status: dict) -> dict[str, dict]:
    return {row["person"]: row for row in status["offloads"]}


# --------------------------------------------------------------------- offloads


def test_offloads_names_the_person_the_counts_and_the_machines(store, status_schema) -> None:
    # Priority pins WHICH units the two claims below take: everything is
    # enqueued in the same second, so created_at ties and the fallback tiebreak
    # is ULID randomness (see test_claim_policy).
    store.enqueue(submit("owner-running", submitted_by="owner", priority=0))
    store.enqueue(submit("owner-running-2", submitted_by="owner", priority=0))
    store.enqueue(submit("owner-queued", submitted_by="owner", priority=3))
    store.enqueue(submit("alice-queued", submitted_by="alice", priority=3))
    store.enqueue(submit("nobodys-unit", priority=3))

    first = store.claim("mw0001-owner", "w1", [], now=at(0))
    second = store.claim("mw0002", "w1", [], now=at(1))
    assert first is not None and second is not None

    status = store.status(now=at(10))
    jsonschema.validate(status, status_schema)
    people = _by_person(status)

    assert people["owner"]["running"] == 2
    assert people["owner"]["queued"] == 1
    assert people["owner"]["in_review"] == 0
    # The machine, not the worker id: lease_owner is '<machine>:<worker>' and
    # worker_id is itself '<machine>:<pid>:<nonce>'.
    assert people["owner"]["machines"] == ["mw0001-owner", "mw0002"]

    assert people["alice"] == {
        "person": "alice",
        "queued": 1,
        "running": 0,
        "in_review": 0,
        "machines": [],
    }
    # An unattributed unit is SHOWN, never dropped — a backlog nobody owns is
    # exactly the thing that would otherwise go unnoticed.
    assert people["(unattributed)"]["queued"] == 1


def test_a_sensitive_unit_lands_in_the_persons_review_column(store) -> None:
    unit_id, _ = store.enqueue(
        submit("needs-review", submitted_by="bob", risk_class="sensitive")
    )
    claimed = store.claim("bob-studio", "w1", [], now=at(0))
    store.record_result(
        unit_id, "bob-studio:w1", claimed["lease_generation"], "pass", exit_code=0, now=at(30)
    )

    row = _by_person(store.status(now=at(60)))["bob"]
    assert (row["in_review"], row["running"], row["queued"]) == (1, 0, 0)


def test_finished_and_parked_units_leave_the_persons_line(store) -> None:
    """Only LIVE work counts: a person's line must not grow forever."""
    unit_id, _ = store.enqueue(submit("done-unit", submitted_by="owner"))
    claimed = store.claim("mw0002", "w1", [], now=at(0))
    store.record_result(
        unit_id, "mw0002:w1", claimed["lease_generation"], "pass", exit_code=0, now=at(30)
    )
    status = store.status(now=at(60))
    assert status["depth"]["done"] == 1
    assert status["offloads"] == []


def test_worker_side_enqueue_without_attribution_is_never_invented(store) -> None:
    """The claiming machine is where it RAN, not who asked for it."""
    store.enqueue(submit("anon", submitted_by=""))
    store.claim("mw0001-owner", "w1", [], now=at(0))
    (row,) = store.status(now=at(5))["offloads"]
    assert row["person"] == "(unattributed)"
    assert row["machines"] == ["mw0001-owner"]


# ------------------------------------------------------------------------ parks


def test_a_soft_park_is_depth_but_not_a_park(store, status_schema) -> None:
    unit_id, _ = store.enqueue(submit("soft"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "unchanged-retry",
        exit_code=2,
        retryable=0,
        remedy="change the tree, the gate config or the gate itself",
        now=at(10),
    )

    unit = store.get_unit(unit_id)
    assert unit["state"] == "parked" and unit["terminal_reason"] is None

    status = store.status(now=at(20))
    jsonschema.validate(status, status_schema)
    assert status["depth"]["parked"] == 1, "a soft park must stay visible in depth"
    assert status["parks"] == 0, "a soft park announces nothing, so it is not a park"
    assert status["alerts_sent"] == 0


def test_alerts_equal_parks_one_to_one_after_a_storm_park(store) -> None:
    """§6's ratio, across the sequence that actually produces both kinds."""
    soft_id, _ = store.enqueue(submit("soft-then-storm"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.record_result(
        soft_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "unchanged-retry",
        retryable=0,
        remedy="nothing changed",
        now=at(10),
    )

    storm_id, _ = store.enqueue(submit("stormy"))
    for index in range(REFUSAL_STORM_CAP):
        store.refusal_record(
            "key-abc",
            "unit-acceptance",
            "instrument-error",
            1,
            "dirty workspace",
            unit_id=storm_id,
            now=at(100 + index),
        )
    claimed = store.claim("mac-studio", "w2", [], now=at(200))
    store.record_result(
        storm_id,
        "mac-studio:w2",
        claimed["lease_generation"],
        "storm-parked",
        retryable=0,
        remedy="the input must change",
        now=at(210),
    )

    status = store.status(now=at(300))
    assert status["depth"]["parked"] == 2  # one soft, one terminal
    assert status["parks"] == 1
    assert status["alerts_sent"] == status["parks"], (
        "alerts sent vs parks must read 1:1 (§6) — a soft park in this number "
        "makes a healthy pool look permanently under-alerted"
    )
