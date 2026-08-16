from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines import RoutineValidationError, should_fire
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store


def test_create_and_get_routine(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    assert created["id"].startswith("rtn_")
    assert created["status"] == "active"
    assert created["trigger_config"] == {"cron": "0 3 * * *"}
    assert created["gate_config"]["command"] == "git diff --check"
    assert created["total_runs"] == 0
    assert created["acceptance_rate"] is None
    assert created["revision"] == 0

    fetched = routines.get_routine(created["id"])
    assert fetched == created


def test_get_missing_routine_returns_none(routines: RoutinesStore) -> None:
    assert routines.get_routine("rtn_does_not_exist") is None


def test_create_rejects_routine_missing_gate(routines: RoutinesStore) -> None:
    payload = valid_routine_payload()
    del payload["gate_type"]
    with pytest.raises(RoutineValidationError):
        routines.create_routine(payload)
    assert routines.list_routines() == []


def test_create_rejects_routine_missing_hard_cap(routines: RoutinesStore) -> None:
    payload = valid_routine_payload()
    del payload["hard_cap_type"]
    with pytest.raises(RoutineValidationError):
        routines.create_routine(payload)
    assert routines.list_routines() == []


def test_create_disabled_draft_omitting_engine_fields(routines: RoutinesStore) -> None:
    """LOOPS1-E2: store persists a field-sparse disabled draft."""
    created = routines.create_routine({"name": "store-draft", "status": "disabled"})
    assert created["status"] == "disabled"
    assert created["name"] == "store-draft"
    assert len(routines.list_routines()) == 1


def test_update_activate_without_engine_fields_rejected(routines: RoutinesStore) -> None:
    """No exemption leak on activate-via-update (merged status re-validated)."""
    draft = routines.create_routine({"name": "promote-me", "status": "disabled"})
    with pytest.raises(RoutineValidationError):
        routines.update_routine(draft["id"], {"status": "active"})
    assert routines.get_routine(draft["id"])["status"] == "disabled"  # type: ignore[index]


def test_create_round_trips_scope_purpose(routines: RoutinesStore) -> None:
    created = routines.create_routine(
        valid_routine_payload(name="scoped", scope="project", purpose="execution")
    )
    assert created["scope"] == "project"
    assert created["purpose"] == "execution"


def test_list_routines_filters_by_status(routines: RoutinesStore) -> None:
    active = routines.create_routine(valid_routine_payload(name="a"))
    routines.create_routine(valid_routine_payload(name="b", status="disabled"))
    assert [r["id"] for r in routines.list_routines(status="active")] == [active["id"]]
    assert len(routines.list_routines()) == 2


def test_update_routine_merges_and_revalidates(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    updated = routines.update_routine(created["id"], {"description": "updated desc"})
    assert updated is not None
    assert updated["description"] == "updated desc"
    # Untouched required fields survive the merge.
    assert updated["gate_type"] == "exit_code"

    # Clearing the gate type on update is rejected (merged payload re-validated).
    with pytest.raises(RoutineValidationError):
        routines.update_routine(created["id"], {"gate_type": "not-a-real-gate"})
    # The bad update must NOT have been persisted.
    assert routines.get_routine(created["id"])["gate_type"] == "exit_code"  # type: ignore[index]


def test_same_second_updates_receive_distinct_revisions(
    routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omniagentos.scheduler.store.utc_now_iso",
        lambda: "2026-07-31T12:00:00Z",
    )
    created = routines.create_routine(valid_routine_payload(name="same-second"))
    first = routines.update_routine(created["id"], {"description": "first"})
    second = routines.update_routine(created["id"], {"description": "second"})

    assert first is not None
    assert second is not None
    assert created["revision"] == 0
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert created["updated_at"] == first["updated_at"] == second["updated_at"]


def test_store_instance_revision_cas_allows_exactly_one_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "multi-store.db"
    first_store = make_store(SqliteStore, db_path)
    second_store = SqliteStore(str(db_path))
    try:
        first = RoutinesStore(first_store)
        second = RoutinesStore(second_store)
        created = first.create_routine(valid_routine_payload(name="multi-store", status="disabled"))
        expected = created["revision"]

        def update(store: RoutinesStore, description: str) -> dict[str, object] | None:
            return store.update_routine_cas(
                created["id"],
                {"description": description},
                expected_revision=expected,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: update(*pair),
                    [(first, "first"), (second, "second")],
                )
            )

        assert sum(result is not None for result in results) == 1
        winner = first.get_routine(created["id"])
        assert winner is not None
        assert winner["revision"] == expected + 1
        assert winner["description"] in {"first", "second"}
    finally:
        second_store.close()
        first_store.close()


def test_process_revision_cas_allows_exactly_one_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "multi-process.db"
    database = make_store(SqliteStore, db_path)
    try:
        routines = RoutinesStore(database)
        created = routines.create_routine(
            valid_routine_payload(name="multi-process", status="disabled")
        )
        start_at = time.time() + 1.0
        worker = (
            "import sys,time\n"
            "from omniagentos.db.store import SqliteStore\n"
            "from omniagentos.scheduler.store import RoutinesStore\n"
            "db,rid,description,start=sys.argv[1:]\n"
            "store=SqliteStore(db)\n"
            "routines=RoutinesStore(store)\n"
            "revision=routines.get_routine(rid)['revision']\n"
            "time.sleep(max(0.0,float(start)-time.time()))\n"
            "result=routines.update_routine_cas("
            "rid,{'description':description},expected_revision=revision)\n"
            "print('won' if result is not None else 'lost',flush=True)\n"
            "store.close()\n"
        )

        def run(description: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(db_path),
                    created["id"],
                    description,
                    str(start_at),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["first", "second"]))

        assert [result.returncode for result in results] == [0, 0]
        assert sorted(result.stdout.strip() for result in results) == ["lost", "won"]
        winner = routines.get_routine(created["id"])
        assert winner is not None
        assert winner["revision"] == created["revision"] + 1
    finally:
        database.close()


def test_update_missing_routine_returns_none(routines: RoutinesStore) -> None:
    assert routines.update_routine("rtn_missing", {"description": "x"}) is None


def test_enable_disable_toggle(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    disabled = routines.set_status(created["id"], "disabled")
    assert disabled is not None
    assert disabled["status"] == "disabled"
    assert disabled["revision"] == created["revision"] + 1
    enabled = routines.set_status(created["id"], "active")
    assert enabled is not None
    assert enabled["status"] == "active"
    assert enabled["revision"] == disabled["revision"] + 1


def test_delete_routine(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    assert routines.delete_routine(created["id"]) is True
    assert routines.get_routine(created["id"]) is None
    assert routines.delete_routine(created["id"]) is False


def test_record_run_updates_rollups(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"],
        {
            "iteration": 1,
            "gate_passed": True,
            "accepted": True,
            "cost_usd": 2.0,
            "stop_reason": "gate_met",
        },
    )
    assert run["accepted"] is True
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_runs"] == 1
    assert updated["accepted_runs"] == 1
    assert updated["acceptance_rate"] == pytest.approx(1.0)
    assert updated["total_cost_usd"] == pytest.approx(2.0)
    assert updated["cost_per_accepted_change"] == pytest.approx(2.0)
    assert updated["status"] == "active"
    assert updated["revision"] == created["revision"] + 1


# --- ISSUE-8: unknown cost must poison total_cost_usd to NULL, never $0.00 ---


def test_settle_run_with_unknown_cost_writes_null_total_cost(routines: RoutinesStore) -> None:
    """A dispatched run whose provider never reported a cost is genuinely
    UNKNOWN, not free. `settle_run(cost_unknown=True)` is the signal
    `routines_settle.py` sends when `runs.cost_usd is None`; the routine's
    rollup must become NULL, never silently add a zero delta."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"},
    )
    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_unknown=True,
    )
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None
    assert updated["cost_per_accepted_change"] is None


def test_settle_run_with_known_cost_after_unknown_stays_poisoned(routines: RoutinesStore) -> None:
    """Once a routine's rollup is unknown, a LATER run with a real, known cost
    must not resurrect a number: averaging a known figure into an unknown
    total is still unknown. No backfill, no silent healing."""
    created = routines.create_routine(valid_routine_payload())
    first = routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        first["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_unknown=True,
    )
    second = routines.record_run(
        created["id"], {"iteration": 2, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        second["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:02:00Z",
        cost_usd=3.0,
    )
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None
    assert updated["cost_per_accepted_change"] is None


def test_record_run_after_poisoned_total_keeps_it_null(routines: RoutinesStore) -> None:
    """`record_run` reads the CURRENT rollup and adds this run's own (always
    known, API-required) cost on top. If the rollup it reads is already
    poisoned NULL, the new total must stay NULL — never resurrect a number
    from a running total that has already lost precision."""
    created = routines.create_routine(valid_routine_payload())
    first = routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        first["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_unknown=True,
    )
    poisoned = routines.get_routine(created["id"])
    assert poisoned is not None
    assert poisoned["total_cost_usd"] is None

    routines.record_run(
        created["id"],
        {"iteration": 2, "gate_passed": True, "accepted": True, "cost_usd": 5.0, "stop_reason": ""},
    )
    after = routines.get_routine(created["id"])
    assert after is not None
    assert after["total_cost_usd"] is None
    assert after["cost_per_accepted_change"] is None


def test_settle_run_known_cost_still_computes_a_real_total(routines: RoutinesStore) -> None:
    """Sanity check that the new `cost_unknown` parameter is opt-in and does
    not disturb the ordinary known-cost path (regression guard)."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_usd=1.5,
    )
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] == pytest.approx(1.5)
    assert updated["cost_per_accepted_change"] == pytest.approx(1.5)


# --- Sol review, seam 1: record_run's own cost_usd=None must poison too -----


def test_record_run_with_none_cost_usd_poisons_both_child_and_parent(
    routines: RoutinesStore,
) -> None:
    """The public POST /routines/{id}/runs route can genuinely not know a
    run's cost — the request model no longer defaults an omitted cost_usd to
    0.0. record_run must preserve that None as unknown all the way through:
    the CHILD row's own cost_usd (migration 120) and the PARENT rollup both
    land on NULL, never a manufactured $0.00."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )
    assert run["cost_usd"] is None

    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None
    assert updated["cost_per_accepted_change"] is None

    listed = routines.list_runs(created["id"])
    assert listed[0]["cost_usd"] is None


def test_record_run_with_none_cost_usd_poisons_a_previously_known_total(
    routines: RoutinesStore,
) -> None:
    """A KNOWN total must not survive a later run recorded with no cost at
    all — the same "once unknown, stays unknown" rule settle_run enforces."""
    created = routines.create_routine(valid_routine_payload())
    first = routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 2.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        first["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_usd=2.0,
    )
    known = routines.get_routine(created["id"])
    assert known is not None
    assert known["total_cost_usd"] == pytest.approx(2.0)

    routines.record_run(
        created["id"],
        {"iteration": 2, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )
    after = routines.get_routine(created["id"])
    assert after is not None
    assert after["total_cost_usd"] is None
    assert after["cost_per_accepted_change"] is None


def test_record_run_omitted_cost_usd_key_also_reads_as_unknown(routines: RoutinesStore) -> None:
    """A caller that omits the ``cost_usd`` key entirely (not just an
    explicit ``None``) must be treated identically — the HTTP request model's
    own default is what actually produces this shape in production."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(created["id"], {"iteration": 1, "stop_reason": "", "notes": "fired"})
    assert run["cost_usd"] is None
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None


def test_record_run_with_explicit_zero_cost_is_still_a_known_exact_zero(
    routines: RoutinesStore,
) -> None:
    """Regression guard: an explicit ``0.0`` (a caller that DOES know the run
    was free) must not be treated as unknown — only a missing/None value is."""
    created = routines.create_routine(valid_routine_payload())
    routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] == pytest.approx(0.0)


# --- Sol review, seam 2: settle_run(cost_unknown=True) must poison the ------
# --- CHILD row (routine_runs.cost_usd) too, not just the parent rollup -----


def test_settle_run_cost_unknown_writes_null_to_the_child_row_too(
    routines: RoutinesStore,
) -> None:
    """Before migration 120, routine_runs.cost_usd was NOT NULL DEFAULT 0, so
    a cost_unknown settlement poisoned the PARENT rollup but left the CHILD
    audit row at its provisional $0.00 — list_runs/list_recent_runs (and the
    dashboard's RecentRunsPanel) kept serving an exact, manufactured zero for
    that specific run, straight from the audit trail."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"},
    )
    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_unknown=True,
    )
    listed = routines.list_runs(created["id"])
    assert listed[0]["cost_usd"] is None


def test_settle_run_no_op_call_inherits_an_already_unknown_child_row(
    routines: RoutinesStore,
) -> None:
    """A settlement that says nothing new about cost (cost_usd=None,
    cost_unknown=False — e.g. only a gate verdict) must not silently
    resurrect a row whose cost was ALREADY unknown at record time (seam 1)."""
    created = routines.create_routine(valid_routine_payload())
    run = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )
    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
    )
    listed = routines.list_runs(created["id"])
    assert listed[0]["cost_usd"] is None
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None


# --- Coordinator/Sol review: a temporarily-unknown cost must be able to -----
# --- RECOVER once it becomes known, not stay poisoned forever --------------


def test_settle_run_recovers_the_rollup_when_this_was_the_last_unknown_child(
    routines: RoutinesStore,
) -> None:
    """MEDIUM regression fix. Before this fix: record a run with cost_usd
    omitted (parent+child correctly NULL — unknown, not free) -> settle that
    SAME run with a real cost_usd -> the child correctly became known, but
    the PARENT rollup stayed NULL forever, because settle_run's "once
    poisoned, stays poisoned" rule didn't distinguish "still genuinely
    unknown" from "was unknown, is now resolved". A budget_usd-capped
    routine would then be blocked as "cost unknown" PERMANENTLY, even once
    its true, comfortably-under-budget cost was known. The fix: recover the
    rollup when settlement resolves the LAST remaining unknown child by
    recomputing the total from every child row's own cost_usd."""
    created = routines.create_routine(
        valid_routine_payload(
            hard_cap_type="budget_usd",
            hard_cap_value=10.0,
            trigger_config={"cron": "* * * * *"},
        )
    )
    run = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )
    unknown = routines.get_routine(created["id"])
    assert unknown is not None
    assert unknown["total_cost_usd"] is None

    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_usd=2.0,
    )

    listed = routines.list_runs(created["id"])
    assert listed[0]["cost_usd"] == pytest.approx(2.0)

    recovered = routines.get_routine(created["id"])
    assert recovered is not None
    assert recovered["total_cost_usd"] == pytest.approx(2.0)
    assert recovered["cost_per_accepted_change"] == pytest.approx(2.0)

    # End to end: the budget_usd cap must fire normally again now that the
    # true, comfortably-under-budget cost is known — never "unknown" forever.
    fire, reason = should_fire(recovered, now=datetime(2026, 8, 1, tzinfo=UTC))
    assert fire is True, reason


def test_settle_run_does_not_recover_while_another_child_is_still_unknown(
    routines: RoutinesStore,
) -> None:
    """Recovery is conditional on this being the LAST unknown child. A
    second, still-pending (not yet settled) run recorded with an unknown
    cost must keep the rollup NULL even after the first run resolves —
    the routine's TRUE total is still not fully knowable."""
    created = routines.create_routine(valid_routine_payload())
    first = routines.record_run(
        created["id"],
        {"iteration": 1, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )
    routines.record_run(
        created["id"],
        {"iteration": 2, "cost_usd": None, "stop_reason": "", "notes": "fired"},
    )

    routines.settle_run(
        first["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_usd=2.0,
    )

    still_poisoned = routines.get_routine(created["id"])
    assert still_poisoned is not None
    assert still_poisoned["total_cost_usd"] is None
    assert still_poisoned["cost_per_accepted_change"] is None


def test_settle_run_permanently_unknown_cost_unknown_child_never_recovers(
    routines: RoutinesStore,
) -> None:
    """Regression guard: `cost_unknown=True` is a PERMANENT assertion about
    that row (settle_run refuses to settle the same row twice), so a LATER
    run reporting a known cost must not recover a total that a
    cost_unknown=True settlement poisoned — same assertion
    test_settle_run_with_known_cost_after_unknown_stays_poisoned already
    pins; repeated here to make the "when does recovery NOT happen" contract
    explicit next to the new recovery test above."""
    created = routines.create_routine(valid_routine_payload())
    first = routines.record_run(
        created["id"], {"iteration": 1, "cost_usd": 0.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        first["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
        cost_unknown=True,
    )
    second = routines.record_run(
        created["id"], {"iteration": 2, "cost_usd": 4.0, "stop_reason": "", "notes": "fired"}
    )
    routines.settle_run(
        second["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:02:00Z",
        cost_usd=4.0,
    )
    updated = routines.get_routine(created["id"])
    assert updated is not None
    assert updated["total_cost_usd"] is None
    assert updated["cost_per_accepted_change"] is None


def test_every_parent_routine_mutation_increments_revision(
    routines: RoutinesStore,
) -> None:
    created = routines.create_routine(valid_routine_payload(name="revision-writes"))
    assert created["revision"] == 0

    fired = routines.record_fired(created["id"], "2026-07-31T12:00:00Z")
    assert fired is not None
    assert fired["revision"] == 1

    run = routines.record_run(
        created["id"],
        {
            "iteration": 1,
            "accepted": None,
            "cost_usd": 0,
        },
    )
    after_record = routines.get_routine(created["id"])
    assert after_record is not None
    assert after_record["revision"] == 2

    routines.settle_run(
        run["id"],
        gate_passed=True,
        accepted=True,
        finished_at="2026-07-31T12:01:00Z",
    )
    after_settle = routines.get_routine(created["id"])
    assert after_settle is not None
    assert after_settle["revision"] == 3

    disabled = routines.set_status(created["id"], "disabled")
    assert disabled is not None
    assert disabled["revision"] == 4

    updated = routines.update_routine(created["id"], {"description": "changed"})
    assert updated is not None
    assert updated["revision"] == 5


def test_record_run_against_missing_routine_raises(routines: RoutinesStore) -> None:
    with pytest.raises(ValueError):
        routines.record_run("rtn_missing", {"accepted": True, "cost_usd": 1.0})


def _settled(iteration: int, accepted: bool, **overrides: object) -> dict[str, object]:
    """One run in the shape production settles it: a gate verdict AND a finish
    stamp. Only such runs count toward the acceptance floor — a run missing
    either is still pending and carries no acceptance signal."""
    run: dict[str, object] = {
        "iteration": iteration,
        "gate_passed": accepted,
        "accepted": accepted,
        "cost_usd": 1.0,
        "stop_reason": "gate_passed" if accepted else "gate_failed",
        "finished_at": f"2026-01-01T09:0{iteration}:00Z",
    }
    run.update(overrides)
    return run


def test_auto_pause_trips_below_50pct_acceptance(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    # 3 settled runs, only 1 accepted -> 33% acceptance, below the 50% floor at
    # the AUTO_PAUSE_MIN_RUNS=3 sample size.
    routines.record_run(routine_id, _settled(1, True))
    routines.record_run(routine_id, _settled(2, False))
    routines.record_run(routine_id, _settled(3, False))

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "auto_paused"
    assert routine["acceptance_rate"] == pytest.approx(1 / 3)
    assert "50%" in routine["auto_pause_reason"]


def test_auto_pause_does_not_trip_above_floor(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    # 3 settled runs, 2 accepted -> 67%, clear of the floor with the minimum
    # sample already reached (so this exercises the branch, not the sample gate).
    routines.record_run(routine_id, _settled(1, True))
    routines.record_run(routine_id, _settled(2, True))
    routines.record_run(routine_id, _settled(3, False))

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "active"
    assert routine["auto_pause_reason"] == ""
    assert routine["acceptance_rate"] == pytest.approx(2 / 3)


def test_ungateable_settlements_never_trip_the_acceptance_floor(
    routines: RoutinesStore,
) -> None:
    """I-0: routines on a host with no gate workspace settle every run
    ``gate_evidence_unavailable``. That says the gate could not rule, not that
    the routine produced bad work, so it must never reach the floor — counting
    it as a rejection is what silently suppressed both production routines.
    Settlement writes those rows gate_passed=NULL today; the floor also has to
    hold for the rejection-shaped rows written before that convention."""
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    for iteration in range(1, 5):
        routines.record_run(
            routine_id,
            _settled(iteration, False, stop_reason="gate_evidence_unavailable"),
        )

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "active"
    assert routine["auto_pause_reason"] == ""


def test_ungateable_settlements_do_not_mask_genuine_rejections(
    routines: RoutinesStore,
) -> None:
    """Bucketing un-gateable runs apart must not blunt the floor: the three
    runs the gate DID rule on still decide it."""
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    routines.record_run(routine_id, _settled(1, False, stop_reason="gate_evidence_unavailable"))
    routines.record_run(routine_id, _settled(2, True))
    routines.record_run(routine_id, _settled(3, False))
    assert routines.get_routine(routine_id)["status"] == "active"  # type: ignore[index]

    routines.record_run(routine_id, _settled(4, False))

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "auto_paused"
    assert "over the last 3 settled runs" in routine["auto_pause_reason"]


def test_a_refused_gate_still_counts_as_a_rejection(routines: RoutinesStore) -> None:
    """The un-gateable bucket is absence of a verdict only. A gate that ran and
    REFUSED did rule on the run, so it stays a rejection and still trips the
    floor (settle_routine_run settles refusals gate_passed=False for this
    reason)."""
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    for iteration in range(1, 4):
        routines.record_run(routine_id, _settled(iteration, False, stop_reason="gate_refused"))

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "auto_paused"
    assert "50%" in routine["auto_pause_reason"]


def test_settle_run_flips_status_on_a_rejected_settlement(routines: RoutinesStore) -> None:
    """I-6: the floor re-check used to run only on an ACCEPTED transition, so a
    routine whose pending runs all settled rejected stopped firing (should_fire
    reads the same counts) while still reporting status='active' with an empty
    auto_pause_reason."""
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    pending = [
        routines.record_run(routine_id, {"iteration": i, "run_id": f"run-{i}", "cost_usd": 0.0})
        for i in range(1, 4)
    ]
    assert routines.get_routine(routine_id)["status"] == "active"  # type: ignore[index]

    for i, run in enumerate(pending, start=1):
        routines.settle_run(
            run["id"],
            gate_passed=False,
            accepted=False,
            finished_at=f"2026-01-01T09:0{i}:00Z",
            stop_reason="gate_failed",
        )

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "auto_paused"
    assert "50%" in routine["auto_pause_reason"]


def test_settle_run_ungateable_settlements_leave_the_routine_active(
    routines: RoutinesStore,
) -> None:
    """The I-6 re-check must read the same buckets as the fire-time check:
    settling every pending run ``gate_evidence_unavailable`` is not a reason to
    pause, and must not invent an auto_pause_reason either."""
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    pending = [
        routines.record_run(routine_id, {"iteration": i, "run_id": f"run-{i}", "cost_usd": 0.0})
        for i in range(1, 5)
    ]

    for i, run in enumerate(pending, start=1):
        routines.settle_run(
            run["id"],
            gate_passed=False,
            accepted=False,
            finished_at=f"2026-01-01T09:0{i}:00Z",
            stop_reason="gate_evidence_unavailable",
        )

    routine = routines.get_routine(routine_id)
    assert routine is not None
    assert routine["status"] == "active"
    assert routine["auto_pause_reason"] == ""


def test_list_runs_returns_newest_first(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    routine_id = created["id"]
    routines.record_run(routine_id, {"iteration": 1, "accepted": True, "cost_usd": 1.0})
    routines.record_run(routine_id, {"iteration": 2, "accepted": True, "cost_usd": 1.0})
    runs = routines.list_runs(routine_id)
    assert [r["iteration"] for r in runs] == [2, 1]


def test_list_recent_runs_joins_routine_name(routines: RoutinesStore) -> None:
    """``list_recent_runs`` is the cross-routine aggregate (section B contract):
    each row carries the parent routine's name alongside the run fields."""
    a = routines.create_routine(valid_routine_payload(name="loop-a"))
    b = routines.create_routine(
        valid_routine_payload(name="loop-b", trigger_config={"cron": "0 4 * * *"})
    )
    routines.record_run(
        a["id"],
        {
            "iteration": 1,
            "gate_passed": True,
            "accepted": True,
            "cost_usd": 0.5,
            "run_id": "run_a1",
            "finished_at": "2025-01-01T10:00:00Z",
        },
    )
    routines.record_run(
        b["id"],
        {
            "iteration": 1,
            "gate_passed": False,
            "accepted": False,
            "cost_usd": 1.5,
            "run_id": "run_b1",
            "finished_at": "2025-01-02T10:00:00Z",
        },
    )
    routines.record_run(
        a["id"],
        {
            "iteration": 2,
            "gate_passed": True,
            "accepted": True,
            "cost_usd": 0.25,
            "run_id": "run_a2",
            "finished_at": "2025-01-03T10:00:00Z",
        },
    )

    recent = routines.list_recent_runs(limit=10)
    assert len(recent) == 3
    # Newest first.
    assert [r["routine_name"] for r in recent] == ["loop-a", "loop-b", "loop-a"]
    assert [r["routine_id"] for r in recent] == [a["id"], b["id"], a["id"]]
    assert [r["run_id"] for r in recent] == ["run_a2", "run_b1", "run_a1"]
    assert [r["accepted"] for r in recent] == [True, False, True]
    assert [r["gate_passed"] for r in recent] == [True, False, True]


def test_list_recent_runs_limit_is_enforced(routines: RoutinesStore) -> None:
    created = routines.create_routine(valid_routine_payload())
    for i in range(5):
        routines.record_run(created["id"], {"iteration": i + 1, "accepted": True, "cost_usd": 0.1})
    assert len(routines.list_recent_runs(limit=2)) == 2
    assert len(routines.list_recent_runs(limit=100)) == 5


def test_list_recent_runs_empty_when_no_runs(routines: RoutinesStore) -> None:
    routines.create_routine(valid_routine_payload())
    assert routines.list_recent_runs() == []


def test_list_recent_runs_null_gate_and_accepted_coerce_to_none(
    routines: RoutinesStore,
) -> None:
    """Runs created without a settled gate/accepted must surface as None, not
    0 or False."""
    created = routines.create_routine(valid_routine_payload())
    routines.record_run(
        created["id"],
        {"iteration": 1, "gate_passed": None, "accepted": None, "cost_usd": 0.0},
    )
    recent = routines.list_recent_runs()
    assert len(recent) == 1
    assert recent[0]["gate_passed"] is None
    assert recent[0]["accepted"] is None
