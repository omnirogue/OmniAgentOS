"""Fencing: an adopted worker writes nothing, ever.

"Every fenced write carries the generation the worker believes it holds; a
mismatch means 'you were adopted, stop'" (059_scope_locks.sql). These tests are
the executable form of that sentence, plus the §3.4 case it exists for: a unit
IS safe to reclaim on TTL, because the danger is not two machines running it —
it is two machines both RECORDING a result.
"""

from __future__ import annotations

import pytest

from omniagentos.workqueue.schema import LEASE_TTL_S, LeaseLost
from tests.workqueue.conftest import at, submit


def _claim(store, worker="w1", machine="mac-studio", now=None):
    claimed = store.claim(machine, worker, [], now=now or at(0))
    assert claimed is not None
    return claimed


def test_heartbeat_renews_under_the_right_generation(store):
    unit_id, _ = store.enqueue(submit("hb"))
    claimed = _claim(store)
    store.heartbeat(unit_id, "mac-studio:w1", claimed["lease_generation"], now=at(30))
    assert store.get_unit(unit_id)["lease_expires_at"] == at(30 + LEASE_TTL_S)


def test_stale_generation_heartbeat_raises_lease_lost(store):
    unit_id, _ = store.enqueue(submit("hb-stale"))
    claimed = _claim(store)
    with pytest.raises(LeaseLost):
        store.heartbeat(unit_id, "mac-studio:w1", claimed["lease_generation"] + 1, now=at(30))
    with pytest.raises(LeaseLost):
        store.heartbeat(unit_id, "mw0001-owner:w9", claimed["lease_generation"], now=at(30))


def test_expired_lease_is_reclaimed_and_the_zombie_writes_nothing(store):
    unit_id, _ = store.enqueue(submit("zombie"))
    first = _claim(store, worker="w1", machine="mac-studio")

    # Mac A goes silent. Four missed beats later, Mac B takes the unit.
    reclaim_at = at(LEASE_TTL_S + 1)
    second = store.claim("mw0001-owner", "w2", [], now=reclaim_at)
    assert second is not None
    assert second["unit"]["id"] == unit_id
    assert second["lease_generation"] == first["lease_generation"] + 1
    assert second["attempt"] == 2, "the reclaim allocates a NEW execution ordinal"

    # The reclaim closed the abandoned execution, so the double-execution alarm
    # stays honest instead of reading non-zero forever.
    attempts = store.list_attempts(unit_id)
    assert [row["outcome"] for row in attempts] == ["abandoned", None]
    assert store.status()["double_executions"] == 0

    # Mac A wakes up 20 minutes late and tries to report a pass.
    with pytest.raises(LeaseLost):
        store.record_result(
            unit_id,
            "mac-studio:w1",
            first["lease_generation"],
            "pass",
            exit_code=0,
            now=at(1200),
        )

    unit = store.get_unit(unit_id)
    assert unit["state"] == "claimed"
    assert unit["lease_owner"] == "mw0001-owner:w2"
    assert unit["finished_at"] is None
    assert len(store.list_attempts(unit_id)) == 2, "the zombie wrote no attempt row"

    # Mac B, holding the current generation, reports normally.
    out = store.record_result(
        unit_id, "mw0001-owner:w2", second["lease_generation"], "pass", exit_code=0, now=at(1300)
    )
    assert out["unit"]["state"] == "done"
    assert out["unit"]["terminal_reason"] == "accepted"
    assert out["alert"] is None
    passes = [row for row in store.list_attempts(unit_id) if row["outcome"] == "pass"]
    assert len(passes) == 1


def test_result_after_the_unit_left_the_lease_states_raises(store):
    unit_id, _ = store.enqueue(submit("late"))
    claimed = _claim(store)
    store.record_result(unit_id, "mac-studio:w1", claimed["lease_generation"], "pass", exit_code=0)
    with pytest.raises(LeaseLost):
        store.record_result(
            unit_id, "mac-studio:w1", claimed["lease_generation"], "pass", exit_code=0
        )


def test_sensitive_units_stop_at_review_and_never_auto_land(store):
    unit_id, _ = store.enqueue(submit("sensitive", risk_class="sensitive"))
    claimed = _claim(store)
    out = store.record_result(
        unit_id, "mac-studio:w1", claimed["lease_generation"], "pass", exit_code=0
    )
    assert out["unit"]["state"] == "review"
    assert out["unit"]["terminal_reason"] is None
