"""The pool-wide refusal ledger (§4.2) — store-side behaviour only.

The gate wrapper and the classifier are Lane B's; what is tested here is the
property that makes the ledger shared at all: the ORIGINAL cause survives, the
count is authoritative across machines, a pass DELETES the row, and the storm
park alerts exactly once.
"""

from __future__ import annotations

from omniagentos.workqueue.schema import REFUSAL_STORM_CAP
from tests.workqueue.conftest import at, submit

KEY = "a" * 64
GATE = "unit-acceptance"


def test_no_row_means_proceed(store):
    assert store.refusal_check(KEY, GATE) is None


def test_original_class_is_never_overwritten_by_a_cheap_refusal(store):
    first = store.refusal_record(
        KEY, GATE, "instrument-error", 1, "dirty workspace — git clean the worktree", now=at(0)
    )
    assert first["count"] == 1
    assert first["refusal_class"] == "instrument-error"
    assert first["alert"] is None

    second = store.refusal_record(
        KEY, GATE, "unchanged-retry", 0, "you already asked", unit_id="wq_1", now=at(10)
    )
    # accurate-gate.py:342 — a refusal that blames the code for a dirty
    # workspace sends the next agent to debug the wrong thing, and so does one
    # that forgets what actually broke.
    assert second["count"] == 2
    assert second["refusal_class"] == "instrument-error"
    assert second["remedy"] == "dirty workspace — git clean the worktree"
    assert second["retryable"] == 1
    assert second["first_seen_at"] == at(0)
    assert second["last_seen_at"] == at(10)


def test_storm_cap_parks_and_alerts_exactly_once(store):
    alerts = []
    rows = []
    for index in range(REFUSAL_STORM_CAP + 2):
        row = store.refusal_record(
            KEY, GATE, "unchanged-retry", 0, "change the input", unit_id="wq_1", now=at(index)
        )
        rows.append(row)
        if row["alert"] is not None:
            alerts.append(row["alert"])

    assert [row["parked_at"] for row in rows[: REFUSAL_STORM_CAP - 1]] == [None] * 4
    assert rows[REFUSAL_STORM_CAP - 1]["parked_at"] == at(REFUSAL_STORM_CAP - 1)
    assert len(alerts) == 1, "the one-alert guard is the alerted_at CAS, not a counter"
    assert alerts[0]["count"] == REFUSAL_STORM_CAP
    assert len(store.alerts()) == 1


def test_a_pass_deletes_the_row_so_there_is_no_cached_pass_path(store):
    store.refusal_record(KEY, GATE, "candidate-defect", 0, "fix the code", now=at(0))
    store.refusal_clear(KEY, GATE)
    assert store.refusal_check(KEY, GATE) is None
    # …and a different gate's ledger for the same key is independent.
    store.refusal_record(KEY, "merge-gate", "candidate-defect", 0, "fix the code", now=at(1))
    store.refusal_clear(KEY, GATE)
    assert store.refusal_check(KEY, "merge-gate") is not None


def test_unpark_clears_the_refusal_row_for_the_units_last_input_key_only(store):
    unit_id, _ = store.enqueue(submit("refused"))
    claimed = store.claim("mac-studio", "w1", [], now=at(0))
    store.record_result(
        unit_id,
        "mac-studio:w1",
        claimed["lease_generation"],
        "storm-parked",
        exit_code=2,
        retryable=0,
        input_key=KEY,
        remedy="land the exemption on main first, then re-gate",
        now=at(10),
    )
    store.refusal_record(KEY, GATE, "instrument-error", 1, "exemption not on main", unit_id=unit_id)
    store.refusal_record("b" * 64, GATE, "instrument-error", 1, "someone else's problem")

    store.unpark(unit_id, because="landed devtasks/REACHABILITY-EXEMPT.txt on main")

    assert store.refusal_check(KEY, GATE) is None
    assert store.refusal_check("b" * 64, GATE) is not None, "unparking is not an amnesty"
    unit = store.get_unit(unit_id)
    assert unit["state"] == "queued"
    assert unit["park_remedy"] is None
    assert unit["alerted_at"] is None, "a later park must be able to alert again"


def test_unpark_requires_a_reason(store):
    unit_id, _ = store.enqueue(submit("no-reason"))
    store.park(unit_id, "storm-parked", "because")
    for bad in ("", "   "):
        try:
            store.unpark(unit_id, because=bad)
        except ValueError as error:
            assert "because" in str(error)
        else:  # pragma: no cover - the guard is the point of the test
            raise AssertionError("unpark accepted an empty reason")
