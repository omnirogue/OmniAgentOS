"""Conformance tests for omniagentos.db.store.SqliteStore."""

from __future__ import annotations

import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos import contracts
from omniagentos.db import store as store_module
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


def temp_store() -> tuple[SqliteStore, str]:
    """Create a temp store with schema."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    return make_store(SqliteStore, db_path), db_path


class TestStoreProtocol:
    """Test that SqliteStore implements the Store protocol."""

    def test_isinstance_store(self) -> None:
        """SqliteStore must satisfy isinstance(store, contracts.Store)."""
        store = SqliteStore(":memory:")
        assert isinstance(store, contracts.Store)

    def test_all_protocol_methods_exist(self) -> None:
        """All Store protocol methods must be present."""
        store = SqliteStore(":memory:")
        required_methods = [
            "enqueue_run",
            "claim_next_run",
            "reclaim_stale_runs",
            "get_run",
            "update_run",
            "list_runs",
            "request_cancel",
            "requeue_paused_runs",
            "upsert_step",
            "get_steps",
            "idem_insert",
            "idem_get",
            "idem_complete",
            "idem_for_run",
            "create_task",
            "get_task",
            "update_task_state",
            "list_tasks",
            "insert_event",
            "get_events_after",
            "latest_event_id",
            "create_approval",
            "get_approval_for",
            "decide_approval",
            "void_pending_approvals",
            "list_approvals",
            "get_pause",
            "set_pause",
            "upsert_heartbeat",
            "get_heartbeats",
            "get_budget",
            "upsert_budget_usage",
            "list_budgets",
            "add_artifact",
            "get_artifacts",
            "list_disciplines",
            "create_discipline",
        ]
        for method in required_methods:
            assert hasattr(store, method), f"Missing method: {method}"


class TestRuns:
    """Test run management methods."""

    def test_enqueue_and_get_run(self) -> None:
        """Test enqueuing and retrieving a run."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")

            # Create task first (foreign key)
            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "queued",
                    "trace_id": "trace-1",
                    "queued_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            run = store.get_run(run_id)
            assert run is not None
            assert run["id"] == run_id
            assert run["state"] == "queued"
        finally:
            Path(db_path).unlink()

    def test_claim_next_run(self) -> None:
        """Test claiming the oldest queued run."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")
            worker_id = "worker-1"

            # Create task first
            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "queued",
                    "trace_id": "trace-1",
                    "queued_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            claimed = store.claim_next_run(worker_id)
            assert claimed is not None
            assert claimed["id"] == run_id
            assert claimed["state"] == "running"
            assert claimed["worker_id"] == worker_id
        finally:
            Path(db_path).unlink()

    def test_claim_next_run_prefers_lower_priority(self) -> None:
        """A later bottleneck run is claimed before an older normal run."""
        store, db_path = temp_store()
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            for suffix, priority, queued_at in (
                ("normal", 2, (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                ("bottleneck", 0, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ):
                task_id = f"tsk_{suffix}"
                store.create_task(
                    {
                        "id": task_id,
                        "title": suffix,
                        "state": "ready",
                        "created_at": queued_at,
                        "updated_at": queued_at,
                    }
                )
                store.enqueue_run(
                    {
                        "id": f"run_{suffix}",
                        "task_id": task_id,
                        "harness": "mock",
                        "state": "queued",
                        "priority": priority,
                        "trace_id": f"trace-{suffix}",
                        "queued_at": queued_at,
                        "created_at": queued_at,
                        "updated_at": queued_at,
                    }
                )

            claimed = store.claim_next_run("priority-worker")
            assert claimed is not None
            assert claimed["id"] == "run_bottleneck"
            assert claimed["priority"] == 0
        finally:
            Path(db_path).unlink()

    def test_claim_next_run_aging_floats_old_normal_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two aging intervals let priority-2 work overtake fresh 1 and 2 work."""
        monkeypatch.setattr(store_module, "AGING_INTERVAL_SECONDS", 900)
        monkeypatch.setattr(store_module, "AGING_PRIORITY_FLOOR", 0)
        now = datetime.now(UTC).replace(microsecond=0)
        old = (now - timedelta(seconds=1801)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        store, db_path = temp_store()
        try:
            for suffix, priority, queued_at in (
                ("old_normal", 2, old),
                ("fresh_fix", 1, fresh),
                ("fresh_normal", 2, fresh),
            ):
                task_id = f"tsk_{suffix}"
                store.create_task(
                    {
                        "id": task_id,
                        "title": suffix,
                        "state": "ready",
                        "created_at": queued_at,
                        "updated_at": queued_at,
                    }
                )
                store.enqueue_run(
                    {
                        "id": f"run_{suffix}",
                        "task_id": task_id,
                        "harness": "mock",
                        "state": "queued",
                        "priority": priority,
                        "trace_id": f"trace-{suffix}",
                        "queued_at": queued_at,
                        "created_at": queued_at,
                        "updated_at": queued_at,
                    }
                )

            claimed = store.claim_next_run("aging-worker")
            assert claimed is not None
            assert claimed["id"] == "run_old_normal"
            assert claimed["priority"] == 2  # aging changes only effective priority
        finally:
            Path(db_path).unlink()

    def test_update_run_with_fencing(self) -> None:
        """Test fencing: update_run with expect_worker must check worker_id."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")

            # Create task first
            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "queued",
                    "trace_id": "trace-1",
                    "queued_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.update_run(run_id, {"state": "running", "worker_id": "worker-1"})

            # Try to update with wrong worker_id - should return False
            success = store.update_run(run_id, {"state": "completed"}, expect_worker="wrong-worker")
            assert success is False

            # Verify the run was not updated
            run = store.get_run(run_id)
            assert run["state"] == "running"

            # Update with correct worker_id - should succeed
            success = store.update_run(run_id, {"state": "completed"}, expect_worker="worker-1")
            assert success is True

            run = store.get_run(run_id)
            assert run["state"] == "completed"
        finally:
            Path(db_path).unlink()


class TestSteps:
    """Test step management methods."""

    def test_upsert_step_fencing(self) -> None:
        """Test fencing: upsert_step with wrong expect_worker returns False."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")

            # Create task and run
            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "running",
                    "worker_id": "worker-1",
                    "trace_id": "trace-1",
                    "queued_at": contracts.utc_now_iso(),
                    "started_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            # Try to upsert with wrong worker - should fail
            success = store.upsert_step(
                run_id, 0, {"name": "step-1", "status": "started"}, expect_worker="wrong-worker"
            )
            assert success is False

            # Verify step was not created
            steps = store.get_steps(run_id)
            assert len(steps) == 0

            # Upsert with correct worker - should succeed
            success = store.upsert_step(
                run_id, 0, {"name": "step-1", "status": "started"}, expect_worker="worker-1"
            )
            assert success is True

            steps = store.get_steps(run_id)
            assert len(steps) == 1
        finally:
            Path(db_path).unlink()


class TestIdempotency:
    """Test idempotency receipt methods."""

    def test_idem_insert_unique(self) -> None:
        """Test that idem_insert returns True only on first insert."""
        store, db_path = temp_store()
        try:
            key = "idem-key-1"
            run_id = contracts.new_id("run")

            # First insert should succeed
            success1 = store.idem_insert(key, run_id, "step-1")
            assert success1 is True

            # Duplicate should return False
            success2 = store.idem_insert(key, run_id, "step-1")
            assert success2 is False
        finally:
            Path(db_path).unlink()

    def test_idem_complete_and_get(self) -> None:
        """Test completing an idempotency receipt and retrieving it."""
        store, db_path = temp_store()
        try:
            key = "idem-key-2"
            run_id = contracts.new_id("run")
            result = json.dumps({"status": "ok"})

            store.idem_insert(key, run_id, "step-2")
            store.idem_complete(key, result)

            receipt = store.idem_get(key)
            assert receipt is not None
            assert receipt["result_json"] == result
            assert receipt["completed_at"] is not None
        finally:
            Path(db_path).unlink()


class TestApprovals:
    """Test approval management methods."""

    def test_void_pending_approvals(self) -> None:
        """Test that void_pending_approvals only updates pending rows."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")

            # Create task and run first
            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "running",
                    "trace_id": "trace-1",
                    "queued_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            # Create multiple approvals with different states
            pending_ids = []
            for i in range(2):
                approval_id = contracts.new_id("apr")
                pending_ids.append(approval_id)
                store.create_approval(
                    {
                        "id": approval_id,
                        "run_id": run_id,
                        "task_id": task_id,
                        "step_seq": i,
                        "action_class": "sandboxed_creation",
                        "proposed_action": "test action",
                        "state": "pending",
                        "created_at": contracts.utc_now_iso(),
                    }
                )

            # Create one already-approved approval
            approved_id = contracts.new_id("apr")
            store.create_approval(
                {
                    "id": approved_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "step_seq": 2,
                    "action_class": "sandboxed_creation",
                    "proposed_action": "test action",
                    "state": "approved",
                    "created_at": contracts.utc_now_iso(),
                }
            )

            # Void pending approvals
            count = store.void_pending_approvals(run_id, "test note")

            # Should only void the pending ones
            assert count == 2

            # Verify states changed
            for approval_id in pending_ids:
                approval = store._connection.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                assert approval["state"] == "expired"
                assert approval["decision_note"] == "test note"

            # Verify approved one was not changed
            approved = store._connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approved_id,)
            ).fetchone()
            assert approved["state"] == "approved"
        finally:
            Path(db_path).unlink()


class TestTasks:
    """Test task management methods."""

    def test_update_task_state(self) -> None:
        """Test updating task state with guards."""
        store, db_path = temp_store()
        try:
            task_id = contracts.new_id("tsk")

            store.create_task(
                {
                    "id": task_id,
                    "title": "Test Task",
                    "state": "ready",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            # Update with valid expect list
            success = store.update_task_state(task_id, "queued", expect=["ready"])
            assert success is True

            task = store.get_task(task_id)
            assert task["state"] == "queued"

            # Try to update from wrong state - should fail
            success = store.update_task_state(task_id, "running", expect=["ready"])
            assert success is False

            # Verify state didn't change
            task = store.get_task(task_id)
            assert task["state"] == "queued"
        finally:
            Path(db_path).unlink()


class TestEvents:
    """Test event management methods."""

    def test_insert_event_returns_id(self) -> None:
        """Test that insert_event returns an autoincrement ID."""
        store, db_path = temp_store()
        try:
            event_id = store.insert_event(
                type="run.updated",
                actor="runner:w1",
                action="claim",
                target_type="run",
                target_id="run-123",
                payload={"state": "running"},
            )
            assert isinstance(event_id, int)
            assert event_id > 0
        finally:
            Path(db_path).unlink()

    def test_insert_event_without_execution_id_leaves_columns_null(self) -> None:
        """W2.6 (086): omitting execution_id must not force a value -- not every
        event has a lane execution to correlate with."""
        store, db_path = temp_store()
        try:
            event_id = store.insert_event(
                "run.updated", "runner", "claim", target_type="run", target_id="run-1"
            )
            row = store._connection.execute(
                "SELECT execution_id, sequence FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            assert row["execution_id"] is None
            assert row["sequence"] is None
        finally:
            Path(db_path).unlink()

    def test_insert_event_populates_execution_id_and_starts_sequence_at_one(self) -> None:
        store, db_path = temp_store()
        try:
            event_id = store.insert_event(
                "run.updated",
                "runner",
                "claim",
                target_type="run",
                target_id="run-1",
                execution_id="exec-1",
            )
            row = store._connection.execute(
                "SELECT execution_id, sequence FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            assert row["execution_id"] == "exec-1"
            assert row["sequence"] == 1
        finally:
            Path(db_path).unlink()

    def test_insert_event_sequence_is_gap_free_and_monotonic_per_execution(self) -> None:
        store, db_path = temp_store()
        try:
            for _ in range(5):
                store.insert_event(
                    "run.updated",
                    "runner",
                    "claim",
                    target_type="run",
                    target_id="run-1",
                    execution_id="exec-a",
                )
            rows = store._connection.execute(
                "SELECT sequence FROM events WHERE execution_id = 'exec-a' ORDER BY id ASC"
            ).fetchall()
            assert [row["sequence"] for row in rows] == [1, 2, 3, 4, 5]
        finally:
            Path(db_path).unlink()

    def test_insert_event_sequence_scoped_independently_per_execution_id(self) -> None:
        """Two executions interleaving writes must each get their own dense
        1, 2, 3, ... run -- one execution's writes must never steal or skip a
        sequence number that belongs to a different execution_id."""
        store, db_path = temp_store()
        try:
            for execution_id in ("exec-a", "exec-b", "exec-a", "exec-b", "exec-a"):
                store.insert_event(
                    "run.updated",
                    "runner",
                    "claim",
                    target_type="run",
                    target_id="run-1",
                    execution_id=execution_id,
                )
            a_rows = store._connection.execute(
                "SELECT sequence FROM events WHERE execution_id = 'exec-a' ORDER BY id ASC"
            ).fetchall()
            b_rows = store._connection.execute(
                "SELECT sequence FROM events WHERE execution_id = 'exec-b' ORDER BY id ASC"
            ).fetchall()
            assert [row["sequence"] for row in a_rows] == [1, 2, 3]
            assert [row["sequence"] for row in b_rows] == [1, 2]
        finally:
            Path(db_path).unlink()

    def test_insert_event_sequence_dense_and_gap_free_across_threads(self) -> None:
        """Race-safety per execution: N threads hammering the SAME execution_id
        on a file-backed store (one connection per thread, matching the dense-id
        guard above) must still produce the exact set {1..total} with no
        duplicate and no gap -- proof the sequence read-then-insert in
        ``_next_event_sequence`` is atomic with the INSERT, not a racy
        read-outside-the-transaction pattern."""
        store, db_path = temp_store()
        try:
            worker_count, writes_per_worker = 8, 40
            execution_id = "exec_concurrent"

            def write_events(worker: int) -> None:
                for i in range(writes_per_worker):
                    store.insert_event(
                        "test.event",
                        f"worker-{worker}",
                        "write",
                        target_type="run",
                        target_id="run_dense",
                        payload={"i": i},
                        execution_id=execution_id,
                    )

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                for future in [
                    pool.submit(write_events, worker) for worker in range(worker_count)
                ]:
                    future.result()

            total = worker_count * writes_per_worker
            rows = store._connection.execute(
                "SELECT sequence FROM events WHERE execution_id = ?", (execution_id,)
            ).fetchall()
            sequences = sorted(row["sequence"] for row in rows)
            assert sequences == list(range(1, total + 1)), (
                "sequence must be the exact dense set 1..N with no gap or duplicate"
            )
        finally:
            store.close()
            Path(db_path).unlink()


class TestProjectScopedQueries:
    """list_tasks_for_project / list_events_for_project / list_project_ids.

    Concrete SQLite extensions (not part of the frozen Store protocol) that
    back GET /api/projects/{id}/activity -- see omniagentos.projects.activity.
    """

    def _project_with_task_and_run(
        self, store: SqliteStore, *, name: str, ts: str
    ) -> tuple[str, str, str]:
        project_id = contracts.new_id("proj")
        store._write(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project_id, name, ts),
        )
        task_id = contracts.new_id("tsk")
        store.create_task(
            {
                "id": task_id,
                "title": name,
                "state": "ready",
                "project_id": project_id,
                "created_at": ts,
                "updated_at": ts,
            }
        )
        run_id = contracts.new_id("run")
        store.enqueue_run(
            {
                "id": run_id,
                "task_id": task_id,
                "harness": "mock",
                "trace_id": "tr",
                "queued_at": ts,
                "created_at": ts,
                "updated_at": ts,
            }
        )
        return project_id, task_id, run_id

    def test_list_tasks_for_project_orders_by_updated_at_desc_and_scopes(self) -> None:
        store = SqliteStore(":memory:")
        project_a, task_a, _ = self._project_with_task_and_run(
            store, name="A", ts="2020-01-01T00:00:00Z"
        )
        project_b, task_b, _ = self._project_with_task_and_run(
            store, name="B", ts="2020-01-01T00:00:00Z"
        )
        # A just got touched (e.g. a new run queued against it) -- it must
        # resurface to the top of ITS OWN project's list, and must never leak
        # into project B's.
        store._write(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", ("2030-01-01T00:00:00Z", task_a)
        )
        assert [t["id"] for t in store.list_tasks_for_project(project_a, limit=10)] == [task_a]
        assert [t["id"] for t in store.list_tasks_for_project(project_b, limit=10)] == [task_b]

    def test_list_events_for_project_merges_run_and_approval_events_newest_first(self) -> None:
        store = SqliteStore(":memory:")
        project_id, task_id, run_id = self._project_with_task_and_run(
            store, name="Acme", ts=contracts.utc_now_iso()
        )
        other_project, other_task, other_run = self._project_with_task_and_run(
            store, name="Other", ts=contracts.utc_now_iso()
        )

        e1 = store.insert_event(
            "run.updated", "runner", "queued", target_type="run", target_id=run_id, payload={}
        )
        approval_id = contracts.new_id("apr")
        store.create_approval(
            {
                "id": approval_id,
                "run_id": run_id,
                "task_id": task_id,
                "action_class": "read_only",
                "proposed_action": "look",
                "created_at": contracts.utc_now_iso(),
            }
        )
        e2 = store.insert_event(
            "approval.decided",
            "api",
            "approval.decided",
            target_type="approval",
            target_id=approval_id,
            payload={"run_id": run_id},
        )
        # Noise belonging to the OTHER project must never show up.
        store.insert_event(
            "run.updated", "runner", "queued", target_type="run", target_id=other_run, payload={}
        )
        other_approval = contracts.new_id("apr")
        store.create_approval(
            {
                "id": other_approval,
                "run_id": other_run,
                "task_id": other_task,
                "action_class": "read_only",
                "proposed_action": "look",
                "created_at": contracts.utc_now_iso(),
            }
        )
        store.insert_event(
            "approval.decided",
            "api",
            "approval.decided",
            target_type="approval",
            target_id=other_approval,
            payload={"run_id": other_run},
        )

        events = store.list_events_for_project(project_id, limit=100)
        assert [e["id"] for e in events] == [e2, e1]
        assert {e["target_type"] for e in events} == {"run", "approval"}
        assert other_project  # sanity: fixture id used only to seed noise above

    def test_list_events_for_project_respects_limit(self) -> None:
        store = SqliteStore(":memory:")
        project_id, _task_id, run_id = self._project_with_task_and_run(
            store, name="Acme", ts=contracts.utc_now_iso()
        )
        for _ in range(5):
            store.insert_event(
                "run.updated", "runner", "queued", target_type="run", target_id=run_id, payload={}
            )
        assert len(store.list_events_for_project(project_id, limit=2)) == 2
        assert len(store.list_events_for_project(project_id, limit=100)) == 5

    def test_list_project_ids_returns_every_project(self) -> None:
        store = SqliteStore(":memory:")
        project_a, _, _ = self._project_with_task_and_run(
            store, name="A", ts="2020-01-01T00:00:00Z"
        )
        project_b, _, _ = self._project_with_task_and_run(
            store, name="B", ts="2021-01-01T00:00:00Z"
        )
        assert store.list_project_ids() == [project_a, project_b]


class TestMigrations:
    """Test migration runner."""

    def test_migrate_idempotent(self) -> None:
        """Test that migrate() twice is a no-op the second time."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        version1 = migrate(db_path)
        version2 = migrate(db_path)

        assert version1 == version2
        Path(db_path).unlink()


class TestConcurrency:
    """Test concurrent/race conditions."""

    def test_shared_store_serializes_threads(self) -> None:
        """One API-style shared instance accepts concurrent writes safely."""
        store = SqliteStore(":memory:")
        worker_count = 8
        writes_per_worker = 100

        def write_events(worker: int) -> None:
            for sequence in range(writes_per_worker):
                store.insert_event(
                    "test.event",
                    f"worker-{worker}",
                    "write",
                    target_type="run",
                    target_id="run_shared",
                    payload={"sequence": sequence},
                )

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(write_events, worker) for worker in range(worker_count)]
            for future in futures:
                future.result()

        events = store.get_events_for_run("run_shared", limit=worker_count * writes_per_worker)
        assert len(events) == worker_count * writes_per_worker
        assert [event["id"] for event in events] == list(
            range(1, worker_count * writes_per_worker + 1)
        )

    def test_claim_next_run_racing(self) -> None:
        """Test that two workers can't both claim the same QUEUED run."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        run_id = contracts.new_id("run")
        task_id = contracts.new_id("tsk")

        # Create task and run that both workers will try to claim
        store0 = make_store(SqliteStore, db_path)
        store0.create_task(
            {
                "id": task_id,
                "title": "Test Task",
                "state": "ready",
                "created_at": contracts.utc_now_iso(),
                "updated_at": contracts.utc_now_iso(),
            }
        )

        store0.enqueue_run(
            {
                "id": run_id,
                "task_id": task_id,
                "harness": "mock",
                "state": "queued",
                "trace_id": "trace-1",
                "queued_at": contracts.utc_now_iso(),
                "created_at": contracts.utc_now_iso(),
                "updated_at": contracts.utc_now_iso(),
            }
        )

        results = []

        def claim_run(worker_id: str) -> None:
            store = SqliteStore(db_path)
            claimed = store.claim_next_run(worker_id)
            results.append((worker_id, claimed))

        # Have two workers race to claim
        t1 = threading.Thread(target=claim_run, args=("worker-1",))
        t2 = threading.Thread(target=claim_run, args=("worker-2",))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should have claimed it
        claimed_runs = [r for r in results if r[1] is not None]
        assert len(claimed_runs) == 1

        Path(db_path).unlink()


class TestReclaim:
    """Test reclaiming stale runs."""

    def test_reclaim_stale_runs(self) -> None:
        """Test that reclaim_stale_runs only reclaims genuinely stale runs."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        store = make_store(SqliteStore, db_path)
        task_id = contracts.new_id("tsk")

        # Create task first
        store.create_task(
            {
                "id": task_id,
                "title": "Test Task",
                "state": "ready",
                "created_at": contracts.utc_now_iso(),
                "updated_at": contracts.utc_now_iso(),
            }
        )

        # Create a running run with an old heartbeat
        old_run_id = contracts.new_id("run")
        old_timestamp = "2020-01-01T00:00:00Z"

        store.enqueue_run(
            {
                "id": old_run_id,
                "task_id": task_id,
                "harness": "mock",
                "state": "running",
                "worker_id": "old-worker",
                "trace_id": "trace-1",
                "queued_at": old_timestamp,
                "started_at": old_timestamp,
                "created_at": old_timestamp,
                "updated_at": old_timestamp,
            }
        )

        # Add a stale heartbeat
        store.upsert_heartbeat("old-worker", 1234, old_run_id)
        store._connection.execute(
            "UPDATE heartbeats SET last_beat_at = ? WHERE worker_id = ?",
            (old_timestamp, "old-worker"),
        )
        store._connection.commit()

        # Reclaim with a large stale_s value (so 2020 timestamp is considered stale)
        reclaimed = store.reclaim_stale_runs("new-worker", stale_s=1)

        # Should have reclaimed the old run
        assert len(reclaimed) > 0
        assert reclaimed[0]["id"] == old_run_id
        assert reclaimed[0]["worker_id"] == "new-worker"

        Path(db_path).unlink()


class TestConnectionLayer:
    """Phase 2: per-thread connections, reader/writer split, close(), WAL tick.

    The invariant these guard is the one SQLite will NOT enforce for us: the
    store opens connections with ``check_same_thread=False``, so a connection
    reused across threads produces silently interleaved transactions rather
    than an exception. ``TestConcurrency.test_shared_store_serializes_threads``
    stays the dense-id guard for ``:memory:``; these cover the file-backed path
    that actually gets one connection per thread.
    """

    @staticmethod
    def _connection_on_new_thread(store: SqliteStore) -> object:
        """The connection object ``store`` hands to a FRESH thread."""
        captured: list[object] = []
        thread = threading.Thread(target=lambda: captured.append(store._connection))
        thread.start()
        thread.join()
        return captured[0]

    def test_thread_local_connections_are_distinct(self) -> None:
        """A file-backed store hands every thread its own connection object."""
        store, db_path = temp_store()
        try:
            here = store._connection
            assert here is store._connection, "same thread must reuse its connection"

            other = self._connection_on_new_thread(store)
            assert other is not here
            assert self._connection_on_new_thread(store) is not other, (
                "each thread gets its own connection, not one shared spare"
            )
        finally:
            store.close()
            Path(db_path).unlink()

    def test_memory_store_falls_back_to_shared_connection(self) -> None:
        """``:memory:`` must NOT go per-thread: each such connect is a new empty DB.

        Per-thread connections to ``":memory:"`` would hand every thread a
        blank, unmigrated database, so the store keeps one shared handle and
        stays fully lock-serialized there.
        """
        memory = SqliteStore(":memory:")
        assert memory._connection is memory._connection
        assert self._connection_on_new_thread(memory) is memory._connection

        store, db_path = temp_store()
        try:
            assert self._connection_on_new_thread(store) is not store._connection
        finally:
            store.close()
            Path(db_path).unlink()

    def test_reads_do_not_block_writes(self) -> None:
        """Readers complete while another thread sits inside BEGIN IMMEDIATE.

        This is Phase 3's shape exactly: a worker holds the writer lock AND an
        open write transaction while its scope-lock scan runs. Before Phase 2
        every read took that same lock, so each of these reads would have waited
        out the whole transaction (measured p95 ~255 ms against a 250 ms hold).
        """
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
            task_id = contracts.new_id("tsk")
            now = contracts.utc_now_iso()
            store.create_task(
                {
                    "id": task_id,
                    "title": "T",
                    "state": "ready",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "queued",
                    "trace_id": "tr",
                    "queued_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
            )

            done = threading.Event()
            seen: list[object] = []

            def read() -> None:
                seen.append(store.get_run(run_id))
                seen.append(store.list_runs({}, limit=10))
                seen.append(store.latest_event_id())
                seen.append(store.get_events_after(0, limit=10))
                done.set()

            with store._lock:
                store._begin()  # a real open write transaction, not just the lock
                try:
                    store._connection.execute(
                        "UPDATE runs SET state = 'running' WHERE id = ?", (run_id,)
                    )
                    reader = threading.Thread(target=read, daemon=True)
                    reader.start()
                    assert done.wait(timeout=5.0), "reader blocked behind an open write transaction"
                finally:
                    store._rollback()
            reader.join(timeout=5.0)
            assert seen[0] is not None and seen[0]["id"] == run_id  # type: ignore[index]
            # The uncommitted UPDATE must not have been visible to the reader.
            assert seen[0]["state"] == "queued"  # type: ignore[index]
        finally:
            store.close()
            Path(db_path).unlink()

    def test_reader_admission_lock_is_not_the_writer_lock(self) -> None:
        """Readers are admitted through their own lock; a writer never gates them."""
        store, db_path = temp_store()
        try:
            assert store._read_lock is not store._lock
        finally:
            store.close()
            Path(db_path).unlink()

    def test_memory_reads_still_take_the_writer_lock(self) -> None:
        """The ``:memory:`` fallback stays fully serialized -- the trap's guard rail."""
        store = SqliteStore(":memory:")
        done = threading.Event()

        def read() -> None:
            store.latest_event_id()
            done.set()

        store._lock.acquire()
        try:
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            assert not done.wait(timeout=0.5), ":memory: reads must remain serialized"
        finally:
            store._lock.release()
        assert done.wait(timeout=5.0)

    def test_file_store_event_ids_stay_dense_across_threads(self) -> None:
        """The dense-id guard, on the per-thread-connection (file) path.

        ``insert_event`` returns ``cursor.lastrowid``; with one connection per
        thread that value is per-connection state, so this also proves no
        thread reads another's lastrowid.
        """
        store, db_path = temp_store()
        try:
            worker_count, writes_per_worker = 8, 100
            returned: list[int] = []
            returned_lock = threading.Lock()

            def write_events(worker: int) -> None:
                mine = [
                    store.insert_event(
                        "test.event",
                        f"worker-{worker}",
                        "write",
                        target_type="run",
                        target_id="run_dense",
                        payload={"sequence": sequence},
                    )
                    for sequence in range(writes_per_worker)
                ]
                with returned_lock:
                    returned.extend(mine)

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                for future in [pool.submit(write_events, worker) for worker in range(worker_count)]:
                    future.result()

            total = worker_count * writes_per_worker
            events = store.get_events_for_run("run_dense", limit=total)
            assert [event["id"] for event in events] == list(range(1, total + 1))
            assert sorted(returned) == list(range(1, total + 1)), (
                "every insert_event returned its own row id"
            )
        finally:
            store.close()
            Path(db_path).unlink()

    def test_begin_without_the_writer_lock_fails_loudly(self) -> None:
        """A hand-rolled transaction outside the lock must assert, not corrupt."""
        store, db_path = temp_store()
        try:
            raised = False
            try:
                store._begin()
            except AssertionError:
                raised = True
            else:
                store._rollback()
            assert raised, "_begin must refuse to run without the writer lock"

            with store._lock:  # the supported form still works
                store._begin()
                store._rollback()
        finally:
            store.close()
            Path(db_path).unlink()

    def test_holds_writer_lock_fails_closed_when_ownership_unmeasurable(self) -> None:
        """Unknown lock ownership must NOT be reported as held.

        Defect class: three-valued probe consumed as a favourable result.
        When ``_is_owned`` is absent, the guardrail cannot verify isolation;
        returning True (current) lets ``_begin`` proceed without the writer
        lock, which is exactly the silent corruption path the assert exists
        to block.

        Counterfeits this test catches:
        - ``return True if is_owned is None else ...`` (favourable non-result)
        - ``return True`` always (never checks)
        - ``return is_owned is not None`` without calling (method object is
          always truthy → reports held even when free)
        - ``return False`` always (breaks the held-lock positive case below)
        """
        store, db_path = temp_store()
        try:
            assert store._holds_writer_lock() is False
            with store._lock:
                assert store._holds_writer_lock() is True

            class UnmeasurableLock:
                """Supports acquire/release but offers no ownership probe."""

                def __init__(self) -> None:
                    self._inner = threading.RLock()

                def acquire(self, *args: object, **kwargs: object) -> bool:
                    return bool(self._inner.acquire(*args, **kwargs))

                def release(self) -> None:
                    self._inner.release()

                def __enter__(self) -> UnmeasurableLock:
                    self._inner.acquire()
                    return self

                def __exit__(self, *args: object) -> bool:
                    self._inner.release()
                    return False

            store._lock = UnmeasurableLock()  # type: ignore[assignment]
            # Even while holding the stand-in lock, ownership is unmeasurable:
            # must report False (fail closed), never True.
            with store._lock:
                assert store._holds_writer_lock() is False, (
                    "unmeasurable ownership must not be reported as held"
                )
                raised = False
                try:
                    store._begin()
                except AssertionError:
                    raised = True
                else:
                    store._rollback()
                assert raised, (
                    "_begin must fail closed when lock ownership cannot be verified"
                )
        finally:
            store.close()
            Path(db_path).unlink()

    def test_close_is_idempotent_and_rejects_later_use(self) -> None:
        store, db_path = temp_store()
        try:
            self._connection_on_new_thread(store)  # open a second connection
            store.close()
            store.close()  # idempotent

            failed = False
            try:
                store.latest_event_id()
            except RuntimeError:
                failed = True
            assert failed, "use-after-close must raise at the seam"
        finally:
            Path(db_path).unlink()

    def test_wal_checkpoint_truncates_the_log(self) -> None:
        store, db_path = temp_store()
        wal = Path(db_path + "-wal")
        try:
            for index in range(300):
                store.insert_event(
                    "test.event",
                    "w",
                    "write",
                    target_type="run",
                    target_id="run_wal",
                    payload={"index": index, "pad": "x" * 400},
                )
            assert wal.exists() and wal.stat().st_size > 0
            busy, _, _ = store.checkpoint_wal("TRUNCATE")
            assert busy == 0, "no other reader is open; the checkpoint must succeed"
            assert wal.stat().st_size == 0
            assert len(store.get_events_for_run("run_wal", limit=500)) == 300
        finally:
            store.close()
            Path(db_path).unlink(missing_ok=True)
            wal.unlink(missing_ok=True)
            Path(db_path + "-shm").unlink(missing_ok=True)

    def test_wal_checkpoint_tick_is_interval_gated(self) -> None:
        """The maintenance tick fires at most once per interval and never raises."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()
        store = make_store(SqliteStore, db_path, wal_checkpoint_interval_s=0.0)
        try:
            assert store.maybe_checkpoint_wal() is False, "0 disables the tick"

            store._wal_checkpoint_interval_s = 3600.0
            store._last_wal_checkpoint -= 7200.0
            assert store.maybe_checkpoint_wal() is True
            assert store.maybe_checkpoint_wal() is False, "second call is inside the interval"

            memory = SqliteStore(":memory:")
            assert memory.maybe_checkpoint_wal() is False, ":memory: has no WAL"
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                Path(db_path + suffix).unlink(missing_ok=True)
