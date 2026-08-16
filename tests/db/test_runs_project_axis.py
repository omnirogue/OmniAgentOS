"""NSC-C39-04: runs.project_id must be a live, writable DAL column.

Migration 058 (omniagentos/db/migrations/058_execution_contract.sql:221-233)
added ``runs.project_id`` (denormalized from ``tasks`` so the budget fan-out
at runner/core.py:1760 can scope by ``project:<id>`` without a second query,
and so evaluation queries are project-scopable) plus ``idx_runs_project``. The
column existed only as schema: the DAL allow-list (``_RUN_COLUMNS`` in
omniagentos/db/store.py) never admitted ``project_id``, so
``enqueue_run``/``update_run`` raised ``unknown columns`` on it and the fact
could never be written. This module proves the column is now real: writable
via the DAL, round-trippable, and filterable via ``list_runs``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from omniagentos import contracts
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


def temp_store() -> tuple[SqliteStore, str]:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    return make_store(SqliteStore, db_path), db_path


class TestRunsProjectAxis:
    """``runs.project_id`` is a first-class, DAL-writable column."""

    def test_enqueue_run_accepts_project_id(self) -> None:
        """enqueue_run must not raise on a row carrying project_id (was: raised
        ValueError('unknown columns: project_id') because _RUN_COLUMNS omitted
        it -- the hard-gate falsifier for NSC-C39-04)."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
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

            store.enqueue_run(
                {
                    "id": run_id,
                    "task_id": task_id,
                    "harness": "mock",
                    "state": "queued",
                    "trace_id": "trace-1",
                    "project_id": "proj_abc123",
                    "queued_at": contracts.utc_now_iso(),
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            run = store.get_run(run_id)
            assert run is not None
            assert run["project_id"] == "proj_abc123"
        finally:
            Path(db_path).unlink()

    def test_project_id_round_trips_through_update_run(self) -> None:
        """A run enqueued without a project (unscoped, the pre-058 default of
        NULL) can be bound to a project later via update_run without raising."""
        store, db_path = temp_store()
        try:
            run_id = contracts.new_id("run")
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
            assert run["project_id"] is None

            updated = store.update_run(run_id, {"project_id": "proj_later"})
            assert updated is True
            run = store.get_run(run_id)
            assert run is not None
            assert run["project_id"] == "proj_later"
        finally:
            Path(db_path).unlink()

    def test_list_runs_filters_by_project_id(self) -> None:
        """project_id must also be usable as a list_runs() filter (it goes
        through the same _RUN_COLUMNS allow-list as enqueue_run/update_run)."""
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
            for project_id in ("proj_x", "proj_y", None):
                store.enqueue_run(
                    {
                        "id": contracts.new_id("run"),
                        "task_id": task_id,
                        "harness": "mock",
                        "state": "queued",
                        "trace_id": contracts.new_id("trc"),
                        "project_id": project_id,
                        "queued_at": contracts.utc_now_iso(),
                        "created_at": contracts.utc_now_iso(),
                        "updated_at": contracts.utc_now_iso(),
                    }
                )

            matched = store.list_runs({"project_id": "proj_x"})
            assert len(matched) == 1
            assert matched[0]["project_id"] == "proj_x"
        finally:
            Path(db_path).unlink()

    def test_services_create_run_service_populates_project_id_from_task(self) -> None:
        """The API's create_run_service (omniagentos/api/services.py) must
        denormalize project_id from the owning task onto the new run row --
        this is the "live population" half of the objective, not just the
        allow-list."""
        from omniagentos.api.services import create_run_service
        from omniagentos.projects import ProjectStore

        store, db_path = temp_store()
        try:
            project = ProjectStore(store).create_project(
                {"id": "proj_services_test", "name": "Services Test Project"}
            )
            project_id = project["id"]
            task_id = contracts.new_id("tsk")
            store.create_task(
                {
                    "id": task_id,
                    "project_id": project_id,
                    "discipline_id": None,
                    "title": "Test Task",
                    "input_json": "{}",
                    "acceptance_json": "{}",
                    "state": "ready",
                    "risk": "low",
                    "created_at": contracts.utc_now_iso(),
                    "updated_at": contracts.utc_now_iso(),
                }
            )

            created = create_run_service(
                store,
                {},
                task_id=task_id,
                harness="mock",
            )

            run = store.get_run(created["id"])
            assert run is not None
            assert run["project_id"] == project_id
        finally:
            Path(db_path).unlink()
