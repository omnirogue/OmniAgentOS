"""Tests for GET /api/projects/{id}/activity and its aggregation logic.

Builds tasks/runs/steps/approvals/events directly against the real SQLite
schema (mirroring tests/projects/test_routes.py's pattern) rather than
inventing new storage -- the whole point of this endpoint is to aggregate
what's already there.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.projects.activity import build_project_activity


@pytest.fixture(autouse=True)
def _sandbox_var_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GET /activity's best-effort on-disk log projection is real I/O against
    OMNIAGENTOS_VAR_DIR (see omniagentos.projects.activity.project_pending_activity);
    sandbox it to tmp_path so an ordinary test run never scatters
    var/projects/<random-id>/ directories into the real repo var/. The
    projector itself is covered directly by test_activity_log.py."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _seed_task(store: SqliteStore, project_id: str, *, suffix: str, title: str, ts: str) -> str:
    task_id = f"tsk_{suffix}"
    store.create_task(
        {
            "id": task_id,
            "title": title,
            "state": "ready",
            "project_id": project_id,
            "created_at": ts,
            "updated_at": ts,
        }
    )
    return task_id


def _seed_run(
    store: SqliteStore, task_id: str, *, suffix: str, ts: str, state: str = "queued"
) -> str:
    run_id = f"run_{suffix}"
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "harness": "mock",
            "trace_id": f"tr_{suffix}",
            "queued_at": ts,
            "created_at": ts,
            "updated_at": ts,
        }
    )
    if state != "queued":
        store.update_run(run_id, {"state": state})
    return run_id


def test_missing_project_activity_is_404(asgi_client: httpx.AsyncClient) -> None:
    resp = _run(asgi_client.get("/api/projects/proj_missing/activity"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_empty_project_activity_is_a_clean_empty_state(asgi_client: httpx.AsyncClient) -> None:
    created = _run(asgi_client.post("/api/projects", json={"name": "Empty"}))
    project_id = created.json()["id"]

    resp = _run(asgi_client.get(f"/api/projects/{project_id}/activity"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == project_id
    assert body["project_name"] == "Empty"
    assert body["tasks"] == []
    assert body["activity_log"] == []
    assert body["summary"] == {
        "tasks": 0,
        "runs": 0,
        "running": 0,
        "awaiting_approval": 0,
        "completed": 0,
        "failed": 0,
    }
    assert body["generated_at"]


def test_activity_reflects_task_run_step_and_approval_progress(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    created = _run(asgi_client.post("/api/projects", json={"name": "Acme"}))
    project_id = created.json()["id"]
    now = utc_now_iso()

    task_id = _seed_task(store, project_id, suffix="a", title="Ship feature", ts=now)
    run_id = _seed_run(store, task_id, suffix="a", ts=now, state="running")
    store.insert_event(
        "run.updated",
        "runner:w1",
        "running",
        target_type="run",
        target_id=run_id,
        payload={"state": "running"},
    )
    store.upsert_step(
        run_id,
        0,
        {
            "name": "setup",
            "action_class": "sandboxed_creation",
            "status": "completed",
            "started_at": now,
            "finished_at": now,
        },
    )
    store.insert_event(
        "step.updated",
        "runner:w1",
        "completed",
        target_type="run",
        target_id=run_id,
        payload={"seq": 0},
    )
    store.upsert_step(
        run_id,
        1,
        {"name": "agent", "action_class": "consequential", "status": "started", "started_at": now},
    )
    store.insert_event(
        "step.updated",
        "runner:w1",
        "started",
        target_type="run",
        target_id=run_id,
        payload={"seq": 1},
    )

    approval_id = new_id("apr")
    store.create_approval(
        {
            "id": approval_id,
            "run_id": run_id,
            "task_id": task_id,
            "step_seq": 1,
            "action_class": "consequential",
            "proposed_action": "write file",
            "created_at": now,
        }
    )
    store.insert_event(
        "approval.requested",
        "runner:w1",
        "requested",
        target_type="run",
        target_id=run_id,
        payload={"seq": 1, "action_class": "consequential", "proposed_action": "write file"},
    )
    store.decide_approval(approval_id, "approved", "operator", None)
    store.insert_event(
        "approval.decided",
        "api",
        "approval.decided",
        target_type="approval",
        target_id=approval_id,
        payload={
            "approval_id": approval_id,
            "run_id": run_id,
            "state": "approved",
            "action_class": "consequential",
        },
    )

    resp = _run(asgi_client.get(f"/api/projects/{project_id}/activity"))
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["tasks"]) == 1
    task_row = body["tasks"][0]
    assert task_row["id"] == task_id
    assert task_row["title"] == "Ship feature"
    assert len(task_row["runs"]) == 1

    run_row = task_row["runs"][0]
    assert run_row["id"] == run_id
    assert run_row["state"] == "running"
    assert run_row["steps_done"] == 1
    assert run_row["steps_total"] == 2
    assert run_row["current_step"]["seq"] == 1
    assert run_row["current_step"]["name"] == "agent"
    assert len(run_row["steps"]) == 2

    # log_tail is newest-first. It draws on get_events_for_run (target_type='run'
    # only), so approval.decided -- persisted with target_type='approval' by the
    # decide-approval route -- surfaces in the merged activity_log below instead.
    tail_lines = [entry["line"] for entry in run_row["log_tail"]]
    assert tail_lines[0] == "approval requested (consequential): write file"
    assert "step 1 (agent) started" in tail_lines
    assert "step 0 (setup) completed" in tail_lines
    assert "run running" in tail_lines

    # The merged project-wide feed carries the same substance, labeled by task,
    # newest-first, and includes the approval.decided leg (target_type='approval').
    merged_lines = [entry["line"] for entry in body["activity_log"]]
    assert merged_lines[0] == "Ship feature: approval approved"
    assert body["activity_log"][0]["task_id"] == task_id
    assert body["activity_log"][0]["run_id"] == run_id
    ids = [entry["id"] for entry in body["activity_log"]]
    assert ids == sorted(ids, reverse=True)

    assert body["summary"] == {
        "tasks": 1,
        "runs": 1,
        "running": 1,
        "awaiting_approval": 0,
        "completed": 0,
        "failed": 0,
    }


def test_activity_is_scoped_to_the_requested_project(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    project_a = _run(asgi_client.post("/api/projects", json={"name": "Proj A"})).json()["id"]
    project_b = _run(asgi_client.post("/api/projects", json={"name": "Proj B"})).json()["id"]
    now = utc_now_iso()
    task_a = _seed_task(store, project_a, suffix="a", title="Task A", ts=now)
    task_b = _seed_task(store, project_b, suffix="b", title="Task B", ts=now)
    run_a = _seed_run(store, task_a, suffix="a", ts=now)
    run_b = _seed_run(store, task_b, suffix="b", ts=now)
    store.insert_event(
        "run.updated", "runner:w1", "queued", target_type="run", target_id=run_a, payload={}
    )
    store.insert_event(
        "run.updated", "runner:w1", "queued", target_type="run", target_id=run_b, payload={}
    )

    body_a = _run(asgi_client.get(f"/api/projects/{project_a}/activity")).json()
    assert [t["id"] for t in body_a["tasks"]] == [task_a]
    assert all(entry["task_id"] == task_a for entry in body_a["activity_log"])

    body_b = _run(asgi_client.get(f"/api/projects/{project_b}/activity")).json()
    assert [t["id"] for t in body_b["tasks"]] == [task_b]
    assert all(entry["task_id"] == task_b for entry in body_b["activity_log"])


def test_tasks_and_runs_are_ordered_newest_first(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    project_id = _run(asgi_client.post("/api/projects", json={"name": "Ordered"})).json()["id"]
    older = "2020-01-01T00:00:00Z"
    newer = "2030-01-01T00:00:00Z"
    task_old = _seed_task(store, project_id, suffix="old", title="Old task", ts=older)
    task_new = _seed_task(store, project_id, suffix="new", title="New task", ts=newer)
    _seed_run(store, task_old, suffix="old1", ts=older)
    _seed_run(store, task_new, suffix="new1", ts="2030-01-01T00:00:01Z")
    _seed_run(store, task_new, suffix="new2", ts=newer)

    body = _run(asgi_client.get(f"/api/projects/{project_id}/activity")).json()
    assert [t["id"] for t in body["tasks"]] == [task_new, task_old]
    new_task_runs = body["tasks"][0]["runs"]
    assert [r["id"] for r in new_task_runs] == ["run_new1", "run_new2"]


def test_activity_bounds_query_survives_a_global_limit(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    # F6-style: one OLD project's task/run must not be pushed out by many
    # NEWER runs belonging to a different project, since scoping happens in
    # SQL before LIMIT (list_tasks_for_project / list_runs_for_project).
    project_a = _run(asgi_client.post("/api/projects", json={"name": "Old A"})).json()["id"]
    project_b = _run(asgi_client.post("/api/projects", json={"name": "New B"})).json()["id"]
    task_a = _seed_task(store, project_a, suffix="a", title="A", ts="2000-01-01T00:00:00Z")
    _seed_run(store, task_a, suffix="a", ts="2000-01-01T00:00:00Z")
    task_b = _seed_task(store, project_b, suffix="b", title="B", ts="2030-01-01T00:00:00Z")
    for index in range(120):
        stamp = f"2030-01-01T00:{index // 60:02d}:{index % 60:02d}Z"
        store.enqueue_run(
            {
                "id": f"run_b_{index}",
                "task_id": task_b,
                "harness": "mock",
                "trace_id": f"tr_b_{index}",
                "queued_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )

    body = _run(asgi_client.get(f"/api/projects/{project_a}/activity")).json()
    assert [t["id"] for t in body["tasks"]] == [task_a]
    assert [r["id"] for r in body["tasks"][0]["runs"]] == ["run_a"]


def test_build_project_activity_ignores_a_nonexistent_project_id(store: SqliteStore) -> None:
    result = build_project_activity(store, "proj_ghost")
    assert result["tasks"] == []
    assert result["activity_log"] == []
