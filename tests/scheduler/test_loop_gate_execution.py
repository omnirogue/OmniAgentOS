"""Doctrine: a loop's verdict is produced by its GATE, never by the loop.

Why this file exists
--------------------
Loop and built-in ticks never reached settlement. ``routines_tick._fire``
recorded them with no ``run_id`` and stamped ``finished_at`` in the same INSERT,
writing the worker's own status into ``gate_passed``; ``settle_pending`` — the
only code in this repo that executes ``gate_config.command`` — requires a
``run_id``, so it structurally never saw one. The result, live, at the moment
this file was written:

    routine rtn_1e5567b9f3314a2c9d76 (w3-health-monitor)
    10 runs, run_id NULL on all 10, gate_passed=1 on all 10, acceptance 1.0
    every one of them a PARK that healed nothing
    declared gate `pytest loops/tests/instances/test_health_monitor.py`:
    never executed, not once

Every green tick was the worker grading its own homework, and the objective gate
the routine declared was decoration.

What these tests assert
-----------------------
Nothing is injected. Each test sets ``OMNIAGENTOS_GATE_WORKSPACE`` to a real git
checkout, runs the production entry point (``routines_tick.tick``), and then
reads BOTH the ``routine_runs`` row and the signed ``GateEvidence`` the run
produced. The assertions are on the evidence — command, counts, exit code,
binding — not on a status string, because a status string is exactly what could
not be trusted.

The composition, in one sentence: **a self-report may lower a verdict and may
never raise one.** ``gate_passed`` holds only what an executed gate found;
``accepted`` (the acceptance floor's numerator) is true only where a favourable
claim met a passing gate.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler import loop_jobs
from omniagentos.scheduler.gate_evidence import (
    MAX_EVIDENCE_AGE_SECONDS,
    GateEvidenceStore,
)
from omniagentos.scheduler.loop_jobs import (
    ADVERSE_STATUSES,
    FAVOURABLE_STATUSES,
    NEUTRAL_STATUSES,
)
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore, _count_settled_runs

GREEN_SUITE = "def test_one(): assert True\ndef test_two(): assert True\n"
RED_SUITE = "def test_one(): assert True\ndef test_two(): assert False\n"

GATE_COMMAND = "pytest gates/test_loop_gate.py"


# ---------------------------------------------------------------------------
# fixtures — a real workspace, a real (fake-reporting) worker binary
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence store + signing key under tmp; no inherited gate workspace."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)


@pytest.fixture
def now() -> datetime:
    """The tick clock, anchored to the REAL one on purpose.

    These tests execute the gate for real, and real evidence is stamped with the
    wall clock. A frozen literal in the past would make every verdict "dated in
    the future" (``evidence_rejections``) and every test here would be measuring
    the fixture instead of the code. Offsets from this base move FORWARD only —
    which is also the only direction production's clock moves.
    """
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    from tests.support.db_template import make_store

    db_path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, db_path)
    yield handle
    handle.close()


def _git_workspace(root: Path, suite: str) -> Path:
    """A real, clean, committed git checkout the gate can be pinned to."""
    (root / "gates").mkdir(parents=True, exist_ok=True)
    (root / "gates" / "test_loop_gate.py").write_text(suite, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")
    for argv in (
        ["git", "init"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(argv, cwd=str(root), check=True, capture_output=True)
    return root


@pytest.fixture
def green_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = _git_workspace(tmp_path / "gate-green", GREEN_SUITE)
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    return workspace


@pytest.fixture
def red_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = _git_workspace(tmp_path / "gate-red", RED_SUITE)
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    return workspace


@pytest.fixture
def loop_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a REAL executable that reports a chosen loop status.

    A shell script rather than a patched ``subprocess.run``: patching the
    attribute on the shared ``subprocess`` module would also intercept the gate
    runner's own git and pytest invocations, and this file exists to watch those
    actually happen.
    """

    def install(status: str, *, detail: str = "", stage: str = "verify") -> Path:
        report = json.dumps(
            {
                "instance_id": "w3_health_monitor",
                "template": "monitor_diagnose_repair_verify",
                "status": status,
                "detail": detail,
                "stage": stage,
                "effects": [],
                "approval_id": None,
                "resumed": False,
                # The worker's own "accepted" claim. Deliberately True for every
                # status this file drives: if it were ever consulted again, these
                # tests go red.
                "accepted": True,
            }
        )
        path = tmp_path / "loop-worker"
        path.write_text(f"#!/bin/sh\ncat <<'JSON'\n{report}\nJSON\n", encoding="utf-8")
        path.chmod(0o755)
        monkeypatch.setattr(loop_jobs, "_loop_worker_path", lambda: path)
        return path

    return install


def _loop_routine(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "w3-health-monitor",
        "description": "W3 health monitor loop",
        "trigger_type": "cron",
        "trigger_config": {"cron": "* * * * *"},
        "task_template": {
            "title": "Loop tick: w3_health_monitor",
            "harness": "mock",
            "input": {
                "module": loop_jobs.LOOP_MODULE,
                "kind": "loop",
                "template": "monitor_diagnose_repair_verify",
                "instance_id": "w3_health_monitor",
                "instance_module": "omniagentos_loops.instances.health_monitor",
                "params": {},
            },
        },
        "gate_type": "test_command",
        "gate_config": {"command": GATE_COMMAND, "expected_exit_code": 0},
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _tick(store: SqliteStore, *, now: datetime) -> dict[str, Any]:
    return tick(store, load_policy(), now=now)


def _only_run(routines: RoutinesStore, routine_id: str) -> dict[str, Any]:
    rows = routines.list_runs(routine_id)
    assert len(rows) == 1, rows
    return rows[0]


# ---------------------------------------------------------------------------
# 1. the wiring: a loop run is settleable at all
# ---------------------------------------------------------------------------


def test_a_loop_tick_records_a_run_id_and_leaves_the_verdict_to_settlement(
    store: SqliteStore, loop_worker, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, inverted: the firing tick must not judge its own tick.

    Settlement is suppressed here so the PENDING row is observable — the state
    that could not exist before, and without which ``gate_config.command`` can
    never run.
    """
    from omniagentos.scheduler import routines_tick

    monkeypatch.setattr(
        routines_tick, "settle_pending", lambda *a, **k: {"checked": 0, "settled": [], "errors": []}
    )
    loop_worker("completed")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    fired = _tick(store, now=now)["fired"][0]

    row = _only_run(routines, routine["id"])
    assert fired["run_id"].startswith("btrun_")
    assert row["run_id"] == fired["run_id"], "a loop run with no run_id can never be settled"
    assert row["finished_at"] is None, "a tick that has not been judged is not finished"
    assert row["gate_passed"] is None, "the firing tick may never write a gate verdict"
    assert row["accepted"] is None
    assert row["outcome_class"] is None, "pending is not an outcome"
    # The claim travels to settlement in its own column, beside the verdict.
    assert row["self_reported_status"] == "completed"
    assert row["stop_reason"] == ""


# ---------------------------------------------------------------------------
# 2. the gate actually executes — asserted on evidence, not on a status string
# ---------------------------------------------------------------------------


def test_a_loops_declared_gate_command_actually_executes(
    store: SqliteStore, green_workspace: Path, loop_worker, now: datetime
) -> None:
    """The command in ``gate_config`` runs, in the workspace, and is signed.

    Every assertion below is on the ``GateEvidence`` record: which command ran,
    how many checks it collected and passed, what it exited, and which commit it
    ran at. A test that asserted ``stop_reason == "gate_passed"`` alone would
    have passed against the old code too, because the old code wrote that string
    from the worker's own status.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    result = _tick(store, now=now)

    assert result["settled"]["errors"] == []
    row = _only_run(routines, routine["id"])
    evidence = GateEvidenceStore().load(routine["id"], str(row["run_id"]))
    assert evidence is not None, "the declared gate did not execute for this loop run"
    assert evidence.command == GATE_COMMAND
    assert evidence.targets == ("gates/test_loop_gate.py",)
    assert evidence.exit_code == 0
    assert evidence.checks_collected == 2, "the real suite has two tests; this is a real count"
    assert evidence.checks_passed == 2
    assert evidence.checks_failed == 0
    assert evidence.tool == "pytest"
    head = subprocess.run(
        ["git", "-C", str(green_workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert evidence.workspace_sha == head, "evidence must name the commit it was produced at"
    assert evidence.run_id == row["run_id"], "evidence is bound to THIS loop run"
    assert evidence.iteration == row["iteration"]

    assert row["gate_passed"] == 1
    assert row["accepted"] == 1
    assert row["outcome_class"] == "favourable"
    assert row["stop_reason"] == "gate_passed"
    assert row["self_reported_status"] == "completed"
    # The loop's own account survives settlement; it is context, not verdict.
    assert "completed" in (row["notes"] or "")


def test_the_gate_runs_in_a_throwaway_tree_and_leaves_the_pin_source_clean(
    store: SqliteStore, green_workspace: Path, loop_worker, now: datetime
) -> None:
    """The ephemeral per-run worktree is inherited, not re-litigated.

    A verifier that writes into a persistent checkout condemns every LATER run
    (dirty tree -> GateWorkspaceUnusable -> permanent NULL), which would make a
    misbehaving routine permanently immune to auto-pause. Loop runs get the same
    protection because they use the same runner.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())

    _tick(store, now=now)

    status = subprocess.run(
        ["git", "-C", str(green_workspace), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == "", "the gate must never write into the pin source"
    worktrees = subprocess.run(
        ["git", "-C", str(green_workspace), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(worktrees.stdout.strip().splitlines()) == 1, "the run tree must be destroyed"


# ---------------------------------------------------------------------------
# 3. THE invariant: the claim cannot survive a red gate
# ---------------------------------------------------------------------------


def test_a_loop_reporting_completed_whose_gate_fails_settles_adverse(
    store: SqliteStore, red_workspace: Path, loop_worker, now: datetime
) -> None:
    """The whole lane in one test.

    The worker says ``completed`` and its report says ``accepted: true``. The
    routine's declared gate goes red. The run settles ADVERSE and counts against
    the acceptance floor, and nothing the worker can say prevents it.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["self_reported_status"] == "completed", "the claim is recorded..."
    assert row["gate_passed"] == 0, "...and refuted"
    assert row["accepted"] == 0
    assert row["outcome_class"] == "adverse"
    assert row["stop_reason"] == "gate_failed"
    assert "failed checks" in (row["notes"] or ""), row["notes"]

    settled, accepted = _count_settled_runs(store._connection, routine["id"])
    assert (settled, accepted) == (1, 0), "a refuted claim belongs in the floor's denominator"


def test_a_failing_gate_condemns_even_a_non_result(
    store: SqliteStore, red_workspace: Path, loop_worker, now: datetime
) -> None:
    """Neutrality protects a non-RESULT, not a broken loop.

    A park is neither success nor failure and stays out of the denominator — but
    only while the gate is silent or passing. A gate that RAN and FAILED is
    evidence of failure, not absence of evidence, and the distinction is the
    whole reason absence is treated separately: a loop that parks forever with a
    red gate would otherwise be permanently invisible, which is the exact
    invisibility this taxonomy exists to close.
    """
    loop_worker("parked")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["self_reported_status"] == "parked"
    assert row["gate_passed"] == 0
    assert row["accepted"] == 0
    assert row["outcome_class"] == "adverse"
    assert row["stop_reason"] == "gate_failed"


# ---------------------------------------------------------------------------
# 4. the sibling lane's taxonomy is preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [("parked", "loop_parked_awaiting_human"), ("idle", "loop_idle_no_work")],
)
def test_a_park_or_idle_still_settles_neutral_when_the_gate_passes(
    store: SqliteStore, green_workspace: Path, loop_worker, now: datetime, status: str, reason: str
) -> None:
    """A passing gate does not turn a non-result into a result.

    The evidence is kept — it was really produced, and deleting it would blind
    the sentinel's evidence-free streak alarm — but the run stays OUT of the
    acceptance denominator, and the executor's own reason code (waiting on a
    human vs nothing to do) is restored as the row's ``stop_reason``.
    """
    loop_worker(status)
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["gate_passed"] == 1, "the gate really ran; its verdict is recorded"
    assert row["accepted"] is None, "a non-result is never an acceptance"
    assert row["outcome_class"] == "neutral"
    assert row["stop_reason"] == reason

    settled, accepted = _count_settled_runs(store._connection, routine["id"])
    assert (settled, accepted) == (0, 0), "a non-result is out of the denominator entirely"
    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["acceptance_rate"] is None, "an empty denominator is UNKNOWN, not 0%"
    assert updated["status"] == "active", updated.get("auto_pause_reason")


@pytest.mark.parametrize("status", sorted(ADVERSE_STATUSES))
def test_a_self_reported_failure_counts_against_the_floor_with_no_gate_run(
    store: SqliteStore, loop_worker, now: datetime, status: str
) -> None:
    """A tick that failed has nothing for a gate to verify.

    Same rule the dispatched path has always used for ``RunState.FAILED``: the
    run failed, the gate is not evaluated, the row settles 0/0. Believing a
    worker that reports its OWN failure is the one safe direction — a
    self-report may lower a verdict and never raise one — and it is what keeps a
    crashing loop trippable by the floor on an installation with no gate
    workspace at all.
    """
    loop_worker(status)
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["self_reported_status"] == status
    assert row["gate_passed"] == 0
    assert row["accepted"] == 0
    assert row["outcome_class"] == "adverse"
    assert row["stop_reason"] == loop_jobs.STATUS_STOP_REASONS[status]
    assert GateEvidenceStore().load(routine["id"], str(row["run_id"])) is None


# ---------------------------------------------------------------------------
# 5. absence of evidence stays absence — never a failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "parked"])
def test_no_gate_workspace_settles_null_null_never_zero(
    store: SqliteStore, loop_worker, now: datetime, status: str
) -> None:
    """Unjudged is not failed. Regressing this auto-pauses healthy routines.

    This is also the honest answer to a favourable claim with nothing to back
    it: UNCORROBORATED. Not accepted (the claim is not evidence), not rejected
    (nothing ruled against it), and loud — NULL/NULL over three settlements is
    exactly what the health sentinel's ``gate_settlement`` check alarms on.
    """
    loop_worker(status)
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["gate_passed"] is None
    assert row["accepted"] is None
    assert row["outcome_class"] == "neutral"
    settled, _ = _count_settled_runs(store._connection, routine["id"])
    assert settled == 0


def test_a_dirty_gate_workspace_is_absence_not_failure(
    store: SqliteStore, green_workspace: Path, loop_worker, now: datetime
) -> None:
    """``GateWorkspaceUnusable`` -> ``unavailable`` -> NULL/NULL, on the loop path too.

    The configuration probe proved the workspace clean when the job spawned; it
    can go dirty at any moment afterwards (a concurrent merge, an editor). That
    is a fact about the workspace, never a verdict on the run.
    """
    loop_worker("completed")
    (green_workspace / "stray.txt").write_text("dirty\n", encoding="utf-8")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["gate_passed"] is None, "a dirty workspace judged nothing"
    assert row["accepted"] is None
    assert row["outcome_class"] == "neutral"
    assert row["stop_reason"] == "gate_evidence_unavailable"


def test_evidence_older_than_the_freshness_window_is_refused(
    store: SqliteStore, green_workspace: Path, loop_worker, now: datetime
) -> None:
    """The 24h window applies to loop evidence exactly as to dispatched runs.

    Nobody may "fix" a slow loop by widening ``MAX_EVIDENCE_AGE_SECONDS``: a
    verdict older than a day is not a verdict about today's tick.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)
    row = _only_run(routines, routine["id"])
    assert row["gate_passed"] == 1

    # Second tick: same routine, new run, evidence produced "now" but judged a
    # day later. The settlement clock is the one that moves.
    stale_at = now + timedelta(seconds=MAX_EVIDENCE_AGE_SECONDS + 3600)
    _tick(store, now=stale_at)
    later = routines.list_runs(routine["id"])[0]
    assert later["run_id"] != row["run_id"]
    assert later["gate_passed"] == 0, later["notes"]
    assert "stale" in (later["notes"] or ""), later["notes"]


# ---------------------------------------------------------------------------
# 6. the doctrine, stated mechanically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    sorted(FAVOURABLE_STATUSES | NEUTRAL_STATUSES | ADVERSE_STATUSES) + ["quantum_superposition"],
)
def test_doctrine_no_self_report_can_produce_an_acceptance_without_gate_evidence(
    store: SqliteStore, loop_worker, now: datetime, status: str
) -> None:
    """A loop's self-report is NEVER the sole verdict — for any status it can emit.

    With no gate workspace there is no executed evidence, so no status — not
    ``completed``, not one this scheduler has never heard of — may settle
    ``accepted``. The only thing a self-report can do on its own is settle
    against the loop (``adverse``), which is the safe direction.
    """
    loop_worker(status)
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["accepted"] is not True, f"{status!r} was accepted on the loop's word alone"
    assert row["accepted"] in (None, 0)
    assert row["gate_passed"] in (None, 0), "gate_passed may only ever hold an executed verdict"


def test_doctrine_the_claim_and_the_verdict_are_separately_queryable(
    store: SqliteStore, red_workspace: Path, loop_worker, now: datetime
) -> None:
    """ "The loop said it worked and the gate said otherwise" must be one SQL query.

    Before migration 104 the claim and the verdict were the same column, so this
    question could not be asked at all — which is why ten consecutive parks could
    read as ten acceptances and nobody could tell from the data.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine())

    _tick(store, now=now)

    disagreements = store._connection.execute(
        "SELECT routine_id, run_id FROM routine_runs "
        "WHERE self_reported_status = 'completed' AND gate_passed = 0"
    ).fetchall()
    assert len(disagreements) == 1


# ---------------------------------------------------------------------------
# 7. the canary seam: a probe that fails by construction must auto-pause
# ---------------------------------------------------------------------------


def test_a_routine_whose_declared_gate_fails_by_construction_auto_pauses(
    store: SqliteStore, red_workspace: Path, loop_worker, now: datetime
) -> None:
    """The verdict machinery's own health check, end to end.

    A canary loop reports ``completed`` every tick and declares a gate that is
    red by construction. Probe -> settlement -> floor -> pause must fire without
    a human, and ``auto_pause_reason`` must say why (``status`` alone cannot tell
    an automatic pause from a manual one after the fact).

    This is the hook the canary routine plugs into: nothing about it is specific
    to what the gate checks, only that a failing declared gate reaches the floor.
    """
    loop_worker("completed")
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    for minute in range(3):  # AUTO_PAUSE_MIN_RUNS
        _tick(store, now=now + timedelta(minutes=minute))

    rows = routines.list_runs(routine["id"])
    assert len(rows) == 3, rows
    assert all(row["gate_passed"] == 0 for row in rows)

    paused = routines.get_routine(routine["id"])
    assert paused is not None
    assert paused["status"] == "auto_paused"
    assert "below the 50% floor" in (paused["auto_pause_reason"] or "")


# ---------------------------------------------------------------------------
# 8. code ahead of schema: degrade honestly, never back to the defect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_gate", "outcome"),
    [
        ("completed", None, "neutral"),
        ("parked", None, "neutral"),
        ("failed", False, "adverse"),
    ],
)
def test_a_database_without_migration_104_degrades_to_absence_not_to_the_defect(
    store: SqliteStore,
    green_workspace: Path,
    loop_worker,
    now: datetime,
    status: str,
    expected_gate: bool | None,
    outcome: str,
) -> None:
    """Deploy ahead of ``make migrate``: the claim cannot travel, so there is no gate.

    The tempting fallback — keep writing the worker's status into ``gate_passed``
    until the column exists — is the defect itself, and a transitional window is
    exactly when a silent lie is most expensive. Instead the tick settles inline
    with the composition an ABSENT gate produces: a favourable claim is
    uncorroborated (NULL/NULL, out of the floor) and a self-reported failure
    still counts against it.

    Note the gate workspace here is perfectly usable; the run is unjudged
    because the claim could not reach settlement, which the row says out loud.
    """
    store._connection.execute("ALTER TABLE routine_runs DROP COLUMN self_reported_status")
    store._connection.commit()
    assert not RoutinesStore(store).supports_self_report()

    loop_worker(status)
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine())

    _tick(store, now=now)

    row = _only_run(routines, routine["id"])
    assert row["finished_at"] is not None, "an unsettleable row must never be left pending"
    assert row["gate_passed"] is expected_gate
    assert row["accepted"] is not True
    assert row["outcome_class"] == outcome
    assert "migration 104" in (row["notes"] or "")
