"""Tests for the WP6a swarm API — plan/read/cancel slice.

Mirrors ``tests/api/test_board_files.py``'s harness style (a REAL sqlite-backed
``CollabStore``/``SwarmDal`` rather than a duck-typed fake, since ``SwarmDal``
is a concrete SQL-backed class): ``CollabStore(db)`` migrates the shared
schema (incl. 045's swarm tables) and ``SwarmDal`` opens its own connection to
the same file. Every route in ``omniagentos.api.routes.swarm`` carries its OWN
``Depends(_authorized)`` (hoisted to the router constructor, board_files.py's
F-016 idiom) rather than the app-level ``require_session_token`` gate, so —
like ``test_board_files.py`` — the session-token requirement here is NOT
bypassed by the suite-wide ``tests/conftest.py`` autouse fixture (that fixture
only overrides ``require_session_token`` itself); every authorized request
below carries ``auth_headers`` from ``tests/api/conftest.py``.

``POST /api/swarm`` calls the real WP4 planner (``plan_swarm``/
``provision_run``), which by default runs a real Fable/Claude-CLI call —
exactly the network dependency ``tests/swarm/test_planner.py``'s own suite
always avoids by injecting a fake. Every test harness below overrides
``get_swarm_planner_llm``/``get_swarm_clarify_llm``/``get_swarm_recall_fn``
with fast, deterministic fakes for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import board_files
from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- fake WP4 LLM seams (mirrors tests/swarm/test_planner.py's own fakes) ----


def _fake_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    del prompt, schema, effort
    return {
        "goal": "planned goal",
        "assumptions": [],
        "tasks": [
            {
                "id": task_id,
                "title": task_id.upper(),
                "description": f"do {task_id}",
                "depends_on": [],
                "owned_paths": [f"src/{task_id}"],
                "est_agent_minutes": 10,
                "est_manual_minutes": 30,
                "acceptance": f"{task_id} done",
                "verify_command": "git diff --check",
            }
            for task_id in ("a", "b", "c")
        ],
        "suite_command": "pytest -q",
    }


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


def _build_harness(db_path: str, workdir: Path) -> SimpleNamespace:
    # Sole creator of this path (fresh tmp_path per test): a template copy
    # instead of a full 86-migration apply.
    db_path = migrated_db(CollabStore, db_path)
    collab = CollabStore(db_path)
    store = collab._store
    dal = SwarmDal(db_path)
    # The REAL sessions DAL, on this test's own DB: cancel's session fanout is
    # only worth testing end to end (route -> swarm_attempts -> sessions row).
    # Overriding the dependency also keeps the default accessor — which opens
    # ``default_db_path()`` — away from the developer's live database.
    sessions_dal = SessionsDal(db_path)
    workdir.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[swarm_routes.get_swarm_dal] = lambda: dal
    app.dependency_overrides[swarm_routes.get_swarm_session_killer] = lambda: sessions_dal
    app.dependency_overrides[swarm_routes.get_swarm_planner_llm] = lambda: _fake_planner_llm
    app.dependency_overrides[swarm_routes.get_swarm_clarify_llm] = lambda: _fake_clarify_llm
    app.dependency_overrides[swarm_routes.get_swarm_recall_fn] = lambda: _fake_recall_fn
    return SimpleNamespace(
        client=client,
        store=store,
        collab=collab,
        dal=dal,
        sessions=sessions_dal,
        workdir=workdir,
    )


@pytest.fixture
def swarm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """The F-015 workspace floor is widened to ``tmp_path`` for convenience —
    every test EXCEPT the dedicated floor-403 test below uses this fixture.
    """
    harness = _build_harness(str(tmp_path / "swarm.db"), tmp_path / "workspace")
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(tmp_path.resolve())])
    try:
        yield harness
    finally:
        _run(harness.client.aclose())
        app.dependency_overrides.clear()
        harness.dal.close()
        harness.sessions.close()


@pytest.fixture
def real_floor_swarm(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Like ``swarm``, but does NOT widen the F-015 floor — for the 403 test."""
    harness = _build_harness(str(tmp_path / "floor-swarm.db"), tmp_path / "workspace")
    try:
        yield harness
    finally:
        _run(harness.client.aclose())
        app.dependency_overrides.clear()
        harness.dal.close()
        harness.sessions.close()


# --- envelope + token gate ---------------------------------------------------


def test_missing_session_token_401(swarm: SimpleNamespace) -> None:
    response = _run(swarm.client.get("/api/swarm"))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "message" in body["error"]
    assert "detail" in body["error"]


def test_get_fleet_empty_with_token(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(swarm.client.get("/api/swarm", headers=auth_headers))
    assert response.status_code == 200
    assert response.json() == {
        "runs": [],
        "utilization": {
            "active_sessions": 0,
            "active_swarms": 0,
            "queued_runs": 0,
            "max_concurrent_swarms": swarm_routes.MAX_CONCURRENT_SWARMS,
        },
    }


def test_get_fleet_includes_terminal_run_statuses(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    statuses = [
        "queued",
        "planning",
        "running",
        "merging",
        "completed",
        "failed",
        "cancelled",
    ]
    created = {
        status: swarm.dal.create_run(
            working_dir=str(swarm.workdir), source="test",
            goal=f"{status} run",
            status=status,
        )["id"]
        for status in statuses
    }

    response = _run(swarm.client.get("/api/swarm", headers=auth_headers))

    assert response.status_code == 200
    rows = response.json()["runs"]
    assert {row["id"]: row["status"] for row in rows} == {
        created[status]: status for status in statuses
    }


def test_error_envelope_shape_on_404(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(swarm.client.get("/api/swarm/swr_missing", headers=auth_headers))
    assert response.status_code == 404
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "detail"}
    assert body["error"]["code"] == "not_found"
    assert body["error"]["detail"] == {"id": "swr_missing"}


# --- 404s ---------------------------------------------------------------


def test_get_run_404(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(swarm.client.get("/api/swarm/swr_missing", headers=auth_headers))
    assert response.status_code == 404


def test_get_activity_404(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(swarm.client.get("/api/swarm/swr_missing/activity", headers=auth_headers))
    assert response.status_code == 404


def test_cancel_404(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(swarm.client.post("/api/swarm/swr_missing/cancel", headers=auth_headers))
    assert response.status_code == 404


# --- working_dir workspace floor ---------------------------------------------


@pytest.mark.parametrize("sim_isolated", [False, True], ids=["non-sim", "sim"])
def test_create_workspace_outside_approved_roots_403(
    real_floor_swarm: SimpleNamespace,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sim_isolated: bool,
) -> None:
    """POST /api/swarm keeps the workspace floor active in both modes."""
    approved_parent = tmp_path / "approved"
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(approved_parent / "var"))
    monkeypatch.setattr(
        board_files,
        "grantable_mount_roots",
        lambda: [str((approved_parent / "mount").resolve())],
    )
    if sim_isolated:
        sim_root = approved_parent / "sim-root"
        campaign_root = sim_root / "selfopt-001"
        var_dir = campaign_root / "var"
        var_dir.mkdir(parents=True)
        monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "selfopt-001")
        monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
        monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    else:
        monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_ROOT", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", raising=False)

    response = _run(
        real_floor_swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={
                "brief": "build a thing",
                "working_dir": str(real_floor_swarm.workdir),
            },
        )
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert response.json()["error"]["message"] == "workspace outside approved roots"


def test_create_working_dir_missing_400(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={"brief": "build a thing", "working_dir": str(swarm.workdir / "nope")},
        )
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation"


def test_create_working_dir_not_a_directory_400(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    a_file = swarm.workdir.parent / "a_file.txt"
    a_file.write_text("not a dir", encoding="utf-8")
    response = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={"brief": "build a thing", "working_dir": str(a_file)},
        )
    )
    assert response.status_code == 400


def test_create_missing_brief_400(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={"working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation"


# --- budget validation --------------------------------------------------


def test_create_budget_over_ceiling_400(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={
                "brief": "too much money",
                "working_dir": str(swarm.workdir),
                "budget_usd_max": swarm_routes.MAX_BUDGET_USD_MAX + 1,
            },
        )
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation"


def test_create_budget_non_positive_400(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm",
            headers=auth_headers,
            json={"brief": "negative", "working_dir": str(swarm.workdir), "budget_usd_max": 0},
        )
    )
    assert response.status_code == 400


# --- planner resilience seam ------------------------------------------------


def test_planner_unavailable_returns_503(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    with patch.object(swarm_routes, "_wp4_planner_functions", return_value=None):
        response = _run(
            swarm.client.post(
                "/api/swarm?sync=1",
                headers=auth_headers,
                json={"brief": "a goal", "working_dir": str(swarm.workdir)},
            )
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


# --- creation: plans + provisions a real DAG via WP4 -------------------------


def test_create_provisions_real_plan(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "Build the thing", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code == 202
    body = response.json()
    assert body["swarm_run_id"].startswith("swr_")
    assert body["board_task_id"].startswith("btk_")

    run = swarm.dal.get_run(body["swarm_run_id"])
    assert run is not None
    assert run["status"] == "planning"
    assert run["board_task_id"] == body["board_task_id"]
    assert run["budget_usd_max"] == swarm_routes.DEFAULT_BUDGET_USD_MAX

    root_card = swarm.collab.get_board_task(body["board_task_id"])
    assert root_card is not None
    assert root_card["title"].startswith("Swarm:")

    # 3 fake tasks (a, b, c; ratio 3.0 >= 1.5, tasks > SOLO_MAX_TASKS) means
    # `build_plan` auto-appends an integration task -> 4 member cards.
    detail = _run(swarm.client.get(f"/api/swarm/{body['swarm_run_id']}", headers=auth_headers))
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["run"]["id"] == body["swarm_run_id"]
    assert len(detail_body["tasks"]) == 4
    assert all(task["id"] != body["board_task_id"] for task in detail_body["tasks"])
    assert detail_body["progress"]["total"] == 4
    assert detail_body["progress"]["open"] == 4
    assert len(detail_body["deps"]) > 0  # integration task depends on a/b/c

    # plan_created is emitted with a real task_count/parallelism_ratio.
    activity = _run(
        swarm.client.get(f"/api/swarm/{body['swarm_run_id']}/activity", headers=auth_headers)
    )
    assert activity.status_code == 200
    events = activity.json()
    assert len(events) == 1
    assert events[0]["action"] == "plan_created"
    assert events[0]["payload"]["task_count"] == 4
    assert events[0]["payload"]["parallelism_ratio"] == pytest.approx(3.0)

    fleet = _run(swarm.client.get("/api/swarm", headers=auth_headers)).json()
    run_ids = [row["id"] for row in fleet["runs"]]
    assert body["swarm_run_id"] in run_ids
    fleet_row = next(row for row in fleet["runs"] if row["id"] == body["swarm_run_id"])
    assert fleet_row["task_counts"]["open"] == 4
    assert fleet_row["progress"] == {"done": 0, "total": 4}
    assert fleet["utilization"]["active_swarms"] == 1
    assert fleet["utilization"]["queued_runs"] == 0


def test_create_uses_spec_alias_when_brief_absent(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    response = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"spec": "Refined spec text", "working_dir": str(swarm.workdir)},
        )
    )
    assert response.status_code == 202
    body = response.json()
    run = swarm.dal.get_run(body["swarm_run_id"])
    assert run is not None
    assert run["status"] == "planning"


# --- cancel --------------------------------------------------------------


def test_cancel_non_terminal_run(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    run = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="g")
    swarm.dal.set_run_status(run["id"], "running")

    response = _run(swarm.client.post(f"/api/swarm/{run['id']}/cancel", headers=auth_headers))
    assert response.status_code == 200
    assert response.json() == {
        "id": run["id"],
        "status": "cancelled",
        "kill_complete": True,
        "sessions": {
            "cancelled": [],
            "already_terminal": [],
            "kill_pending": [],
            "failed": [],
            "not_owned": [],
            "unbound_attempts": [],
        },
    }
    assert swarm.dal.get_run(run["id"])["status"] == "cancelled"

    activity = _run(
        swarm.client.get(f"/api/swarm/{run['id']}/activity", headers=auth_headers)
    ).json()
    assert len(activity) == 1
    assert activity[0]["action"] == "run_failed"
    # A run with nothing running keeps the historical payload byte for byte.
    assert activity[0]["payload"] == {"reason": "cancelled"}


def test_cancel_is_idempotent_on_terminal_run(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    run = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="g")
    swarm.dal.set_run_status(run["id"], "completed")

    response = _run(swarm.client.post(f"/api/swarm/{run['id']}/cancel", headers=auth_headers))
    assert response.status_code == 200
    assert response.json() == {
        "id": run["id"],
        "status": "completed",
        "kill_complete": True,
        "sessions": {
            "cancelled": [],
            "already_terminal": [],
            "kill_pending": [],
            "failed": [],
            "not_owned": [],
            "unbound_attempts": [],
        },
    }

    # No cancel event emitted for an already-terminal run.
    activity = _run(
        swarm.client.get(f"/api/swarm/{run['id']}/activity", headers=auth_headers)
    ).json()
    assert activity == []


# --- cancel: the live-session fanout -------------------------------------
#
# The bug these pin: cancel used to flip swarm_runs.status and report success
# while every session the run had spawned kept running. Only the scheduler
# killed anything, and only if a coordinator was alive to notice the flip — a
# run whose coordinator had died was cancelled on paper and spending in fact.


def _seed_live_session(
    harness: SimpleNamespace,
    run_id: str,
    session_id: str,
    *,
    attempt_id: str | None = None,
    task_id: str | None = None,
    state: str = "running",
    bind_session: bool = True,
    mark_owner: bool | str = True,
) -> str:
    """One live ``swarm_attempts`` row for ``run_id``, optionally bound to a session.

    Seeded with direct SQL (``tests/api/test_sessions.py``'s own idiom) rather
    than ``open_attempt``, which insists on a real member ``board_tasks`` row —
    irrelevant to what cancel reads, which is the attempt's ``swarm_run_id``.

    ``mark_owner`` mirrors what every real spawn path does: record the
    session's durable owner in ``orchestrator_sessions`` (True = this run;
    a string = that run id — the mis-bound case; False = never marked).
    """
    if mark_owner and bind_session:
        harness.sessions.mark_orchestrator_session(
            session_id, run_id if mark_owner is True else mark_owner
        )
    if state is not None:
        harness.sessions.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "project_dir": str(harness.workdir),
                "provider": "claude",
                "state": state,
                "model": "fable",
                "prompt": "do the work",
            }
        )
    attempt_id = attempt_id or f"swa_{session_id[4:]}"
    harness.dal._connection.execute(  # noqa: SLF001 - direct seed row.
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, session_id, "
        "provider, model, tier, account_id, started_at, ended_at, end_reason, detail, source) "
        "VALUES (?, ?, ?, 0, ?, 'claude', 'fable', 'standard', NULL, ?, NULL, NULL, '', 'test')",
        (
            attempt_id,
            run_id,
            task_id or f"tsk_{attempt_id}",
            session_id if bind_session else None,
            utc_now_iso(),
        ),
    )
    harness.dal._connection.commit()  # noqa: SLF001
    return attempt_id


def _running_run(harness: SimpleNamespace) -> str:
    run = harness.dal.create_run(working_dir=str(harness.workdir), source="test", goal="g")
    harness.dal.set_run_status(run["id"], "running")
    return str(run["id"])


class _FakeKiller:
    """A sessions DAL whose behaviour per session id the test dictates.

    ``rows`` is the read-back the route verifies against; ``raise_on`` names
    session ids whose ``request_cancel`` blows up. Nothing here mutates
    ``rows`` on a successful call unless the test says so — that is precisely
    how the "returned True, recorded nothing" case is built. ``owners`` is the
    durable ownership binding (session id -> run id) the F002 gate reads; a
    session absent from it is refused as unowned, exactly like a session with
    no ``orchestrator_sessions`` row. ``terminalize`` names sessions whose
    supervisor "kills" them synchronously: ``request_cancel`` flips their
    state terminal, which is the ONE read-back that may count as cancelled.
    """

    def __init__(
        self,
        rows: dict[str, dict[str, Any] | None],
        *,
        owners: dict[str, str] | None = None,
        raise_on: set[str] | None = None,
        record: set[str] | None = None,
        terminalize: set[str] | None = None,
    ) -> None:
        self.rows = rows
        self.owners = owners or {}
        self.raise_on = raise_on or set()
        self.record = record if record is not None else set(rows)
        self.terminalize = terminalize or set()
        self.cancel_calls: list[str] = []

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.rows.get(session_id)

    def orchestrator_session_run(self, session_id: str) -> str | None:
        return self.owners.get(session_id)

    def request_cancel(self, session_id: str) -> bool:
        self.cancel_calls.append(session_id)
        if session_id in self.raise_on:
            raise RuntimeError("supervisor unreachable")
        if session_id in self.record and self.rows.get(session_id) is not None:
            self.rows[session_id].update({"kill_requested": 1, "killed_by": "cancel_requested"})
        if session_id in self.terminalize and self.rows.get(session_id) is not None:
            self.rows[session_id].update({"state": "cancelled"})
        return True


def test_default_session_killer_is_the_shared_sessions_dal() -> None:
    """Every other test here overrides the killer, so the real wiring needs its
    own pin: the default must be the process-lifetime ``SessionsDal`` that
    ``routes.sessions`` owns — not a second connection, and not something that
    only satisfies the protocol at type-check time."""
    from omniagentos.api.routes.sessions import get_sessions_dal

    killer = swarm_routes.get_swarm_session_killer()

    assert killer is get_sessions_dal()
    assert callable(killer.request_cancel)
    assert callable(killer.get_session)
    # The F002 ownership gate refuses every kill when this binding is missing,
    # so the real DAL exposing it is part of the wiring contract.
    assert callable(killer.orchestrator_session_run)


def test_cancel_signals_every_live_session_of_the_run(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """The acceptance criterion: cancel reaches the real session rows.

    F001: reaching them is NOT killing them. ``request_cancel`` records a
    durable kill request; the session state stays non-terminal until the
    supervisor stops the process. Both sessions are therefore
    ``kill_pending`` — never ``cancelled`` — and ``kill_complete`` is false,
    because nothing here confirmed a process death.
    """
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_alpha")
    _seed_live_session(swarm, run_id, "ses_beta")

    response = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["kill_complete"] is False
    assert body["sessions"]["cancelled"] == []
    assert sorted(body["sessions"]["kill_pending"]) == ["ses_alpha", "ses_beta"]
    assert "ses_alpha" in body["warning"] and "ses_beta" in body["warning"]

    # The durable half: both sessions carry an OPERATOR cancel, which is what
    # makes the supervisor finish them CANCELLED and stops the longhaul engine
    # from respawning a successor (its killed_by discrimination is a blocklist:
    # anything it does not recognise as operator intent gets respawned).
    for session_id in ("ses_alpha", "ses_beta"):
        row = swarm.sessions.get_session(session_id)
        assert row["kill_requested"] == 1, session_id
        assert row["killed_by"] == "cancel_requested", session_id


def test_cancel_never_touches_another_runs_sessions(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """Scope confusion: two live runs, one Stop press, one victim."""
    doomed = _running_run(swarm)
    bystander = _running_run(swarm)
    _seed_live_session(swarm, doomed, "ses_doomed")
    _seed_live_session(swarm, bystander, "ses_bystander")

    response = _run(swarm.client.post(f"/api/swarm/{doomed}/cancel", headers=auth_headers))

    assert response.status_code == 200
    body = response.json()
    assert body["sessions"]["kill_pending"] == ["ses_doomed"]
    assert "ses_bystander" not in json.dumps(body)

    bystander_row = swarm.sessions.get_session("ses_bystander")
    assert bystander_row["kill_requested"] == 0
    assert bystander_row["killed_by"] is None
    assert swarm.dal.get_run(bystander)["status"] == "running"


def test_cancel_names_the_sessions_it_could_not_stop(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """Partial failure is loud: one bad session must not read as a clean stop."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_ok")
    _seed_live_session(swarm, run_id, "ses_stuck")
    killer = _FakeKiller(
        {
            "ses_ok": {"state": "running", "kill_requested": 0, "killed_by": None},
            "ses_stuck": {"state": "running", "kill_requested": 0, "killed_by": None},
        },
        owners={"ses_ok": run_id, "ses_stuck": run_id},
        raise_on={"ses_stuck"},
        terminalize={"ses_ok"},
    )
    app.dependency_overrides[swarm_routes.get_swarm_session_killer] = lambda: killer

    response = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers))

    assert response.status_code == 200
    body = response.json()
    # The run really is cancelled — the failure is about the sessions, and
    # saying so is not the same as pretending the cancel did not happen.
    assert body["status"] == "cancelled"
    assert body["kill_complete"] is False
    assert body["sessions"]["cancelled"] == ["ses_ok"]
    assert [item["session_id"] for item in body["sessions"]["failed"]] == ["ses_stuck"]
    assert "RuntimeError" in body["sessions"]["failed"][0]["reason"]
    assert "ses_stuck" in body["warning"]
    # A survivor is news; the activity feed carries it too.
    activity = _run(swarm.client.get(f"/api/swarm/{run_id}/activity", headers=auth_headers)).json()
    assert activity[0]["payload"]["sessions_unstopped"] == ["ses_stuck"]


def test_cancel_reports_failure_when_the_cancel_was_not_durably_recorded(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """The verification itself.

    ``request_cancel`` returns True and the session row is untouched — a DAL
    that lies, a write that lost a race, a CAS that matched nothing. Trusting
    the return value reports this as a kill; reading the row back does not.
    Delete ``_verify_cancelled``'s read-back and this test is what fails.
    """
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_liar")
    killer = _FakeKiller(
        {"ses_liar": {"state": "running", "kill_requested": 0, "killed_by": None}},
        owners={"ses_liar": run_id},
        record=set(),
    )
    app.dependency_overrides[swarm_routes.get_swarm_session_killer] = lambda: killer

    response = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers))

    body = response.json()
    assert killer.cancel_calls == ["ses_liar"]
    assert body["sessions"]["cancelled"] == []
    assert [item["session_id"] for item in body["sessions"]["failed"]] == ["ses_liar"]
    assert body["kill_complete"] is False
    assert "ses_liar" in body["warning"]


def test_cancel_rejects_a_kill_recorded_under_someone_elses_attribution(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """``kill_requested`` alone is not proof OUR cancel landed.

    The idle reaper sets that flag too, under its own ``killed_by`` — and the
    longhaul engine respawns those. Verification therefore checks the
    attribution, not just the flag.
    """
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_reaped")
    killer = _FakeKiller(
        {"ses_reaped": {"state": "running", "kill_requested": 1, "killed_by": "idle-reaper"}},
        owners={"ses_reaped": run_id},
        record=set(),
    )
    app.dependency_overrides[swarm_routes.get_swarm_session_killer] = lambda: killer

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["sessions"]["cancelled"] == []
    assert [item["session_id"] for item in body["sessions"]["failed"]] == ["ses_reaped"]
    assert "idle-reaper" in body["sessions"]["failed"][0]["reason"]


def test_cancel_reports_a_missing_session_row_as_unverifiable(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """An attempt naming a session that has no row: a process we cannot see is
    not a process we know is dead."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_ghost", state=None)

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is False
    assert [item["session_id"] for item in body["sessions"]["failed"]] == ["ses_ghost"]
    assert "unverifiable" in body["sessions"]["failed"][0]["reason"]


def test_cancel_counts_already_finished_sessions_as_gone_not_killed(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_done", state="completed")

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is True
    assert body["sessions"]["already_terminal"] == ["ses_done"]
    assert body["sessions"]["cancelled"] == []
    # A terminal session is untouched: request_cancel's CAS refuses it and the
    # historical attribution stays whatever finished it.
    assert swarm.sessions.get_session("ses_done")["kill_requested"] == 0


def test_cancel_ignores_attempts_that_already_ended(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """Only OPEN attempts are live work; a settled attempt's session is history."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_settled")
    swarm.dal._connection.execute(  # noqa: SLF001
        "UPDATE swarm_attempts SET ended_at = ?, end_reason = 'completed' WHERE session_id = ?",
        (utc_now_iso(), "ses_settled"),
    )
    swarm.dal._connection.commit()  # noqa: SLF001

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is True
    assert body["sessions"] == {
        "cancelled": [],
        "already_terminal": [],
        "kill_pending": [],
        "failed": [],
        "not_owned": [],
        "unbound_attempts": [],
    }
    assert swarm.sessions.get_session("ses_settled")["kill_requested"] == 0


def test_cancel_flags_an_attempt_caught_mid_spawn(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """A live attempt with no session id yet is the one thing cancel cannot
    signal. Reporting complete here would be the exact lie being fixed."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_spawning", attempt_id="swa_spawning", bind_session=False)

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is False
    assert body["sessions"]["unbound_attempts"] == ["swa_spawning"]
    assert "swa_spawning" in body["warning"]


def test_cancel_stops_sessions_left_behind_by_a_terminal_run(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """The coordinator-absent leak, and why a second Stop is not a no-op.

    A run stamped terminal by some other path (a crashed coordinator adopted
    and failed, an API failure stamp) can still own live sessions that nothing
    will ever poll for again. Cancel looks anyway.
    """
    run = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="g")
    swarm.dal.set_run_status(run["id"], "failed")
    _seed_live_session(swarm, run["id"], "ses_orphan")

    body = _run(swarm.client.post(f"/api/swarm/{run['id']}/cancel", headers=auth_headers)).json()

    assert body["status"] == "failed"  # the run's own status is not rewritten
    assert body["sessions"]["kill_pending"] == ["ses_orphan"]
    assert swarm.sessions.get_session("ses_orphan")["kill_requested"] == 1
    # Still no run_failed event for an already-terminal run.
    activity = _run(
        swarm.client.get(f"/api/swarm/{run['id']}/activity", headers=auth_headers)
    ).json()
    assert activity == []


def test_double_cancel_re_verifies_instead_of_reporting_stale_success(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """Idempotent, but never silent: press Stop twice and the second press
    re-reads the world rather than replaying the first answer."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_stubborn")

    first = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()
    second = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert first["status"] == second["status"] == "cancelled"
    # The session is still non-terminal (the supervisor has not reaped it yet),
    # so the second call re-signals, re-verifies — and keeps refusing to call
    # an unconfirmed death complete.
    assert second["sessions"]["kill_pending"] == ["ses_stubborn"]
    assert second["kill_complete"] is False
    # Exactly one cancel event: the second press did not re-terminalize the run.
    activity = _run(swarm.client.get(f"/api/swarm/{run_id}/activity", headers=auth_headers)).json()
    assert [event["action"] for event in activity] == ["run_failed"]


def test_cancel_event_payload_carries_the_fanout_result(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_live")
    _seed_live_session(swarm, run_id, "ses_gone", state="failed")

    _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers))

    activity = _run(swarm.client.get(f"/api/swarm/{run_id}/activity", headers=auth_headers)).json()
    assert activity[0]["payload"] == {
        "reason": "cancelled",
        "sessions_cancelled": 0,
        "sessions_already_terminal": 1,
        "sessions_kill_pending": ["ses_live"],
    }


def test_cancel_reports_an_unreadable_attempt_scan_as_incomplete(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """A scan that failed is not a scan that found nothing."""
    run_id = _running_run(swarm)

    def _boom(_run_id: str) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("database is locked")

    with patch.object(swarm.dal, "attempts_with_usage", _boom):
        body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["status"] == "cancelled"
    assert body["kill_complete"] is False
    assert "OperationalError" in body["sessions"]["scan_error"]
    assert "could not enumerate" in body["warning"]


# --- cancel: process death, ownership, atomicity, contract (F001-F005) ----


def test_cancel_confirms_death_only_from_a_terminal_session_state(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """F001 both ways in one run: the supervisor terminalizes ``ses_dead``
    synchronously (confirmed death -> ``cancelled``), while ``ses_flagged``
    only gets the durable kill request (-> ``kill_pending``, never
    ``cancelled``) — and one unconfirmed death makes ``kill_complete`` false.
    """
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_dead")
    _seed_live_session(swarm, run_id, "ses_flagged")
    killer = _FakeKiller(
        {
            "ses_dead": {"state": "running", "kill_requested": 0, "killed_by": None},
            "ses_flagged": {"state": "running", "kill_requested": 0, "killed_by": None},
        },
        owners={"ses_dead": run_id, "ses_flagged": run_id},
        terminalize={"ses_dead"},
    )
    app.dependency_overrides[swarm_routes.get_swarm_session_killer] = lambda: killer

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["sessions"]["cancelled"] == ["ses_dead"]
    assert body["sessions"]["kill_pending"] == ["ses_flagged"]
    assert body["kill_complete"] is False
    assert "ses_flagged" in body["warning"]
    assert "ses_dead" not in body["warning"]


def test_cancel_refuses_a_session_durably_owned_by_another_run(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """F002: ``swarm_attempts.session_id`` has no FK — a stale/corrupt row can
    name ANY session. The durable owner (``orchestrator_sessions.run_id``) is
    cross-checked BEFORE any kill request; a mismatch is refused and reported,
    and the bystander session is never signalled."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_bystander", mark_owner="swr_other")

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is False
    assert [item["session_id"] for item in body["sessions"]["not_owned"]] == ["ses_bystander"]
    assert "swr_other" in body["sessions"]["not_owned"][0]["reason"]
    assert "ses_bystander" in body["warning"]
    # The whole point: the bystander was never touched.
    row = swarm.sessions.get_session("ses_bystander")
    assert row["kill_requested"] == 0
    assert row["killed_by"] is None


def test_cancel_refuses_a_session_with_no_recorded_owner(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """F002, fail-closed half: no ``orchestrator_sessions`` binding at all is
    an UNPROVEN owner, which refuses exactly like a mismatch — never kill a
    session you cannot prove is yours."""
    run_id = _running_run(swarm)
    _seed_live_session(swarm, run_id, "ses_unowned", mark_owner=False)

    body = _run(swarm.client.post(f"/api/swarm/{run_id}/cancel", headers=auth_headers)).json()

    assert body["kill_complete"] is False
    assert [item["session_id"] for item in body["sessions"]["not_owned"]] == ["ses_unowned"]
    assert "no durable owner" in body["sessions"]["not_owned"][0]["reason"]
    assert swarm.sessions.get_session("ses_unowned")["kill_requested"] == 0


def test_concurrent_cancels_emit_exactly_one_terminal_receipt(
    swarm: SimpleNamespace,
) -> None:
    """F003: the status flip is a CAS (one UPDATE with the non-terminal
    predicate), so two cancels racing through the read-then-write window
    produce exactly ONE ``run_failed`` receipt. The barrier parks both
    threads right after their initial ``get_run`` read — the exact interleave
    that used to double-emit."""
    run = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="race")
    run_id = str(run["id"])
    swarm.dal.set_run_status(run_id, "running")

    original_get_run = swarm.dal.get_run
    barrier = threading.Barrier(2)
    local = threading.local()

    def racing_get_run(inner_run_id: str) -> dict[str, Any] | None:
        row = original_get_run(inner_run_id)
        if not getattr(local, "did_initial_read", False):
            local.did_initial_read = True
            barrier.wait(timeout=5)
        return row

    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            swarm_routes.cancel_swarm_run(run_id, swarm.dal, swarm.store, _FakeKiller({}))
        except BaseException as exc:  # noqa: BLE001 -- surfaced via the assertion below
            errors.append(exc)

    with patch.object(swarm.dal, "get_run", racing_get_run):
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert not errors, errors

    events = swarm.store.get_events_for_target("swarm_run", run_id)
    cancel_events = [event for event in events if event["action"] == "run_failed"]
    assert len(cancel_events) == 1, cancel_events
    assert swarm.dal.get_run(run_id)["status"] == "cancelled"


def test_cancel_openapi_declares_the_response_contract() -> None:
    """F005: the cancel route's response is a machine-declared model, not
    ``dict[str, Any]`` — generated clients can rely on ``kill_complete``
    being present, and the session buckets (including ``scan_error``) are
    represented in the schema."""
    document = app.openapi()

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            target: Any = document
            for part in node["$ref"].removeprefix("#/").split("/"):
                if part:
                    target = target[part]
            node = target
        return node

    schema = resolve(
        document["paths"]["/api/swarm/{run_id}/cancel"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
    )
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    assert "kill_complete" in properties
    assert "kill_complete" in required
    assert {"id", "status", "sessions"} <= required

    sessions = resolve(properties["sessions"])
    session_properties = sessions.get("properties", {})
    for bucket in (
        "cancelled",
        "already_terminal",
        "kill_pending",
        "failed",
        "not_owned",
        "unbound_attempts",
        "scan_error",
    ):
        assert bucket in session_properties, bucket


# --- activity pagination ------------------------------------------------


def test_activity_pagination_and_scoping(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    run_id = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="g")["id"]
    other_run_id = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="other")["id"]

    for i in range(5):
        swarm.store.insert_event(
            "swarm.event",
            "swarm",
            "task_assigned",
            target_type="swarm_run",
            target_id=run_id,
            payload={"i": i},
        )
    swarm.store.insert_event(
        "swarm.event",
        "swarm",
        "task_assigned",
        target_type="swarm_run",
        target_id=other_run_id,
        payload={"i": "other"},
    )

    first = _run(
        swarm.client.get(
            f"/api/swarm/{run_id}/activity",
            headers=auth_headers,
            params={"limit": 2},
        )
    )
    assert first.status_code == 200
    first_rows = first.json()
    assert [row["payload"]["i"] for row in first_rows] == [0, 1]
    assert all(row["target_id"] == run_id for row in first_rows)

    second = _run(
        swarm.client.get(
            f"/api/swarm/{run_id}/activity",
            headers=auth_headers,
            params={"after": first_rows[-1]["id"]},
        )
    )
    assert second.status_code == 200
    second_rows = second.json()
    assert [row["payload"]["i"] for row in second_rows] == [2, 3, 4]

    # The other run's events never leak into this run's feed.
    all_rows = first_rows + second_rows
    assert all(row["target_id"] == run_id for row in all_rows)


def test_activity_strips_unallowed_fields_from_approval_receipt(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """F001 sibling: /activity must project ``approval.recorded`` payloads.

    Mirrors the engine approval-receipt endpoint's own F001 regression test —
    a hostile ``approval.recorded`` write smuggling nested credential-shaped
    fields under an allow-listed key (and extra non-allow-listed keys) must
    never reach a caller through the swarm activity feed either, since both
    routes read the identical ``swarm_run`` event rows.
    """
    run_id = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="g")["id"]

    hostile_payload = {
        "reviewer": "owner",
        "run_id": run_id,
        "head_sha": "abc123",
        "recorded_at": "2026-08-13T00:00:00Z",
        # Nested dict smuggled under an allow-listed key: must be dropped,
        # not copied, even though "reviewer" is allow-listed.
        "api_key": "sk-hostile-secret",
        "credentials": {"aws_secret_access_key": "hostile"},
        "notes": "should not appear",
    }
    swarm.store.insert_event(
        "swarm.event",
        "swarm",
        "approval.recorded",
        target_type="swarm_run",
        target_id=run_id,
        payload=hostile_payload,
    )

    activity = _run(
        swarm.client.get(f"/api/swarm/{run_id}/activity", headers=auth_headers)
    ).json()
    assert len(activity) == 1
    payload = activity[0]["payload"]
    assert payload == {
        "reviewer": "owner",
        "run_id": run_id,
        "head_sha": "abc123",
        "recorded_at": "2026-08-13T00:00:00Z",
    }
    assert "api_key" not in payload
    assert "credentials" not in payload
    assert "notes" not in payload


# --- overview (C2) -----------------------------------------------------------


def test_overview_requires_token(swarm: SimpleNamespace) -> None:
    response = _run(swarm.client.get("/api/swarm/overview"))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert set(body["error"].keys()) == {"code", "message", "detail"}


def test_overview_empty_fleet_shape(swarm: SimpleNamespace, auth_headers: dict[str, str]) -> None:
    """The literal /overview path is matched BEFORE /{run_id} (a 200 envelope,
    not a 404 for a run named "overview"), and an empty fleet zeroes cleanly."""
    response = _run(swarm.client.get("/api/swarm/overview", headers=auth_headers))
    assert response.status_code == 200
    assert response.json() == {
        "active": 0,
        "progress": {"done": 0, "total": 0, "pct": 0},
        "est_completion_at": None,
        "throughput": {
            "est_manual_minutes": 0,
            "est_swarm_minutes": 0,
            "actual_minutes": 0.0,
            "speedup": 0.0,
            "time_saved_minutes": 0.0,
            "active_terminals": 0,
            "max_terminals": swarm_routes.MAX_SWARM_TERMINALS,
            "utilization_pct": 0.0,
            "idle_pct": 100.0,
            "tasks_per_hour": 0.0,
            "rate_limit_delays_avoided": 0,
            "health": "idle",
        },
        "tasks": [],
        "budget": {"consumed_usd": 0.0, "cap_usd": None},
    }


def test_overview_aggregates_active_runs(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    created = _run(
        swarm.client.post(
            "/api/swarm?sync=1",
            headers=auth_headers,
            json={"brief": "Build the thing", "working_dir": str(swarm.workdir)},
        )
    ).json()
    run_id = created["swarm_run_id"]
    root_card_id = created["board_task_id"]

    # A freshly provisioned run is `planning` (an ACTIVE status) with 4 open
    # member cards. Flip one to done, one to in_progress + a live attempt that
    # carries a session id, and add some cost.
    members = [task for task in swarm.dal.tasks_for_run(run_id) if task["id"] != root_card_id]
    assert len(members) == 4
    done_card, active_card = members[0], members[1]
    swarm.collab.update_board_task(done_card["id"], {"status": "done"})
    swarm.collab.update_board_task(active_card["id"], {"status": "in_progress"})
    swarm.dal.open_attempt(
        run_id,
        active_card["id"],
        provider="codex",
        model="gpt-5.6-sol",
        session_id="ses_overview", source="test")
    swarm.dal.add_cost(run_id, 3.5)

    expected_manual = sum(
        int(json.loads(task["swarm_json"] or "{}").get("est_manual_minutes") or 0)
        for task in swarm.dal.tasks_for_run(run_id)
        if task["id"] != root_card_id
    )

    body = _run(swarm.client.get("/api/swarm/overview", headers=auth_headers)).json()

    assert body["active"] == 1
    assert body["progress"] == {"done": 1, "total": 4, "pct": 25}
    assert body["throughput"]["est_manual_minutes"] == expected_manual
    assert expected_manual > 0
    assert body["throughput"]["active_terminals"] == 1
    assert body["throughput"]["max_terminals"] == swarm_routes.MAX_SWARM_TERMINALS
    # started_at is NULL on a planning run, so wall-clock derived fields are 0.
    assert body["throughput"]["actual_minutes"] == 0.0
    assert body["throughput"]["speedup"] == 0.0
    assert body["throughput"]["tasks_per_hour"] == 0.0
    # actual_minutes is 0.0 here (planning run, no wall-clock) and nothing is
    # blocked, so "healthy" is correct under the overview predicate: it
    # degrades on (actual_minutes > 0 and speedup < 1.0) -- the case that was
    # live-lying at speedup 0.06 with time_saved_minutes -3446 -- or on any
    # blocked task, and reports "idle" with no active runs. A 0-minute run is
    # "no data yet", which is neither slow nor healthy-washed.
    assert body["throughput"]["health"] == "healthy"
    assert body["budget"] == {"consumed_usd": 3.5, "cap_usd": swarm_routes.DEFAULT_BUDGET_USD_MAX}

    # Only the in_progress card is an in-flight task; it carries its session and
    # a provider/model assignment reason for the phase overlay.
    assert len(body["tasks"]) == 1
    task = body["tasks"][0]
    assert task["task_id"] == active_card["id"]
    assert task["session_id"] == "ses_overview"
    assert task["phase"] in {"running", "integration"}
    assert "codex" in task["assignment_reason"]


def test_overview_excludes_terminal_runs(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    run = swarm.dal.create_run(working_dir=str(swarm.workdir), source="test", goal="done run")
    swarm.dal.set_run_status(run["id"], "completed")

    body = _run(swarm.client.get("/api/swarm/overview", headers=auth_headers)).json()
    assert body["active"] == 0
    assert body["progress"] == {"done": 0, "total": 0, "pct": 0}
    assert body["throughput"]["health"] == "idle"


# --- providers (C4) ----------------------------------------------------------

_PROVIDER_ROW_KEYS = {
    "provider",
    "account_id",
    "display_name",
    "status",
    "cooldown_until",
    "reset_in_seconds",
    "active_sessions",
    "max_inflight",
    "status_detail",
}


def _insert_account(
    db_path: str,
    *,
    account_id: str,
    provider: str,
    status: str,
    cooldown_until: str | None = None,
    label: str = "acct",
    config_dir: str | None = None,
    enabled: int = 1,
    is_default: int = 0,
) -> None:
    """Insert a ``claude_accounts`` row directly into the test DB.

    Bypasses ``accounts.service.add_account`` (which requires a real, existing
    config dir and reads its email) so a test can plant an account in any
    provider/status/cooldown state deterministically."""
    from datetime import UTC, datetime

    from omniagentos.db.store import _connect

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO claude_accounts "
            "(id, label, auth_type, config_dir, provider, enabled, is_default, "
            " status, cooldown_until, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                label,
                "config_dir",
                config_dir,
                provider,
                enabled,
                is_default,
                status,
                cooldown_until,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_providers_requires_token(swarm: SimpleNamespace) -> None:
    response = _run(swarm.client.get("/api/swarm/providers"))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert set(body["error"].keys()) == {"code", "message", "detail"}


def test_providers_shape_and_implicit_rows(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """Every known provider is present; the account-less CLI providers each get
    exactly one implicit row. The literal /providers path is matched BEFORE
    /{run_id} (a 200 list, not a 404 for a run named "providers")."""
    response = _run(swarm.client.get("/api/swarm/providers", headers=auth_headers))
    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    assert rows, "at least the implicit CLI-provider rows must be present"
    for row in rows:
        assert set(row.keys()) == _PROVIDER_ROW_KEYS

    by_provider: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_provider.setdefault(str(row["provider"]), []).append(row)

    # The wrapped CLI providers have no accounts row on a fresh DB, so each
    # is a single implicit "unknown" row.
    for provider in ("codex", "grok", "gemini", "kimi", "qwen"):
        assert provider in by_provider
        implicit = by_provider[provider]
        assert len(implicit) == 1
        assert implicit[0]["account_id"] is None
        assert implicit[0]["status"] == "unknown"
        assert implicit[0]["active_sessions"] == 0
        assert implicit[0]["cooldown_until"] is None
        assert implicit[0]["reset_in_seconds"] is None
        assert isinstance(implicit[0]["max_inflight"], int)

    assert "claude" in by_provider


def test_providers_account_rows_and_cooling(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    """A registered account surfaces as a real row; a rate-limited account with a
    future cooldown renders with a positive reset countdown."""
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_account(
        swarm.dal.db_path,
        account_id="acct_ok",
        provider="claude",
        status="ok",
        label="Primary",
    )
    _insert_account(
        swarm.dal.db_path,
        account_id="acct_cooling",
        provider="claude",
        status="rate_limited",
        cooldown_until=future,
        label="Cooling",
    )

    rows = _run(swarm.client.get("/api/swarm/providers", headers=auth_headers)).json()
    by_id = {row["account_id"]: row for row in rows}

    assert "acct_ok" in by_id
    ok_row = by_id["acct_ok"]
    assert ok_row["provider"] == "claude"
    assert ok_row["display_name"] == "Primary"
    assert ok_row["status"] == "ok"
    assert ok_row["cooldown_until"] is None
    assert ok_row["reset_in_seconds"] is None
    assert ok_row["active_sessions"] == 0
    assert isinstance(ok_row["max_inflight"], int)

    assert "acct_cooling" in by_id
    cooling = by_id["acct_cooling"]
    assert cooling["status"] == "rate_limited"
    assert cooling["cooldown_until"] == future
    assert isinstance(cooling["reset_in_seconds"], int)
    assert cooling["reset_in_seconds"] > 0


# --- formation_selections close-orphaned regression tests ---------------------


def test_close_orphaned_formation_selections(
    swarm: SimpleNamespace, auth_headers: dict[str, str]
) -> None:
    from omniagentos.formation.telemetry import record_selection

    # Scenario 1: Intake-style row keyed by board task ID + run-id-keyed row
    # both reach completed/terminal outcome.
    board_task_id = "btk_intake_style_123"
    run = swarm.dal.create_run(
        working_dir=str(swarm.workdir), source="test",
        goal="Verify outcome telemetry updates work for run and board task",
        board_task_id=board_task_id,
    )
    run_id = run["id"]
    conn = swarm.dal._connection

    # Seed run-id row
    run_row_id = record_selection(
        conn,
        task_id=run_id,
        goal="Verify run-id row",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )
    # Seed board-task-id row
    board_row_id = record_selection(
        conn,
        task_id=board_task_id,
        goal="Verify board-task-id row",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )
    # Seed already terminal row (to prove they are not overwritten)
    already_terminal_id = record_selection(
        conn,
        task_id=board_task_id,
        goal="Already terminal board-task-id row",
        arm="formation",
        formation_id="coding",
        outcome="rejected",
        source="production",
    )

    # Set run status to completed and trigger the route
    swarm.dal.set_run_status(run_id, "completed")
    response = _run(swarm.client.get(f"/api/swarm/{run_id}", headers=auth_headers))
    assert response.status_code == 200

    # Retrieve and assert rows
    run_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (run_row_id,)
    ).fetchone()
    assert run_row is not None
    assert run_row["outcome"] == "accepted"
    assert run_row["finished_at"] is not None

    board_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (board_row_id,)
    ).fetchone()
    assert board_row is not None
    assert board_row["outcome"] == "accepted"
    assert board_row["finished_at"] is not None

    terminal_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (already_terminal_id,)
    ).fetchone()
    assert terminal_row is not None
    assert terminal_row["outcome"] == "rejected"
    assert terminal_row["finished_at"] is None  # seeded without finished_at, remains untouched

    # Scenario 2: Cancel route closes both run-id and board-task-id rows
    board_task_id_cancel = "btk_cancel_style_456"
    run_cancel = swarm.dal.create_run(
        working_dir=str(swarm.workdir), source="test",
        goal="Verify cancellation telemetry",
        board_task_id=board_task_id_cancel,
    )
    run_cancel_id = run_cancel["id"]
    swarm.dal.set_run_status(run_cancel_id, "running")

    # Seed run-id row
    run_cancel_row_id = record_selection(
        conn,
        task_id=run_cancel_id,
        goal="Verify cancel run-id row",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )
    # Seed board-task-id row
    board_cancel_row_id = record_selection(
        conn,
        task_id=board_task_id_cancel,
        goal="Verify cancel board-task-id row",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )
    # Seed already terminal row (to prove they are not overwritten on cancel)
    already_terminal_cancel_id = record_selection(
        conn,
        task_id=board_task_id_cancel,
        goal="Already terminal board-task-id row for cancel",
        arm="formation",
        formation_id="coding",
        outcome="accepted",
        source="production",
    )

    # Post cancel and trigger route
    cancel_resp = _run(
        swarm.client.post(f"/api/swarm/{run_cancel_id}/cancel", headers=auth_headers)
    )
    assert cancel_resp.status_code == 200

    # Retrieve and assert rows
    run_cancel_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (run_cancel_row_id,)
    ).fetchone()
    assert run_cancel_row is not None
    assert run_cancel_row["outcome"] == "cancelled"
    assert run_cancel_row["finished_at"] is not None

    board_cancel_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (board_cancel_row_id,)
    ).fetchone()
    assert board_cancel_row is not None
    assert board_cancel_row["outcome"] == "cancelled"
    assert board_cancel_row["finished_at"] is not None

    terminal_cancel_row = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?",
        (already_terminal_cancel_id,),
    ).fetchone()
    assert terminal_cancel_row is not None
    assert terminal_cancel_row["outcome"] == "accepted"
    assert terminal_cancel_row["finished_at"] is None
