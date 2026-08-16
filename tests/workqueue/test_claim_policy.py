"""What a machine may claim: labels, capacity, drain, priority.

Labels declare CAPABILITY, not preference (§5.3): a unit with labels ["gate"]
can only be claimed by a machine that declares "gate". A unit no enrolled machine
can satisfy is surfaced, never left silently queued.
"""

from __future__ import annotations

from tests.workqueue.conftest import at, submit


def _machine(store, machine_id, labels, max_concurrent=1, **extra):
    store.enroll_machine(
        {
            "machine_id": machine_id,
            "hostname": f"{machine_id}.local",
            "os": "darwin",
            "labels": labels,
            "max_concurrent": max_concurrent,
            **extra,
        }
    )


def test_a_machine_must_declare_every_label_the_unit_asks_for(store):
    store.enqueue(submit("needs-gate", labels=["gate", "build"]))

    assert store.claim("mw0002", "w1", ["build"], now=at(0)) is None
    claimed = store.claim("mw0001-owner", "w1", ["build", "gate", "pytest"], now=at(1))
    assert claimed is not None
    assert claimed["unit"]["labels"] == ["gate", "build"]


def test_unlabelled_units_are_claimable_by_anyone(store):
    store.enqueue(submit("anyone"))
    assert store.claim("any-machine", "w1", [], now=at(0)) is not None


def test_priority_then_age_orders_the_queue(store):
    store.enqueue(submit("normal-first"))
    store.enqueue(submit("normal-second"))
    store.enqueue(submit("urgent", priority=0))

    order = []
    for cycle in range(3):
        claimed = store.claim("mac-studio", f"w{cycle}", [], now=at(cycle))
        assert claimed is not None
        order.append(claimed["unit"]["idempotency_key"])

    # priority 0 is highest and always wins. Within one priority the order is
    # (created_at, id) — and created_at has 1 s resolution, so units enqueued in
    # the same second are ordered by ULID, which is ms-sortable but random
    # inside a millisecond. FIFO is a promise at second granularity, not below
    # it; asserting otherwise would be asserting a coin flip.
    assert order[0] == "urgent"
    assert set(order[1:]) == {"normal-first", "normal-second"}


def test_drain_stops_new_claims_without_breaking_a_live_lease(store):
    store.enqueue(submit("d1"))
    store.enqueue(submit("d2"))
    _machine(store, "mw0001-owner", ["build"], max_concurrent=2)

    first = store.claim("mw0001-owner", "w1", ["build"], now=at(0))
    assert first is not None

    store.set_drain("mw0001-owner", True)
    assert store.claim("mw0001-owner", "w2", ["build"], now=at(1)) is None
    # Nothing was force-reclaimed: the in-flight lease is untouched.
    assert store.get_unit(first["unit"]["id"])["lease_owner"] == "mw0001-owner:w1"
    assert store.machine_beat("mw0001-owner", {"load1": 1.2}, now=at(2)) == {"drain": 1}

    store.set_drain("mw0001-owner", False)
    assert store.claim("mw0001-owner", "w2", ["build"], now=at(3)) is not None


def test_max_concurrent_is_the_capacity_authority(store):
    for index in range(3):
        store.enqueue(submit(f"cap-{index}"))
    _machine(store, "mac-studio", [], max_concurrent=2)

    assert store.claim("mac-studio", "w1", [], now=at(0)) is not None
    assert store.claim("mac-studio", "w2", [], now=at(1)) is not None
    assert store.claim("mac-studio", "w3", [], now=at(2)) is None

    # A second machine is unaffected by the first machine's ceiling.
    _machine(store, "mw0002", [], max_concurrent=1)
    assert store.claim("mw0002", "w1", [], now=at(3)) is not None


def test_machine_beat_records_capacity_telemetry(store):
    _machine(
        store,
        "initech-roi-calculator",
        ["linux"],
        max_concurrent=2,
        os="linux",
        ncpu=16,
        perf_cores=16,
        mem_gb=125.0,
        ceiling_fraction=0.6,
    )
    out = store.machine_beat(
        "initech-roi-calculator",
        {
            "load1": 3.5,
            "load5": 2.1,
            "mem_free_gb": 90.5,
            "worker_id": "initech-roi-calculator:4242:ab12cd34",
            "pid": 4242,
            "current_unit_id": None,
        },
        now=at(5),
    )
    assert out == {"drain": 0}

    machines = store.list_machines()
    assert len(machines) == 1
    row = machines[0]
    assert row["ncpu"] == 16
    assert row["ceiling_fraction"] == 0.6
    assert row["last_load1"] == 3.5
    assert row["last_seen_at"] == at(5)
    assert row["in_flight"] == 0

    # An unknown machine gets a truthful "not draining" and no phantom row.
    assert store.machine_beat("ghost", {"load1": 0.1}) == {"drain": 0}
    assert len(store.list_machines()) == 1


def test_re_enrollment_preserves_drain_and_enrolment_time(store):
    _machine(store, "mac-studio", ["build"], max_concurrent=2)
    store.set_drain("mac-studio", True)
    enrolled_at = store.list_machines()[0]["enrolled_at"]

    _machine(store, "mac-studio", ["build", "gate"], max_concurrent=3)
    row = store.list_machines()[0]
    assert row["drain"] == 1, "re-running enroll.sh must not silently undrain a box"
    assert row["enrolled_at"] == enrolled_at
    assert row["labels"] == ["build", "gate"]
    assert row["max_concurrent"] == 3
