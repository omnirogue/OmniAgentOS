"""M3 — one slow project must not head-of-line block every other project.

The defect
----------
``routines_tick.tick`` fired every due routine in a single sequential loop, and
a built-in firing was EXECUTED inside that loop. A loop tick is a subprocess
that may legitimately run for its whole ``timeout_s`` (up to an hour —
``loop_jobs.MAX_TIMEOUT_S``), so with one loop routine per project the slowest
project in the table set the dispatch latency of every project behind it. Two
projects, one of them slow, and the second one simply did not run.

The decisive assertion
----------------------
:func:`test_a_slow_loop_does_not_delay_another_projects_dispatch` gives the two
projects' loop workers a RENDEZVOUS: each one waits for the other to start. If
the tick executes firings one after another, the first worker waits for a
worker that will not be launched until it returns, the barrier times out, and
both record ``overlapped=False``. Nothing is timing-sensitive — sequential
dispatch cannot pass this test at any speed, and concurrent dispatch passes it
instantly.

The three properties an async dispatch must not lose (and each one's test):

* **no double-fire** — ``test_an_overlapping_tick_does_not_refire_a_running_loop``
  drives a second tick from INSIDE the running worker, exactly as an operator
  running the module by hand while the launchd job is mid-flight would. It must
  find the trigger already served: one worker launch, one ``routine_runs`` row.
* **no premature settlement** — the same test asserts that the overlapping tick
  settles nothing: a firing that is still executing has no claim yet, so there
  is nothing for a gate to judge.
* **no lost settlement** — ``test_a_dispatched_firing_is_still_settled_by_its_own_tick``
  asserts the row a slow firing produces is settled by the tick that fired it,
  which is what the drain-before-settle step exists for.

Counterfeits that must make these RED
-------------------------------------
- executing the built-in inline in the DUE-loop again (the defect): the
  rendezvous test times out.
- stamping ``last_fired`` after the work instead of at the claim: the
  overlapping tick fires a second worker for the same loop instance.
- submitting to the pool without draining it before ``settle_pending``: the
  settlement test finds ``finished_at IS NULL``.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler import loop_jobs, routines_tick
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.support.db_template import make_store

DUE_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
PROJECT_A = "prj_alpha"
PROJECT_B = "prj_beta"
#: The pool-width knob, spelled out here rather than imported: this file must
#: still COLLECT and RUN against a build that has no dispatch pool at all, so
#: the revert-check shows behavioural failures instead of AttributeErrors.
ENV_CONCURRENCY = "OMNIAGENTOS_ROUTINE_DISPATCH_CONCURRENCY"
#: Bounds the RED case only: a sequential tick deadlocks the rendezvous and this
#: is how long it takes to say so. The green case never waits.
RENDEZVOUS_TIMEOUT_S = 10.0


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _worker_report(instance_id: str, status: str = "completed") -> str:
    return json.dumps(
        {
            "instance_id": instance_id,
            "template": "poll_classify_act_verify",
            "status": status,
            "detail": "",
            "effects": [],
            "approval_id": None,
            "resumed": False,
            "accepted": True,
        }
    )


def _loop_routine(name: str, instance_id: str, project_id: str) -> dict[str, Any]:
    """A loop routine scoped to *project_id* — the shape production seeds."""
    return {
        "name": name,
        "description": f"Loop for {project_id}",
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/5 * * * *"},
        "project_id": project_id,
        "task_template": {
            "title": f"Loop tick: {instance_id}",
            "harness": "mock",
            "input": {
                "module": loop_jobs.LOOP_MODULE,
                "kind": "loop",
                "template": "poll_classify_act_verify",
                "instance_id": instance_id,
                "instance_module": f"omniagentos_loops.instances.{instance_id}",
                "params": {},
            },
        },
        "gate_type": "test_command",
        "gate_config": {
            # The INSTANCE's own suite. A loop gated on anything under
            # tests/scheduler/ is refused at creation: that tree grades the
            # scheduler→worker mechanism shared by every loop, so it passes
            # whatever the instance produced (see
            # tests/scheduler/test_loop_gate_refusal.py). Nothing executes it
            # here — the fixture below pins the gate workspace off.
            "command": f"pytest loops/tests/instances/test_{instance_id}.py",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


@pytest.fixture(autouse=True)
def _no_gate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settlement executes the declared gate when a workspace resolves.

    Same pin as ``tests/scheduler/test_loop_jobs.py``: this file asserts WHEN
    firings execute and WHETHER they are settled, not what a gate rules, and an
    operator box exports ``OMNIAGENTOS_GATE_WORKSPACE`` from
    ``scripts/launch-env.sh`` — without this pin the same test would really run
    pytest inside a worktree.
    """
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    monkeypatch.delenv(ENV_CONCURRENCY, raising=False)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "control.sqlite3")
    handle = make_store(SqliteStore, db_path)
    now = utc_now_iso()
    with handle._lock:
        for project_id in (PROJECT_A, PROJECT_B):
            handle._connection.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                (project_id, f"Project {project_id}", now),
            )
        handle._connection.commit()
    yield handle
    handle.close()


@pytest.fixture
def fake_worker_binary(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Pretend the loops venv is installed; the subprocess itself is stubbed."""
    worker = tmp_path / "loop-worker"
    worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(loop_jobs, "_loop_worker_path", lambda: worker)
    return worker


def _instance_of(argv: list[str]) -> str:
    return argv[argv.index("--instance") + 1]


def _tick(store: SqliteStore) -> dict[str, Any]:
    return tick(store, load_policy(), now=DUE_NOW)


# ---------------------------------------------------------------------------
# Decisive: two projects' loops overlap instead of queueing
# ---------------------------------------------------------------------------


def test_a_slow_loop_does_not_delay_another_projects_dispatch(
    store: SqliteStore,
    fake_worker_binary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project B's loop must start while project A's loop is still running.

    Each stubbed worker waits at a two-party barrier, so it can only return if
    the OTHER project's worker has also been launched. Sequential in-tick
    execution makes that impossible by construction.
    """
    routines = RoutinesStore(store)
    routines.create_routine(_loop_routine("loop-alpha", "alpha_loop", PROJECT_A))
    routines.create_routine(_loop_routine("loop-beta", "beta_loop", PROJECT_B))

    rendezvous = threading.Barrier(2, timeout=RENDEZVOUS_TIMEOUT_S)
    overlapped: dict[str, bool] = {}

    def fake_run(argv, **kwargs):
        instance = _instance_of(list(argv))
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            overlapped[instance] = False
        else:
            overlapped[instance] = True
        return _FakeCompleted(_worker_report(instance))

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)

    started = time.monotonic()
    result = _tick(store)
    elapsed = time.monotonic() - started

    assert overlapped == {"alpha_loop": True, "beta_loop": True}, (
        "each project's loop must be dispatched without waiting for the other "
        f"to finish; observed {overlapped}"
    )
    # The barrier only breaks on timeout, so a green run is also a fast run.
    assert elapsed < RENDEZVOUS_TIMEOUT_S

    fired = [entry for entry in result["fired"] if entry.get("fired")]
    assert len(fired) == 2, result["fired"]
    assert all(entry["dispatched"] is True for entry in fired), fired
    # The claim is complete the moment the firing is dispatched: a caller
    # reading the summary never has to wait for the work to know what fired.
    assert all(entry["run_id"].startswith("btrun_") for entry in fired), fired


# ---------------------------------------------------------------------------
# Idempotency: an overlapping tick must not fire the same loop twice
# ---------------------------------------------------------------------------


def test_an_overlapping_tick_does_not_refire_a_running_loop(
    store: SqliteStore,
    fake_worker_binary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second tick, driven WHILE the first one's loop is executing, must skip.

    Realistic because it is what an operator does: ``python -m
    omniagentos.scheduler.routines_tick`` by hand while the launchd job is
    already mid-flight on a long loop. The firing is claimed (``last_fired``
    stamped) before the work starts precisely so this cannot launch a second
    worker for the same loop instance — and the in-flight row must not be
    settled by the overlapping tick either, because its job has not reported
    anything yet.
    """
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine("loop-alpha", "alpha_loop", PROJECT_A))

    calls: list[list[str]] = []
    overlapping: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if len(calls) == 1:
            # Mid-flight: a second, independent tick of the same database.
            overlapping["result"] = _tick(store)
        return _FakeCompleted(_worker_report(_instance_of(argv)))

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)

    _tick(store)

    assert len(calls) == 1, "an overlapping tick must not launch a second worker"

    nested = overlapping["result"]
    assert [entry for entry in nested["fired"] if entry.get("fired")] == []
    reasons = [entry["reason"] for entry in nested["skipped"]]
    assert reasons == ["cron trigger not due"], nested["skipped"]
    # No premature settlement: the in-flight firing has a run_id but no claim,
    # so it is invisible to settlement rather than judged before it finished.
    assert nested["settled"]["settled"] == []

    rows = routines.list_runs(routine["id"])
    assert len(rows) == 1, rows


# ---------------------------------------------------------------------------
# Settlement: dispatching must not lose the verdict
# ---------------------------------------------------------------------------


def test_a_dispatched_firing_is_still_settled_by_its_own_tick(
    store: SqliteStore,
    fake_worker_binary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick that fires a slow loop is still the tick that settles it.

    The pool is drained before the settle pass, so the claim has landed by the
    time ``settle_pending`` runs. Without the drain the row would still be
    unwritten — pending, unjudged, and invisible until some later tick.
    """
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine("loop-alpha", "alpha_loop", PROJECT_A))

    def fake_run(argv, **kwargs):
        time.sleep(0.2)
        return _FakeCompleted(_worker_report(_instance_of(list(argv))))

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)

    result = _tick(store)

    rows = routines.list_runs(routine["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["self_reported_status"] == "completed", row
    assert row["finished_at"] is not None, "the firing tick must settle what it fired"
    # No gate workspace here, so the claim stands uncorroborated: NULL/NULL,
    # out of the acceptance floor. Unchanged by dispatching — the composition
    # is still settlement's, not the worker's.
    assert row["gate_passed"] is None
    assert row["accepted"] is None
    assert row["stop_reason"] == "gate_evidence_unavailable"
    assert [entry["id"] for entry in result["settled"]["settled"]] == [row["id"]]


def test_a_failing_builtin_still_records_its_own_row(
    store: SqliteStore,
    fake_worker_binary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job that raises on the pool thread is recorded, not swallowed.

    ``_execute_builtin`` absorbs the exception exactly as the synchronous path
    did; moving the call onto another thread must not turn a failed loop into a
    firing with no row.
    """
    routines = RoutinesStore(store)
    routine = routines.create_routine(_loop_routine("loop-alpha", "alpha_loop", PROJECT_A))

    def fake_run(argv, **kwargs):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(loop_jobs.subprocess, "run", fake_run)

    result = _tick(store)

    assert [entry for entry in result["fired"] if entry.get("fired")], result["fired"]
    assert "dispatch_error" not in result["fired"][0], result["fired"][0]
    rows = routines.list_runs(routine["id"])
    assert len(rows) == 1
    assert rows[0]["outcome_class"] == "adverse", rows[0]
    assert "worker exploded" in (rows[0]["notes"] or "")


# ---------------------------------------------------------------------------
# The pool's width is bounded and configurable
# ---------------------------------------------------------------------------


#: ``"default"`` resolves inside the test body, never at import: this module's
#: decisive tests must still COLLECT against a build without the knob, so the
#: revert-check can show them failing on behaviour rather than on an
#: AttributeError raised while pytest was still reading the file.
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "default"),
        ("", "default"),
        ("1", 1),
        ("4", 4),
        ("0", "default"),
        ("-3", "default"),
        ("nonsense", "default"),
        ("999", "default"),
    ],
)
def test_dispatch_concurrency_is_bounded(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int | str
) -> None:
    """An unbounded or nonsensical fan-out is a different outage, not a fix."""
    default = routines_tick.DEFAULT_DISPATCH_CONCURRENCY
    assert routines_tick.ENV_DISPATCH_CONCURRENCY == ENV_CONCURRENCY
    assert routines_tick.MAX_DISPATCH_CONCURRENCY < 999
    if value is None:
        monkeypatch.delenv(ENV_CONCURRENCY, raising=False)
    else:
        monkeypatch.setenv(ENV_CONCURRENCY, value)
    assert routines_tick._dispatch_concurrency() == (default if expected == "default" else expected)
