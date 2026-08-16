"""Per-worker concurrency: one worker executes up to K runs at once.

These prove the correctness-critical property the single-slot loop already had,
widened to K slots: no run is ever executed by two slots (exactly-once), the K
runs genuinely OVERLAP in time (not serialized), a K=1 worker is byte-for-byte
the old behavior, and a crashed/stale owner's in-flight run is still reclaimed by
another worker's liveness path.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    RunState,
    SandboxSpec,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies


class ConcurrencyProbeAdapter(MockAdapter):
    """Records every run it executes and gauges how many overlap in real time.

    ``barrier`` (when set) forces genuine overlap: each concurrent ``run`` blocks
    until ``barrier.parties`` of them have arrived, so a serialized executor would
    time out (BrokenBarrierError) instead of quietly passing.
    """

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self._lock = threading.Lock()
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.seen_run_ids: list[str] = []

    def run(self, input: AgentInput) -> AgentResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.seen_run_ids.append(input.run_id)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            return super().run(input)
        finally:
            with self._lock:
                self.active -= 1


def _dependencies(adapter: ConcurrencyProbeAdapter, root: Path) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda _action: PolicyDecision(requires_approval=False),
        sandbox_for_tools=lambda _harness, tools: SandboxSpec(
            level="workspace_write" if tools else "read_only"
        ),
        check_budget=lambda *_args: BudgetDecision(allowed=True),
        resolve_adapter=lambda _harness: adapter,
        append_manifest=lambda ledger, _manifest: str(Path(ledger) / "runs.jsonl"),
        render_run_note=lambda run, _steps, _manifest, _receipts, **_kwargs: (
            f"runs/{run['id']}.md",
            "done",
        ),
        write_note=lambda vault, relpath, _content: str(Path(vault) / relpath),
    )


def _runner(
    store: SqliteStore,
    adapter: ConcurrencyProbeAdapter,
    tmp_path: Path,
    *,
    worker_id: str = "w1",
    concurrency: int,
) -> Runner:
    return Runner(
        store,
        worker_id,
        dependencies=_dependencies(adapter, tmp_path),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
        concurrency=concurrency,
    )


def _enqueue(
    store: SqliteStore,
    run_id: str,
    *,
    delay_ms: int = 0,
    state: str = RunState.QUEUED.value,
    worker_id: str | None = None,
) -> None:
    now = utc_now_iso()
    task_id = new_id("tsk")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "concurrency test",
            "input_json": json.dumps({"prompt": "hi", "tools_allowed": []}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    plan = [
        {
            "name": "work",
            "kind": "agent",
            "action_class": ActionClass.SANDBOXED_CREATION.value,
            "params": {"adapter": "mock", "mock": {"delay_ms": delay_ms}},
        }
    ]
    row: dict[str, Any] = {
        "id": run_id,
        "task_id": task_id,
        "discipline_id": "code-changes",
        "harness": HarnessType.MOCK.value,
        "state": state,
        "plan_json": json.dumps(plan),
        "budget_json": "{}",
        "trace_id": f"trace-{run_id}",
        "queued_at": now,
        "created_at": now,
        "updated_at": now,
    }
    if worker_id is not None:
        row["worker_id"] = worker_id
    store.enqueue_run(row)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runner.db"
    migrate(str(path))
    return path


def test_default_concurrency_is_one() -> None:
    store = SqliteStore(":memory:")
    assert (
        Runner(
            store, "w1", dependencies=_dependencies(ConcurrencyProbeAdapter(), Path("."))
        ).concurrency
        == 1
    )


def test_eight_runs_overlap_and_execute_exactly_once(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    run_ids = [new_id("run") for _ in range(8)]
    for run_id in run_ids:
        _enqueue(store, run_id)
    # A barrier of 8 only clears if all 8 adapter.run calls are in flight together;
    # under a serialized executor it would time out and the runs would FAIL.
    adapter = ConcurrencyProbeAdapter(barrier=threading.Barrier(8))
    runner = _runner(store, adapter, tmp_path, concurrency=8)

    runner.run_forever(once=True)

    # Overlap: all eight cleared the barrier, so eight ran at the same instant.
    assert adapter.max_active == 8
    # Exactly-once: every run executed, none twice.
    assert sorted(adapter.seen_run_ids) == sorted(run_ids)
    assert len(adapter.seen_run_ids) == len(set(adapter.seen_run_ids)) == 8
    for run_id in run_ids:
        run = store.get_run(run_id)
        assert run and run["state"] == RunState.COMPLETED.value
        steps = store.get_steps(run_id)
        assert [s["status"] for s in steps] == ["completed"]


def test_single_slot_runs_one_at_a_time(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    run_ids = [new_id("run") for _ in range(3)]
    for run_id in run_ids:
        _enqueue(store, run_id, delay_ms=15)
    adapter = ConcurrencyProbeAdapter()
    runner = _runner(store, adapter, tmp_path, concurrency=1)

    # once=True on a single slot drains exactly one run (legacy semantics).
    runner.run_forever(once=True)
    assert adapter.max_active == 1
    assert len(adapter.seen_run_ids) == 1
    completed = [r for r in run_ids if store.get_run(r)["state"] == RunState.COMPLETED.value]  # type: ignore[index]
    assert len(completed) == 1


def test_load_twenty_slots_drain_sixty_runs_without_double_execution(
    db_path: Path, tmp_path: Path
) -> None:
    store = SqliteStore(str(db_path))
    run_ids = [new_id("run") for _ in range(60)]
    for run_id in run_ids:
        _enqueue(store, run_id, delay_ms=20)
    adapter = ConcurrencyProbeAdapter()
    runner = _runner(store, adapter, tmp_path, concurrency=20)

    # Repeated once-drains process min(remaining, 20) per pass; three passes clear 60.
    for _ in range(6):
        if all(
            store.get_run(r)["state"] == RunState.COMPLETED.value  # type: ignore[index]
            for r in run_ids
        ):
            break
        runner.run_forever(once=True)

    # Cap held (never more than 20 in flight) and concurrency actually happened.
    assert adapter.max_active <= 20
    assert adapter.max_active >= 2
    # Exactly-once across all 60: no run executed twice.
    assert sorted(adapter.seen_run_ids) == sorted(run_ids)
    assert len(adapter.seen_run_ids) == 60
    for run_id in run_ids:
        assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]


def test_stale_owner_in_flight_run_is_reclaimed(db_path: Path, tmp_path: Path) -> None:
    """A crashed worker leaves its run RUNNING with no heartbeat; a live K>1
    worker's liveness/reclaim path must still take it over and finish it."""
    store = SqliteStore(str(db_path))
    orphan = new_id("run")
    # Simulate the crash: RUNNING, owned by a worker that never heartbeats again.
    _enqueue(store, orphan, state=RunState.RUNNING.value, worker_id="dead")
    adapter = ConcurrencyProbeAdapter()
    runner = _runner(store, adapter, tmp_path, worker_id="rescuer", concurrency=4)

    runner.run_forever(once=True)

    run = store.get_run(orphan)
    assert run and run["worker_id"] == "rescuer"
    assert run["state"] == RunState.COMPLETED.value
    assert adapter.seen_run_ids == [orphan]


def test_stale_owner_unfinalized_terminal_run_is_finalized(db_path: Path, tmp_path: Path) -> None:
    """The finalization-liveness path still completes a dead owner's terminal-but-
    unfinalized run under K>1 (vault note gets written by the rescuer)."""
    store = SqliteStore(str(db_path))
    orphan = new_id("run")
    _enqueue(store, orphan, state=RunState.QUEUED.value)
    assert store.update_run(
        orphan,
        {
            "state": RunState.COMPLETED.value,
            "worker_id": "dead",
            "finished_at": utc_now_iso(),
        },
    )
    adapter = ConcurrencyProbeAdapter()
    runner = _runner(store, adapter, tmp_path, worker_id="rescuer", concurrency=4)

    runner.run_forever(once=True)

    run = store.get_run(orphan)
    assert run and run["worker_id"] == "rescuer"
    assert run["vault_note_path"] and run["manifest_path"]
    # Finalization must not re-run the agent.
    assert adapter.seen_run_ids == []
