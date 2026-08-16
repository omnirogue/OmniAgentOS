"""Lane A: a due built-in routine must EXECUTE its declared module.

Decisive assertion
------------------
Drive ``routines_tick.tick`` (the production entry the launchd job runs) with
the real dream-cycle seeder and a seeded episodic fixture. Observe staged
candidates and an accepted ``routine_runs`` row. This file must **not** import
``run_dream_cycle``, ``Lifecycle``, or ``builtin_jobs`` — importing the
mechanism proves the function works and says nothing about whether anything
calls it.

Counterfeits that must make the decisive test RED
-------------------------------------------------
- ``counterfeit_seeded_but_mock_fired`` — today's prior code: seed the row,
  ``_fire`` enqueues a mock task, cycle never runs.
- ``counterfeit_import_without_invoke`` — import the module, record success.
- ``counterfeit_tick_calls_ensure_only`` — re-call ensure (idempotent no-op).
- ``counterfeit_no_input_marked_failed`` — only COMPLETED is accepted; three
  quiet nights auto-pause the routine forever.

Revert-check: both directions reported with real pytest output in the lane notes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import EpisodicEvent, EventResult
from omniagentos.memlife.dream import ensure_dream_cycle_routine
from omniagentos.memlife.store import MemlifeStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.routines import improve_dispatcher_routine
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.support.db_template import make_store

# 03:00 UTC matches DREAM_CYCLE_CRON ("0 3 * * *"); cron_is_due at that
# exact minute with last_fired=None is already asserted True in test_dream.
DUE_NOW = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
# */5 minute for the improve-dispatcher negative control.
DISPATCHER_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"


def _valid_event_line(event_id: str, *, reflection: str) -> str:
    event = EpisodicEvent(
        id=event_id,
        ts=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        skill="swarm.coder",
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    )
    return event.model_dump_json()


def _seed_two_events(root: Path) -> None:
    """Two claims that do not Jaccard-cluster (threshold 0.3) → staged=2."""
    events_path = root / "episodic" / "events.jsonl"
    lines = [
        _valid_event_line(
            "ev_a",
            reflection="Agents cannot commit inside a sandboxed worktree",
        ),
        _valid_event_line(
            "ev_b",
            reflection="Always pin exact dependency versions in lockfiles",
        ),
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_inherited_gate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN this file's premise instead of inheriting it: no gate executes here.

    ``settle_pending`` resolves the gate workspace from
    ``OMNIAGENTOS_GATE_WORKSPACE`` at settlement time, and
    ``scripts/launch-env.sh`` EXPORTS that variable on any box whose
    ``<repo>-gate`` checkout is clean. Every process descended from a launch-env
    shell — the merge gate's ladder worker included, which isolates
    ``OMNIAGENTOS_DB``/``_VAR_DIR``/``_LEDGER_DIR`` per worker but not this —
    therefore ran these tests with a LIVE gate workspace configured. Settlement
    then really executed the dream routine's declared gate
    (``pytest tests/memlife/test_dream.py``) against that workspace's pin, so
    ``gate_passed``/``accepted`` came back as a verdict instead of the NULL/NULL
    every assertion below describes, and the ladder went red on a test that is
    green in any shell without the export. That execution is correct production
    behaviour — ``test_gate_workspace_default.py`` exists to assert it — it is
    simply not the configuration these tests are about.

    No assertion changes; only the precondition stops depending on the operator
    environment. A test that wants a workspace still sets one explicitly, which
    is the same shape ``test_gate_workspace_default._isolated_runtime`` uses.
    """
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "builtin_jobs.db")


@pytest.fixture
def memlife_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memlife_store"
    store = MemlifeStore(root)
    store.ensure_layout()
    monkeypatch.setenv(ENV_STORE, str(root))
    return root


# ---------------------------------------------------------------------------
# Decisive: production tick entry runs the dream cycle
# ---------------------------------------------------------------------------


def test_due_dream_routine_runs_the_cycle_through_tick(
    database: SqliteStore,
    memlife_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN a seeded dream routine + 2 events, WHEN tick at 03:00 UTC,
    THEN the cycle actually stages candidates — and its own report is NOT the
    verdict.

    The work half is unchanged: the built-in really ran and really staged two
    candidates. What changed is the row it produces. A built-in tick now records
    a run_id and its own claim, and settlement composes that claim with the
    routine's executed gate. With no gate workspace configured here, the claim
    stands uncorroborated: NULL/NULL, neutral, out of the acceptance floor —
    not the accepted=1 it used to write from its own status. A built-in job that
    grades itself is the same theorem as a loop that grades itself.
    """
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    routine = ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)

    result = tick(database, load_policy(), now=DUE_NOW)

    fired = result["fired"]
    assert len(fired) == 1, result
    assert fired[0]["fired"] is True
    assert fired[0].get("builtin") == "omniagentos.memlife.dream"
    assert "task_id" not in fired[0]
    # A built-in run now HAS an id — it names no `runs` row (this process did
    # the work) but without one it could never be settled, which is exactly how
    # every built-in run in production went unjudged.
    assert fired[0]["run_id"].startswith("btrun_")

    pending = MemlifeStore(memlife_root).list_pending()
    assert len(pending) == 2, [c.claim for c in pending]

    routines = RoutinesStore(database)
    runs = routines.list_runs(routine["id"])
    assert len(runs) == 1
    row = runs[0]
    assert row["run_id"] == fired[0]["run_id"]
    assert row["accepted"] is None, "the cycle's own report is not an acceptance"
    assert row["gate_passed"] is None, "no gate executed here, so no gate verdict"
    assert row["outcome_class"] == "neutral"
    assert row["self_reported_status"] == "completed"
    notes = row["notes"] or ""
    assert "dream cycle completed" in notes, "the job's own account must survive settlement"
    assert "staged=2" in notes


# ---------------------------------------------------------------------------
# Missing declared modules fail closed; ordinary tasks still dispatch
# ---------------------------------------------------------------------------


def test_declared_but_unregistered_module_records_adverse_fire_failure(
    database: SqliteStore,
) -> None:
    """A missing executor must never fall through to a favourable mock run."""
    # improve.dispatcher and lab.jobs are now REGISTERED (SI-loop revival,
    # operator approval 2026-08-12), so the negative control uses a module that
    # is genuinely declared-but-unregistered — the property under test is the
    # mechanism (missing executor -> adverse fire), not any one module.
    unregistered_module = "omniagentos.scheduler.__unregistered_control__"
    template = improve_dispatcher_routine()
    template["name"] = "unregistered-module-control"
    template["task_template"]["input"]["module"] = unregistered_module
    routine = RoutinesStore(database).create_routine(template)

    result = tick(database, load_policy(), now=DISPATCHER_NOW)

    fired = [e for e in result["fired"] if e["routine_id"] == routine["id"]]
    assert len(fired) == 1, result
    entry = fired[0]
    assert entry["fired"] is False
    assert entry["reason"].startswith("fire_failed: ")
    assert unregistered_module in entry["reason"]
    assert "task_id" not in entry
    assert "run_id" not in entry

    runs = RoutinesStore(database).list_runs(routine["id"])
    assert len(runs) == 1
    assert runs[0]["stop_reason"] == "fire_failed"
    assert runs[0]["outcome_class"] == "adverse"
    assert runs[0]["gate_passed"] is False
    assert runs[0]["accepted"] is False
    assert runs[0]["finished_at"] == "2026-07-29T12:00:00Z"
    assert result["settled"] == {"checked": 0, "settled": [], "errors": []}

    task_count = database._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    run_count = database._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert task_count == 0
    assert run_count == 0


def test_routine_without_declared_module_still_enqueues_a_task(
    database: SqliteStore,
) -> None:
    """No input.module means a regular dispatched task, not a broken built-in."""
    routines = RoutinesStore(database)
    routine = routines.create_routine(
        {
            "name": "regular-dispatched-task",
            "description": "A normal scheduler-dispatched task.",
            "trigger_type": "cron",
            "trigger_config": {"cron": "*/5 * * * *"},
            "task_template": {
                "title": "Regular dispatched task",
                "harness": "mock",
                "input": {"topic": "scheduler truth"},
            },
            "gate_type": "test_command",
            "gate_config": {
                "command": "pytest tests/scheduler/test_builtin_jobs.py",
                "expected_exit_code": 0,
            },
            "hard_cap_type": "budget_usd",
            "hard_cap_value": 1.0,
            "notification_target": {"channel": "desktop"},
            "status": "active",
        }
    )

    result = tick(database, load_policy(), now=DISPATCHER_NOW)

    fired = [e for e in result["fired"] if e["routine_id"] == routine["id"]]
    assert len(fired) == 1, result
    entry = fired[0]
    assert entry["fired"] is True
    assert entry["task_id"]
    assert entry["run_id"]
    assert "builtin" not in entry

    runs = routines.list_runs(routine["id"])
    assert len(runs) == 1
    assert runs[0]["run_id"] == entry["run_id"]
    assert runs[0]["finished_at"] is None
    assert runs[0]["outcome_class"] is None


# ---------------------------------------------------------------------------
# Failure is recorded, never raised out of tick
# ---------------------------------------------------------------------------


def test_builtin_failure_is_recorded_not_raised(
    database: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store path under a read-only parent → job fails; tick still returns."""
    ensure_dream_cycle_routine(database)

    # Spec A3: path under a read-only parent (no import of builtin_jobs /
    # run_dream_cycle / Lifecycle — this file must not import the mechanism).
    ro_parent = tmp_path / "ro_parent"
    ro_parent.mkdir()
    blocked = ro_parent / "memlife_store"
    monkeypatch.setenv(ENV_STORE, str(blocked))
    ro_parent.chmod(0o555)

    try:
        result = tick(database, load_policy(), now=DUE_NOW)
    finally:
        ro_parent.chmod(0o755)

    assert isinstance(result, dict)
    assert "fired" in result
    fired = result["fired"]
    assert len(fired) == 1
    assert fired[0]["fired"] is True  # firing happened; the job reported failure

    routines = RoutinesStore(database)
    runs = routines.list_runs(fired[0]["routine_id"])
    assert len(runs) == 1
    assert runs[0]["accepted"] == 0 or runs[0]["accepted"] is False
    assert runs[0]["stop_reason"] == "builtin_failed"
    assert "dream cycle raised" in (runs[0]["notes"] or "")


# ---------------------------------------------------------------------------
# NO_INPUT is NEUTRAL — three quiet nights must not auto-pause, and must not
# be scored as three successes either
# ---------------------------------------------------------------------------


def test_no_input_cycle_is_neutral_not_accepted(
    database: SqliteStore,
    memlife_root: Path,
) -> None:
    """Empty store → NO_INPUT → a non-result; a 4th night still fires.

    Both booleans are wrong. If NO_INPUT were marked failed, three quiet nights
    would trip the 50% auto-pause floor and the dream cycle would never run
    again. If it were marked accepted — as it was — a dream cycle that never
    saw input again would report 100% acceptance forever, which is the same
    invisibility that hid a loop parking every tick.
    """
    routine = ensure_dream_cycle_routine(database)
    # Layout exists; events.jsonl is absent → NO_INPUT (not COMPLETED).
    assert not (memlife_root / "episodic" / "events.jsonl").exists()

    routines = RoutinesStore(database)
    for day in (1, 2, 3):
        now = datetime(2026, 7, day, 3, 0, tzinfo=UTC)
        result = tick(database, load_policy(), now=now)
        assert len(result["fired"]) == 1, result
        assert result["fired"][0]["fired"] is True

    runs = routines.list_runs(routine["id"])
    assert len(runs) == 3
    for row in runs:
        assert row["accepted"] is None, "a quiet night is not an acceptance"
        assert row["gate_passed"] is None
        assert row["outcome_class"] == "neutral"
        assert row["stop_reason"] == "loop_idle_no_work"
        assert "dream cycle no_input" in (row["notes"] or "")

    status = routines.get_routine(routine["id"])
    assert status is not None
    assert status["status"] == "active", status.get("auto_pause_reason")
    # Three non-results leave the acceptance denominator EMPTY, which is
    # unknown — not 0%, and not 100%.
    assert status["neutral_runs"] == 3
    assert status["acceptance_rate"] is None

    # 4th night still due and fires (would be skipped if auto-paused).
    fourth = tick(
        database,
        load_policy(),
        now=datetime(2026, 7, 4, 3, 0, tzinfo=UTC),
    )
    assert len(fourth["fired"]) == 1
    assert fourth["fired"][0]["fired"] is True
    assert len(routines.list_runs(routine["id"])) == 4
