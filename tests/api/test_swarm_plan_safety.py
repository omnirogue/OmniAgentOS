"""HTTP surface for P1-SAFETY: typed multi-bundle 409 and zero-side-effect refusals.

Mutations:
- ``multi-bundle-keeps-first`` — restore first-bundle-only persistence on the API
- ``refusal-creates-run`` — provision before the safety decision
- ``dot-scope-plan-runnable`` — permit root-wide ownership through the route
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import board_files
from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.swarm.dal import SwarmDal


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _task_dict(task_id: str, paths: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": [],
        "owned_paths": paths if paths is not None else [f"src/{task_id}"],
        "est_agent_minutes": 10,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": "git diff --check",
    }


def _safe_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    del prompt, schema, effort
    return {
        "goal": "planned goal",
        "assumptions": [],
        "tasks": [_task_dict(tid) for tid in ("a", "b", "c")],
        "suite_command": "pytest -q",
    }


def _multi_bundle_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    del prompt, schema, effort
    return {
        "goal": "two independent asks",
        "assumptions": [],
        "bundles": [
            {
                "goal": "fix the API",
                "tasks": [
                    _task_dict("a1", ["api/a"]),
                    _task_dict("a2", ["api/b"]),
                    _task_dict("a3", ["api/c"]),
                ],
                "suite_command": "pytest api",
            },
            {
                "goal": "refresh the docs",
                "tasks": [_task_dict("d1", ["docs/x"])],
                "suite_command": "pytest docs",
            },
        ],
    }


def _dot_scope_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    del prompt, schema, effort
    return {
        "goal": "own the workspace root",
        "assumptions": [],
        "tasks": [_task_dict("root", ["."])],
    }


def _none_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> None:
    del prompt, schema, effort
    return None


def _fake_clarify_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    del prompt, schema
    return {
        "mode": "spec",
        "spec": {
            "title": "Refined goal",
            "description": "Ship it end to end.",
            "acceptance_criteria": [],
        },
    }


def _fake_recall_fn(goal: str) -> str:
    del goal
    return ""


def _build_harness(
    db_path: str,
    workdir: Path,
    *,
    planner_llm: Any,
) -> SimpleNamespace:
    collab = CollabStore(db_path)
    store = collab._store
    dal = SwarmDal(db_path)
    workdir.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[swarm_routes.get_swarm_dal] = lambda: dal
    app.dependency_overrides[swarm_routes.get_swarm_planner_llm] = lambda: planner_llm
    app.dependency_overrides[swarm_routes.get_swarm_clarify_llm] = lambda: _fake_clarify_llm
    app.dependency_overrides[swarm_routes.get_swarm_recall_fn] = lambda: _fake_recall_fn
    return SimpleNamespace(client=client, store=store, collab=collab, dal=dal, workdir=workdir)


@pytest.fixture
def swarm_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(tmp_path.resolve())])
    harnesses: list[SimpleNamespace] = []

    def _make(planner_llm: Any) -> SimpleNamespace:
        h = _build_harness(
            str(tmp_path / f"s{len(harnesses)}.db"),
            tmp_path / f"ws{len(harnesses)}",
            planner_llm=planner_llm,
        )
        harnesses.append(h)
        return h

    try:
        yield _make
    finally:
        for h in harnesses:
            _run(h.client.aclose())
            h.dal.close()
        app.dependency_overrides.clear()


def test_safe_plan_provisions_sync(swarm_factory, auth_headers: dict[str, str]) -> None:
    swarm = swarm_factory(_safe_planner_llm)
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "ship the widget", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body.get("swarm_run_id")
    assert swarm.dal.list_runs()


def test_multi_bundle_returns_typed_409_and_persists_none(
    swarm_factory, auth_headers: dict[str, str]
) -> None:
    swarm = swarm_factory(_multi_bundle_planner_llm)
    before = (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks()))
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "two unrelated asks", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code == 409, response.text
    err = response.json()["error"]
    assert err["code"] == "multiple_bundles"
    assert err["detail"]["disposition"] == "needs_clarification"
    assert any(i.get("code") == "multiple_bundles" for i in err["detail"]["issues"])
    assert (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks())) == before
    assert not (swarm.workdir / "PLAN.md").exists()


def test_dot_scope_plan_returns_non_ready_and_persists_none(
    swarm_factory, auth_headers: dict[str, str]
) -> None:
    swarm = swarm_factory(_dot_scope_planner_llm)
    before = (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks()))
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "own the root", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code in {403, 422}, response.text
    err = response.json()["error"]
    assert err["detail"]["disposition"] in {"invalid_plan", "policy_denied", "impossible"}
    assert (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks())) == before
    assert not (swarm.workdir / "PLAN.md").exists()


def test_unavailable_planner_returns_non_ready_and_persists_none(
    swarm_factory, auth_headers: dict[str, str]
) -> None:
    swarm = swarm_factory(_none_planner_llm)
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "make a widget", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code in {422, 503}, response.text
    err = response.json()["error"]
    assert err["detail"]["disposition"] in {"planner_unavailable", "invalid_plan"}
    assert swarm.dal.list_runs() == []


def test_multi_bundle_background_job_stores_typed_error_without_rows(
    swarm_factory, auth_headers: dict[str, str]
) -> None:
    swarm = swarm_factory(_multi_bundle_planner_llm)
    before = (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks()))

    created = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={"brief": "two unrelated asks", "working_dir": str(swarm.workdir)},
        )
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    status = _run(swarm.client.get(f"/api/swarm/jobs/{job_id}", headers=auth_headers))

    assert status.status_code == 200
    assert status.json()["status"] == "error"
    assert status.json()["code"] == "multiple_bundles"
    assert status.json()["detail"]["disposition"] == "needs_clarification"
    assert (len(swarm.dal.list_runs()), len(swarm.collab.list_board_tasks())) == before
