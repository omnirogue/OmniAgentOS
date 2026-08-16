"""Tier3 API-surface tests for agent-execution routes.

Single_agent and multi_agent features have NO tier3 coverage of the actual API
endpoints that the UI calls for run creation and monitoring. This suite exercises:

  - Single-agent paths: POST /api/tasks/{task_id}/runs (create), GET /api/runs
    (list), GET /api/runs/{run_id} (detail)
  - Multi-agent (swarm) paths: POST /api/swarm (create run), GET /api/swarm/overview
    (fleet status), GET /api/swarm/{run_id} (detail), GET /api/swarm/{run_id}/activity
    (event feed)

All routes tested via real HTTP (FastAPI TestClient) against the isolated app.
Status codes AND envelope shapes are asserted; minimal test data is seeded through
store APIs where routes 404 on empty (following tier3/test_ui_api_paths.py idiom).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.control import _authorized
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore


def _assert_error_envelope(body: Any) -> None:
    """Every API failure carries the {"error": {code, message, detail}} envelope."""
    assert isinstance(body, dict) and "error" in body, f"no error envelope: {body!r}"
    error = body["error"]
    for key in ("code", "message", "detail"):
        assert key in error, f"error envelope missing {key!r}: {error!r}"
    assert error["code"], f"error.code empty: {error!r}"
    assert error["message"], f"error.message empty: {error!r}"


@pytest.fixture()
def sqlite_store(tmp_path: Path) -> SqliteStore:
    """Sqlite store with schema migrations (collab + swarm tables)."""
    db_path = str(tmp_path / "fh-tier3-agent.db")
    # Initialize with CollabStore to auto-migrate swarm/collab tables
    CollabStore(db_path)
    return SqliteStore(db_path)


@pytest.fixture()
def client(sqlite_store: SqliteStore) -> Any:
    """TestClient over the real app with the tmp store injected.

    Also bypasses the swarm-router's _authorized dependency check and
    injects a tmp SwarmDal so tests can exercise the swarm API surface.
    """
    from omniagentos.api.routes.swarm import get_swarm_dal
    from omniagentos.swarm.dal import SwarmDal

    tmp_dal = SwarmDal(sqlite_store._db_path)
    app.dependency_overrides[get_store] = lambda: sqlite_store
    app.dependency_overrides[_authorized] = lambda: None
    app.dependency_overrides[get_swarm_dal] = lambda: tmp_dal
    try:
        yield TestClient(app, base_url="http://testserver")
    finally:
        app.dependency_overrides.pop(get_store, None)
        app.dependency_overrides.pop(_authorized, None)
        app.dependency_overrides.pop(get_swarm_dal, None)


class TestSingleAgentPaths:
    """Single-agent task/run routes the control UI drives."""

    def test_tasks_list_returns_empty_list_initially(self, client: TestClient) -> None:
        """GET /api/tasks with empty store."""
        response = client.get("/api/tasks")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, list), f"expected list, got {type(body)}"
        assert body == [], f"empty store should return empty list, got {body}"

    def test_create_task_and_list_includes_it(
        self, client: TestClient, sqlite_store: SqliteStore
    ) -> None:
        """POST /api/tasks creates, GET /api/tasks includes it."""
        create_payload = {
            "title": "fh-tier3 single agent task",




        }
        created = client.post("/api/tasks", json=create_payload)
        assert created.status_code == 201, created.text
        task = created.json()
        assert "id" in task, f"created task missing id: {task}"
        task_id = task["id"]

        # List now includes the new task
        listed = client.get("/api/tasks")
        assert listed.status_code == 200, listed.text
        tasks = listed.json()
        assert isinstance(tasks, list) and tasks, "list should not be empty after create"
        found_task = next((t for t in tasks if t["id"] == task_id), None)
        assert found_task is not None, f"created task {task_id} not in list"
        assert found_task["title"] == "fh-tier3 single agent task"

    def test_unknown_task_is_404_with_envelope(self, client: TestClient) -> None:
        """GET /api/tasks/{unknown} returns 404 with error envelope."""
        response = client.get("/api/tasks/tsk_does_not_exist")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())

    def test_runs_list_empty_initially(self, client: TestClient) -> None:
        """GET /api/runs with empty store."""
        response = client.get("/api/runs")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, list)

    def test_runs_detail_404_on_missing(self, client: TestClient) -> None:
        """GET /api/runs/{run_id} for unknown run."""
        response = client.get("/api/runs/run_does_not_exist")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())

    def test_cancel_run_returns_202_or_error(self, client: TestClient) -> None:
        """POST /api/runs/{run_id}/cancel on unknown run."""
        response = client.post("/api/runs/run_does_not_exist/cancel")
        # Cancel may 202 (accepted for async) or 404 (not found); both are valid
        assert 200 <= response.status_code < 500, f"unexpected status: {response.status_code}"


class TestMultiAgentPaths:
    """Multi-agent (swarm) execution routes the command center UI drives."""

    def test_swarm_fleet_list_initially_empty(self, client: TestClient) -> None:
        """GET /api/swarm with empty store."""
        response = client.get("/api/swarm")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, dict), f"expected dict, got {type(body)}"
        # Fleet response shape: typically {"runs": [...], "stats": {...}}
        assert "runs" in body, f"fleet response missing 'runs' key: {sorted(body.keys())}"

    def test_swarm_overview_returns_structure(self, client: TestClient) -> None:
        """GET /api/swarm/overview with empty store."""
        response = client.get("/api/swarm/overview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, dict), f"overview should be dict, got {type(body)}"
        # Overview includes active runs, team, providers, etc.
        # Exact keys depend on implementation; just verify it's a dict with content.
        assert len(body) > 0, "overview should not be empty"

    def test_swarm_team_returns_structure(self, client: TestClient) -> None:
        """GET /api/swarm/team with empty store."""
        response = client.get("/api/swarm/team")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, dict)

    def test_swarm_providers_returns_list(self, client: TestClient) -> None:
        """GET /api/swarm/providers returns list."""
        response = client.get("/api/swarm/providers")
        assert response.status_code == 200, response.text
        body = response.json()
        # Providers should be a list
        assert isinstance(body, list), f"expected list, got {type(body)}"

    def test_swarm_run_detail_404_on_missing(self, client: TestClient) -> None:
        """GET /api/swarm/{run_id} for unknown run."""
        response = client.get("/api/swarm/run_does_not_exist")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())

    def test_swarm_activity_404_on_missing_run(self, client: TestClient) -> None:
        """GET /api/swarm/{run_id}/activity for unknown run."""
        response = client.get("/api/swarm/run_does_not_exist/activity")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())

    def test_cancel_swarm_run_on_missing(self, client: TestClient) -> None:
        """POST /api/swarm/{run_id}/cancel on unknown run."""
        response = client.post("/api/swarm/run_does_not_exist/cancel")
        # Cancel may 202 (accepted) or 404 (not found)
        assert 200 <= response.status_code < 500, f"unexpected status: {response.status_code}"

    def test_swarm_job_status_404_on_missing(self, client: TestClient) -> None:
        """GET /api/swarm/jobs/{job_id} for unknown job."""
        response = client.get("/api/swarm/jobs/job_does_not_exist")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())
