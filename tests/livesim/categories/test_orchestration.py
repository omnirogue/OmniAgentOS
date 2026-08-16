"""LiveSim: orchestration — swarm runs/attempts, routines, runner heartbeat,
spawn queue, approvals producer, loops.

Live surfaces exercised (all read-only):

  * GET /api/health          — runner worker heartbeat (`worker.alive`, `last_beat_at`)
  * GET /api/approvals       — the approvals producer's pending queue
  * live DB (mode=ro)        — swarm_runs / swarm_attempts (partial unique index
    idx_swarm_attempts_live), routines / routine_runs (settlement + acceptance
    counters), session_spawn_queue, loop_reservations / loop_settings, sessions.

The one destructive check (does the partial unique index really reject a second
live attempt for the same board task?) runs against a FRESH scratch DB built
from the live schema's own DDL — never the live DB, and never a 589MB copy.

LIVESIM-REPAIR-PLAN.md T-1/T-2 correction (2026-08-06): two "defects" this
suite used to document here were TEST bugs, not product bugs, and have been
fixed rather than removed:

  * "Acceptance-rate drift" (LS-015): the test compared
    routines.acceptance_rate against accepted_runs/total_runs, but the product
    contract (`omniagentos.scheduler.routines.acceptance_rate`) defines the
    denominator as JUDGED runs — total_runs minus the persisted neutral_runs
    counter — precisely so a routine that parks/idles doesn't get its rate
    depressed by non-results. Using the wrong (naive) denominator manufactured
    an apparent ~0.016 drift on the two high-volume routines that isn't there;
    accepted_runs/(total_runs-neutral_runs) matches the stored rate exactly
    (0.679725759059745 for improve-lane-dispatcher, bit for bit).
  * "912 unsettled routine_runs" (LS-016): the test counted every row with
    `finished_at IS NOT NULL AND accepted IS NULL` as an unsettled leak, but
    the product's own outcome taxonomy (`classify_run_outcome`) classifies a
    row with a NEUTRAL stop_reason (`loop_parked_awaiting_human`,
    `gate_evidence_unavailable`, ...) or with both gate_passed and accepted
    NULL as NEUTRAL by design (evidence-absence, not a leak) and deliberately
    excludes it from the acceptance floor's denominator. Every one of the
    "unsettled" rows classifies as NEUTRAL under that taxonomy; there are zero
    rows where a gate actually ruled (`gate_passed IS NOT NULL`) and
    `accepted` was still never settled with a non-neutral stop_reason.

Both are now regression guards for the CORRECT behaviour (test_routine_
acceptance_rate_matches_judged_denominator, test_routine_unsettled_runs_are_
all_neutral_classified) rather than defect-documenting assertions of the
observed-but-wrong behaviour.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.scheduler import loop_budget

pytestmark = pytest.mark.livesim

APPROVAL_STATES = {"pending", "approved", "rejected", "expired"}  # contracts.ApprovalState
SPAWN_STATES = {"queued", "launched", "failed"}  # session_spawn_queue CHECK constraint
SWARM_RUN_STATES = {"queued", "planning", "running", "merging", "completed", "failed", "cancelled"}
TERMINAL_SESSION_STATES = ("completed", "failed", "killed")


def _parse_ts(value: str) -> datetime:
    """Parse the runtime DB / API timestamp shapes ('...Z' or naive ISO) as UTC."""
    v = value.strip().replace(" ", "T")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Runner heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_runner_heartbeat_fresh(livesim, live_api):
    """The runner worker is alive and its heartbeat is fresh (within minutes).

    /api/health carries worker.alive + worker.last_beat_at; a stale beat means
    the tick loop is wedged even if the API answers.
    """
    livesim.target("api")
    status, body, _ = live_api.get("/api/health")
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    livesim.record(inputs={"path": "/api/health"}, outputs=body)
    assert status == 200
    assert isinstance(body, dict) and body.get("db") is True
    worker = body.get("worker") or {}
    assert worker.get("alive") is True, f"runner worker not alive: {worker}"
    last_beat = worker.get("last_beat_at")
    assert isinstance(last_beat, str) and last_beat, "worker.last_beat_at missing"
    age_s = (_now() - _parse_ts(last_beat)).total_seconds()
    livesim.extra(heartbeat_age_s=round(age_s, 1))
    livesim.note(f"runner heartbeat age: {age_s:.1f}s")
    # Structural freshness bound, generous vs the tick cadence (seconds):
    assert -120 <= age_s <= 15 * 60, f"heartbeat stale: last_beat_at={last_beat} ({age_s:.0f}s ago)"


# ---------------------------------------------------------------------------
# Routines: recent firing + counter/settlement invariants
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_routines_fired_recently(livesim, live_db_ro):
    """The routine scheduler is actually firing: at least one active routine
    exists and the newest routine_run started within the last 48h."""
    livesim.target("db")
    active = live_db_ro.execute(
        "SELECT COUNT(*) n FROM routines WHERE status='active'"
    ).fetchone()["n"]
    row = live_db_ro.execute(
        "SELECT MAX(started_at) m, COUNT(*) n FROM routine_runs WHERE started_at IS NOT NULL"
    ).fetchone()
    livesim.record(
        inputs={"query": "max(routine_runs.started_at), count(routines active)"},
        outputs={"active_routines": active, "latest_run_started_at": row["m"], "total_runs": row["n"]},
    )
    if row["n"] == 0:
        pytest.skip("no routine_runs recorded yet — cannot judge firing recency")
    assert active >= 1, "no active routines — scheduler has nothing to fire"
    age_h = (_now() - _parse_ts(row["m"])).total_seconds() / 3600.0
    livesim.extra(latest_routine_run_age_h=round(age_h, 2))
    assert age_h <= 48.0, f"newest routine_run is {age_h:.1f}h old — scheduler looks stalled"


@pytest.mark.boundary
def test_routine_ledger_invariants(livesim, live_db_ro):
    """Stable settlement invariants that DO hold on the live ledger:
    accepted_runs <= total_runs, total_runs == count(routine_runs),
    acceptance_rate within [0,1] when set, and no run marked accepted=1
    whose gate_passed=0 (the acceptance floor's core soundness)."""
    livesim.target("db")
    routines = live_db_ro.execute(
        "SELECT r.id, r.name, r.status, r.total_runs, r.accepted_runs, r.acceptance_rate,"
        " (SELECT COUNT(*) FROM routine_runs rr WHERE rr.routine_id=r.id) rr_count"
        " FROM routines r"
    ).fetchall()
    accepted_without_gate = live_db_ro.execute(
        "SELECT COUNT(*) n FROM routine_runs WHERE accepted=1 AND gate_passed=0"
    ).fetchone()["n"]
    summary = [dict(r) for r in routines]
    livesim.record(inputs={"tables": ["routines", "routine_runs"]}, outputs=summary)
    livesim.evidence("routine-ledger.json", json.dumps(
        {"routines": summary, "accepted_without_gate": accepted_without_gate}, indent=2))
    assert accepted_without_gate == 0, "a run was accepted despite failing its gate"
    for r in routines:
        assert r["accepted_runs"] <= r["total_runs"], f"{r['name']}: accepted > total"
        assert r["total_runs"] == r["rr_count"], (
            f"{r['name']}: total_runs={r['total_runs']} but routine_runs has {r['rr_count']}"
        )
        if r["acceptance_rate"] is not None:
            assert 0.0 <= r["acceptance_rate"] <= 1.0, f"{r['name']}: rate out of [0,1]"


# LiveSim-test's own stop_reason taxonomy mirror of
# omniagentos.scheduler.routines.{NEUTRAL_STOP_REASONS, UNGATEABLE_STOP_REASONS}
# — duplicated (not imported) deliberately: an observational test should not
# silently track a product-code rename of these sets and keep passing; if the
# product taxonomy changes, this constant must be updated by a human reading
# both sides, the same discipline as any other contract test.
_NEUTRAL_OUTCOME_STOP_REASONS = frozenset(
    {
        "loop_parked_awaiting_human",
        "loop_idle_no_work",
        "gate_evidence_unavailable",
        "gate_unconfigured",
        "gate_unverifiable",
    }
)


@pytest.mark.positive
def test_routine_acceptance_rate_matches_judged_denominator(livesim, live_db_ro):
    """CORRECTED (was: test_routine_settlement_drift_defect part a, LS-015).

    Fixed 2026-08-06: the prior version compared routines.acceptance_rate
    against the naive accepted_runs/total_runs and flagged a "drift" of ~0.016
    on the two high-volume routines. That was the test using the wrong
    denominator — omniagentos.scheduler.routines.acceptance_rate() defines the
    rate over JUDGED runs (total_runs minus the persisted neutral_runs
    counter), specifically so a routine that parks/idles doesn't get its rate
    depressed by non-results (see that function's docstring: "a lie with
    teeth"). Using the product's own denominator, the stored rate matches
    exactly (bit-for-bit, not just within tolerance) for every routine with a
    judged run — there is no drift.
    """
    livesim.target("db")
    checked = []
    mismatched = []
    for r in live_db_ro.execute(
        "SELECT name, status, total_runs, accepted_runs, neutral_runs, acceptance_rate"
        " FROM routines WHERE total_runs > 0"
    ).fetchall():
        neutral_runs = r["neutral_runs"] or 0
        judged = r["total_runs"] - neutral_runs
        if judged <= 0:
            # Product contract: empty judged denominator -> acceptance_rate
            # must be NULL, never a lying 0.0 or 1.0.
            if r["acceptance_rate"] is not None:
                mismatched.append(
                    {"name": r["name"], "reason": "judged=0 but acceptance_rate is not NULL",
                     "stored_rate": r["acceptance_rate"]}
                )
            continue
        implied = r["accepted_runs"] / judged
        entry = {"name": r["name"], "stored_rate": r["acceptance_rate"],
                 "implied_rate": round(implied, 12), "accepted": r["accepted_runs"],
                 "total": r["total_runs"], "neutral": neutral_runs, "judged": judged}
        checked.append(entry)
        if r["acceptance_rate"] is None or abs(implied - r["acceptance_rate"]) > 1e-9:
            mismatched.append(entry)
    out = {"checked": checked, "mismatched": mismatched}
    livesim.record(inputs={"denominator": "total_runs - neutral_runs", "tolerance": 1e-9}, outputs=out)
    livesim.evidence("acceptance-rate-judged-denominator.json", json.dumps(out, indent=2))
    if not checked and not mismatched:
        pytest.skip("no routines with total_runs>0 to check yet")
    assert mismatched == [], (
        f"routines.acceptance_rate disagrees with accepted_runs/(total_runs-neutral_runs) "
        f"for: {[m['name'] for m in mismatched]} — this IS the product defect the old test "
        f"meant to catch; investigate the write path in omniagentos/scheduler/store.py "
        f"before assuming it's another test bug."
    )


@pytest.mark.positive
def test_routine_unsettled_runs_are_all_neutral_classified(livesim, live_db_ro):
    """CORRECTED (was: test_routine_settlement_drift_defect part b, LS-016).

    Fixed 2026-08-06: the prior version counted every finished routine_run
    with `accepted IS NULL` as an "unsettled" leak. But
    omniagentos.scheduler.routines.classify_run_outcome() classifies a
    finished run as NEUTRAL — by design, excluded from the acceptance
    denominator — whenever its stop_reason names a non-result cause
    (awaiting a human, idle, or gate evidence unavailable/unconfigured), or
    when gate_passed AND accepted are both NULL (evidence-absence). Those are
    not unsettled leaks; the settlement layer deliberately leaves `accepted`
    NULL for them. The real invariant is narrower: a run where a gate
    actually RULED (`gate_passed IS NOT NULL`) with a non-neutral stop_reason
    but `accepted` was still never written IS a genuine settlement leak, and
    that set should be empty.
    """
    livesim.target("db")
    placeholders = ",".join("?" for _ in _NEUTRAL_OUTCOME_STOP_REASONS)
    all_unsettled = live_db_ro.execute(
        "SELECT id, routine_id, stop_reason, gate_passed FROM routine_runs"
        " WHERE finished_at IS NOT NULL AND accepted IS NULL"
    ).fetchall()
    # CORRECTED 2026-08-06 (LS-TEST-006): `rr.stop_reason NOT IN (...)` is
    # SQL-NULL, not TRUE, when stop_reason IS NULL -- every leaked row with a
    # NULL stop_reason silently vanished from this query. The product's own
    # taxonomy (omniagentos/scheduler/routines.py classify_run_outcome)
    # classifies finished + stop_reason NULL + gate_passed set + accepted
    # NULL as ADVERSE -- exactly the genuine settlement leak this test
    # exists to catch. `(rr.stop_reason IS NULL OR rr.stop_reason NOT IN (...))`
    # makes a NULL stop_reason count as "not a named neutral reason" (TRUE),
    # matching the Python sibling filter two lines below, which already used
    # `row["stop_reason"] not in _NEUTRAL_OUTCOME_STOP_REASONS` -- `None not
    # in a set of strings` is True in Python, so the two halves used to
    # disagree about NULL and now agree.
    leaks = live_db_ro.execute(
        "SELECT rr.id, r.name, r.status, rr.stop_reason, rr.gate_passed"
        " FROM routine_runs rr JOIN routines r ON r.id = rr.routine_id"
        " WHERE rr.finished_at IS NOT NULL AND rr.accepted IS NULL"
        f"   AND (rr.stop_reason IS NULL OR rr.stop_reason NOT IN ({placeholders}))"
        "   AND rr.gate_passed IS NOT NULL",
        tuple(_NEUTRAL_OUTCOME_STOP_REASONS),
    ).fetchall()
    non_neutral_null_both = [
        dict(row) for row in all_unsettled
        if row["stop_reason"] not in _NEUTRAL_OUTCOME_STOP_REASONS and row["gate_passed"] is None
    ]
    out = {
        "total_accepted_null_finished": len(all_unsettled),
        "genuine_settlement_leaks": [dict(r) for r in leaks],
        "non_neutral_evidence_absent_rows": non_neutral_null_both,
    }
    livesim.record(
        inputs={"neutral_stop_reasons": sorted(_NEUTRAL_OUTCOME_STOP_REASONS)}, outputs=out
    )
    livesim.evidence("routine-unsettled-classification.json", json.dumps(out, indent=2))
    livesim.note(
        f"{len(all_unsettled)} finished routine_runs have accepted IS NULL; "
        f"{len(leaks)} are genuine settlement leaks (gate ruled, non-neutral "
        f"stop_reason, accepted still NULL)."
    )
    assert leaks == [], (
        f"genuine settlement leak(s): a gate ruled on these runs (gate_passed set, "
        f"non-neutral stop_reason) but `accepted` was never settled: "
        f"{[dict(r) for r in leaks]} — this IS a product defect; investigate the "
        f"settlement/gate write path before assuming it's a test bug."
    )


# ---------------------------------------------------------------------------
# Swarm attempts: live-uniqueness invariant
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_swarm_live_attempt_uniqueness_live(livesim, live_db_ro):
    """The live DB carries the partial unique index idx_swarm_attempts_live
    (one open attempt per board task) and the live data honours it: no
    board_task_id has more than one attempt with ended_at IS NULL."""
    livesim.target("db")
    idx = live_db_ro.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_swarm_attempts_live'"
    ).fetchone()
    assert idx is not None, "idx_swarm_attempts_live missing from live schema"
    ddl = " ".join(idx["sql"].split()).lower()
    assert "unique" in ddl and "board_task_id" in ddl and "ended_at is null" in ddl
    dupes = live_db_ro.execute(
        "SELECT board_task_id, COUNT(*) c FROM swarm_attempts"
        " WHERE ended_at IS NULL GROUP BY board_task_id HAVING c > 1"
    ).fetchall()
    open_attempts = live_db_ro.execute(
        "SELECT COUNT(*) n FROM swarm_attempts WHERE ended_at IS NULL"
    ).fetchone()["n"]
    total = live_db_ro.execute("SELECT COUNT(*) n FROM swarm_attempts").fetchone()["n"]
    statuses = {
        r["status"]: r["n"]
        for r in live_db_ro.execute(
            "SELECT status, COUNT(*) n FROM swarm_runs GROUP BY status"
        ).fetchall()
    }
    assert set(statuses) <= SWARM_RUN_STATES, f"unknown swarm_run status: {set(statuses) - SWARM_RUN_STATES}"
    livesim.record(
        inputs={"index": "idx_swarm_attempts_live"},
        outputs={"open_attempts": open_attempts, "total_attempts": total,
                 "duplicate_open_tasks": [dict(d) for d in dupes],
                 "swarm_run_statuses": statuses},
    )
    assert dupes == [], f"live-uniqueness violated for: {[d['board_task_id'] for d in dupes]}"


@pytest.mark.negative
@pytest.mark.boundary
def test_swarm_live_index_enforced_on_scratch(livesim, live_db_ro, livesim_ns, scratch_dir):
    """The partial unique index actually REJECTS a second live attempt for the
    same board task, and admits one again once the first attempt has ended.

    Destructive by nature, so it runs on a fresh scratch DB built from the LIVE
    schema's own DDL (sqlite_master), never the live DB.
    """
    livesim.target("db", "fs")
    rows = live_db_ro.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND ("
        " (type='table' AND name='swarm_attempts') OR"
        " (type='index' AND tbl_name='swarm_attempts'))"
    ).fetchall()
    ddl = {r["name"]: r["sql"] for r in rows}
    assert "swarm_attempts" in ddl and "idx_swarm_attempts_live" in ddl
    scratch = Path(scratch_dir) / "swarm_schema.sqlite3"
    conn = sqlite3.connect(scratch)
    try:
        for sql in ddl.values():
            conn.execute(sql)
        task = f"btask_{livesim_ns}"
        ins = (
            "INSERT INTO swarm_attempts"
            " (id, swarm_run_id, board_task_id, seq, provider, model, started_at, ended_at)"
            " VALUES (?, 'swr_livesim', ?, ?, 'claude', 'test', '2026-08-06T00:00:00Z', ?)"
        )
        conn.execute(ins, (f"swa_{livesim_ns}_1", task, 1, None))  # open attempt
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, (f"swa_{livesim_ns}_2", task, 2, None))  # 2nd open -> refused
        # once the first attempt ENDS, a new live attempt is admitted
        conn.execute("UPDATE swarm_attempts SET ended_at='2026-08-06T00:01:00Z', end_reason='crashed' WHERE seq=1")
        conn.execute(ins, (f"swa_{livesim_ns}_3", task, 3, None))
        open_now = conn.execute(
            "SELECT COUNT(*) FROM swarm_attempts WHERE ended_at IS NULL"
        ).fetchone()[0]
        livesim.record(
            inputs={"scratch_db": scratch.name, "board_task_id": task},
            outputs={"second_open_insert": "IntegrityError", "reopen_after_end": "allowed",
                     "open_after": open_now},
        )
        assert open_now == 1
    finally:
        conn.close()
        scratch.unlink(missing_ok=True)  # scratch db removed; nothing touched live
        livesim.cleanup(not scratch.exists())


@pytest.mark.recovery
def test_no_orphaned_open_swarm_attempts(livesim, live_db_ro):
    """RECOVERY: closing open attempts whose session went terminal is the
    liveness-reaper's job (30m launchd cadence). Nothing may slip through for
    long: no open attempt whose session is completed/failed/killed may be older
    than 2h, and no open attempt at all may be older than 7 days."""
    livesim.target("db")
    now = _now()
    rows = live_db_ro.execute(
        "SELECT sa.id, sa.board_task_id, sa.session_id, sa.started_at, s.state session_state"
        " FROM swarm_attempts sa LEFT JOIN sessions s ON s.id = sa.session_id"
        " WHERE sa.ended_at IS NULL"
    ).fetchall()
    orphaned_old, ancient = [], []
    for r in rows:
        age_h = (now - _parse_ts(r["started_at"])).total_seconds() / 3600.0
        if r["session_state"] in TERMINAL_SESSION_STATES and age_h > 2.0:
            orphaned_old.append({**dict(r), "age_h": round(age_h, 1)})
        if age_h > 7 * 24:
            ancient.append({**dict(r), "age_h": round(age_h, 1)})
    livesim.record(
        inputs={"thresholds": {"orphan_terminal_h": 2, "ancient_days": 7}},
        outputs={"open_attempts": len(rows), "orphaned_terminal_gt_2h": orphaned_old,
                 "ancient_gt_7d": ancient},
    )
    livesim.note(f"open swarm_attempts right now: {len(rows)}")
    assert orphaned_old == [], f"liveness-reaper is not closing orphans: {orphaned_old}"
    assert ancient == [], f"impossibly old open attempts: {ancient}"


# ---------------------------------------------------------------------------
# Approvals producer + spawn queue + loops
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.e2e_live
def test_approvals_producer_reachable_and_consistent(livesim, live_api, live_db_ro):
    """GET /api/approvals answers 200 with a well-formed list, and every
    approval state in the live DB is a legal contracts.ApprovalState."""
    livesim.target("api", "db")
    status, body, _ = live_api.get("/api/approvals")
    if status == 0:
        pytest.skip(f"live API unreachable: {body}")
    assert status == 200, f"/api/approvals -> {status}"
    assert isinstance(body, list), f"expected a JSON list, got {type(body).__name__}"
    for item in body:
        assert isinstance(item, dict)
        for key in ("id", "state", "action_class", "proposed_action", "created_at"):
            assert key in item, f"approval item missing '{key}'"
        assert item["state"] in APPROVAL_STATES
    db_states = {
        r["state"]: r["n"]
        for r in live_db_ro.execute(
            "SELECT state, COUNT(*) n FROM approvals GROUP BY state"
        ).fetchall()
    }
    assert set(db_states) <= APPROVAL_STATES, f"illegal approval state(s): {set(db_states) - APPROVAL_STATES}"
    livesim.record(
        inputs={"path": "/api/approvals"},
        outputs={"api_items": len(body), "api_states": sorted({i['state'] for i in body}),
                 "db_state_counts": db_states},
    )
    livesim.note(f"approvals in DB by state: {db_states}; API returned {len(body)} item(s)")


@pytest.mark.boundary
def test_spawn_queue_state_machine_sane(livesim, live_db_ro):
    """session_spawn_queue rows only ever hold legal states, failed rows carry
    an error, and nothing sits 'queued' for more than a day (a stuck queue is
    exactly how spawns silently stop happening)."""
    livesim.target("db")
    rows = live_db_ro.execute(
        "SELECT id, state, error, created_at FROM session_spawn_queue"
    ).fetchall()
    now = _now()
    counts: dict[str, int] = {}
    stuck = []
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        assert r["state"] in SPAWN_STATES, f"illegal spawn state {r['state']!r} on {r['id']}"
        if r["state"] == "failed":
            assert r["error"], f"failed spawn {r['id']} has no error recorded"
        if r["state"] == "queued":
            age_h = (now - _parse_ts(r["created_at"])).total_seconds() / 3600.0
            if age_h > 24.0:
                stuck.append({"id": r["id"], "age_h": round(age_h, 1)})
    livesim.record(inputs={"table": "session_spawn_queue"},
                   outputs={"rows": len(rows), "state_counts": counts, "stuck_queued_gt_24h": stuck})
    assert stuck == [], f"spawn queue has stuck rows: {stuck}"


@pytest.mark.boundary
@pytest.mark.degradation
def test_loop_tables_present_and_reservations_settle(livesim, live_db_ro):
    """The loop budget plane exists (loop_reservations/loop_settings with the
    expected columns) and no reservation is stuck: anything past its expires_at
    must not still be sitting in an in-flight state.

    In-flight means ``loop_budget.STATE_OPEN`` — a hold taken before a paid call
    that nobody settled or released. It is referenced, not transcribed: this
    filter used to name four states ('active'/'pending'/'held'/'reserved') that
    the ledger has never written, so it matched nothing and the assertion below
    could not fail. `tests/scheduler/test_loop_budget_probe_vocabulary.py` pins
    the agreement in the normal lane, where this livesim probe does not run."""
    livesim.target("db")
    res_cols = {r["name"] for r in live_db_ro.execute("PRAGMA table_info(loop_reservations)").fetchall()}
    set_cols = {r["name"] for r in live_db_ro.execute("PRAGMA table_info(loop_settings)").fetchall()}
    assert {"id", "instance_id", "capability_id", "max_usd", "state",
            "expires_at", "actual_usd", "cost_quality"} <= res_cols
    assert {"key", "value"} <= set_cols
    now_epoch = _now().timestamp()
    rows = live_db_ro.execute(
        "SELECT id, state, expires_at, actual_usd FROM loop_reservations"
    ).fetchall()
    states: dict[str, int] = {}
    stuck = [
        {"id": r["id"], "state": r["state"], "expired_for_s": round(now_epoch - r["expires_at"], 0)}
        for r in rows
        if r["expires_at"] < now_epoch - 3600 and r["state"] in (loop_budget.STATE_OPEN,)
    ]
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    settings = {r["key"]: r["value"] for r in live_db_ro.execute(
        "SELECT key, value FROM loop_settings LIMIT 50").fetchall()}
    livesim.record(
        inputs={"tables": ["loop_reservations", "loop_settings"]},
        outputs={"reservations": len(rows), "reservation_states": states,
                 "stuck_expired_inflight": stuck, "loop_settings": settings},
    )
    livesim.note(f"loop_reservations rows: {len(rows)}; states: {states or '(empty)'}")
    assert stuck == [], f"expired reservations never settled: {stuck}"
