"""M1 — the project axis is WRITTEN, not just declared.

Before M1 the ``project_id`` columns existed (014 tasks, 045 swarm_runs, 087
board_tasks) and the DALs accepted the value, but no production create path for
a swarm run, a board card or a routine ever supplied one — so per-project
scoping, budget rollups and C11 grant binding (migration 115) had nothing to
key on at runtime. These tests pin the three create paths and, just as
importantly, the DEFAULT.

The default is NULL and it is explicit. A card, run or routine whose creator
named no project is UNSCOPED; nothing invents a "default project" for it. That
is the same rule migration 115 states for grants, and for the same reason: the
broker reads a project binding as an authorization fact, so a fabricated
project id is a fabricated grant scope. NULL means "this row proves no project
binding" — honestly refusable — whereas a guessed default would silently make
unowned work look like project P's work to every budget and reach check.

A project that IS named must exist: the HTTP edges resolve it and answer 404,
rather than letting a dangling id through (``SwarmDal`` runs without
``PRAGMA foreign_keys=ON``, so the 087 FK does not catch it there).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
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
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines_tick import _task_kwargs
from omniagentos.scheduler.store import RoutinesStore
from omniagentos.sessions import token
from omniagentos.swarm.dal import SwarmDal
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store, migrated_db

PROJECT_A = "proj_axis_a"
PROJECT_B = "proj_axis_b"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _seed_projects(store: SqliteStore) -> None:
    now = utc_now_iso()
    for project_id in (PROJECT_A, PROJECT_B):
        store._connection.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project_id, f"Project {project_id}", now),
        )
    store._connection.commit()


# --- shared sqlite harness ---------------------------------------------------


@pytest.fixture
def collab(tmp_path: Path) -> CollabStore:
    return CollabStore(migrated_db(CollabStore, tmp_path / "axis.db"))


@pytest.fixture
def sql_store(collab: CollabStore) -> SqliteStore:
    store = collab._store
    _seed_projects(store)
    return store


@pytest.fixture
def dal(collab: CollabStore, sql_store: SqliteStore) -> Iterator[SwarmDal]:
    swarm_dal = SwarmDal(collab._store._db_path)
    try:
        yield swarm_dal
    finally:
        swarm_dal.close()


# ---------------------------------------------------------------------------
# 1. Swarm runs + the cards they provision
# ---------------------------------------------------------------------------


def _provision(dal: SwarmDal, project_id: Any, suffix: str) -> dict[str, Any]:
    run: dict[str, Any] = {"working_dir": "/tmp/ws", "goal": "scoped run", "plan": {}}
    if project_id is not _OMITTED:
        run["project_id"] = project_id
    return dal.provision_run(
        run=run,
        root_card={"id": f"btk_root_{suffix}", "title": "root", "status": "in_progress"},
        cards=[
            {"id": f"btk_child1_{suffix}", "title": "one", "swarm_json": {}},
            {"id": f"btk_child2_{suffix}", "title": "two", "swarm_json": {}},
        ],
        edges=[(f"btk_child2_{suffix}", f"btk_child1_{suffix}")],
    )


_OMITTED = object()


class TestSwarmProvision:
    def test_run_and_every_card_carry_the_project(
        self, dal: SwarmDal, collab: CollabStore
    ) -> None:
        """The whole point: a run's project reaches its board, in one write."""
        run = _provision(dal, PROJECT_A, "scoped")

        assert run["project_id"] == PROJECT_A
        for card_id in ("btk_root_scoped", "btk_child1_scoped", "btk_child2_scoped"):
            card = collab.get_board_task(card_id)
            assert card is not None
            assert card["project_id"] == PROJECT_A, card_id

    def test_omitted_project_provisions_an_unscoped_run(
        self, dal: SwarmDal, collab: CollabStore
    ) -> None:
        """The default is NULL — the pre-M1 behaviour, not a default project."""
        run = _provision(dal, _OMITTED, "bare")

        assert run["project_id"] is None
        for card_id in ("btk_root_bare", "btk_child1_bare", "btk_child2_bare"):
            assert collab.get_board_task(card_id)["project_id"] is None

    def test_blank_project_is_null_not_a_third_state(
        self, dal: SwarmDal, collab: CollabStore
    ) -> None:
        """`''` names no project, so it must not be persisted as a scope."""
        run = _provision(dal, "   ", "blank")

        assert run["project_id"] is None
        assert collab.get_board_task("btk_root_blank")["project_id"] is None

    def test_create_run_normalizes_blank_project(self, dal: SwarmDal) -> None:
        run = dal.create_run(working_dir="/tmp/ws", goal="g", project_id="", source="test")
        assert run["project_id"] is None

        scoped = dal.create_run(
            working_dir="/tmp/ws", goal="g", project_id=PROJECT_B, source="test"
        )
        assert scoped["project_id"] == PROJECT_B

    def test_split_subtasks_inherit_the_parent_card_project(
        self, dal: SwarmDal, collab: CollabStore
    ) -> None:
        """A split re-shapes work; it never moves it between projects."""
        run = _provision(dal, PROJECT_A, "split")

        result = dal.split_task(
            str(run["id"]),
            "btk_child1_split",
            [
                {"id": "btk_sub_one", "title": "sub one"},
                {"id": "btk_sub_two", "title": "sub two"},
            ],
        )

        assert result["subtask_ids"] == ["btk_sub_one", "btk_sub_two"]
        for subtask_id in result["subtask_ids"]:
            assert collab.get_board_task(subtask_id)["project_id"] == PROJECT_A

    def test_split_of_an_unscoped_parent_stays_unscoped(
        self, dal: SwarmDal, collab: CollabStore
    ) -> None:
        run = _provision(dal, _OMITTED, "usplit")

        dal.split_task(
            str(run["id"]),
            "btk_child1_usplit",
            [{"id": "btk_usub_one", "title": "sub one"}],
        )

        assert collab.get_board_task("btk_usub_one")["project_id"] is None


# ---------------------------------------------------------------------------
# 2. POST /api/swarm — the route that had no way to name a project at all
# ---------------------------------------------------------------------------


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


@pytest.fixture
def swarm_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collab: CollabStore,
    sql_store: SqliteStore,
    dal: SwarmDal,
) -> Iterator[SimpleNamespace]:
    """The same real-sqlite harness ``tests/api/test_swarm_routes.py`` uses."""
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board_files, "_approved_workspace_roots", lambda: [str(tmp_path.resolve())])
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides[get_store] = lambda: sql_store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[swarm_routes.get_swarm_dal] = lambda: dal
    app.dependency_overrides[swarm_routes.get_swarm_planner_llm] = lambda: _fake_planner_llm
    app.dependency_overrides[swarm_routes.get_swarm_clarify_llm] = lambda: _fake_clarify_llm
    app.dependency_overrides[swarm_routes.get_swarm_recall_fn] = lambda: _fake_recall_fn
    try:
        yield SimpleNamespace(
            client=client,
            collab=collab,
            dal=dal,
            workdir=workdir,
            headers={"X-Session-Token": token.load_or_create_token()},
        )
    finally:
        app.dependency_overrides.clear()
        _run(client.aclose())


class TestSwarmRoute:
    def test_named_project_scopes_the_run_and_its_whole_board(
        self, swarm_api: SimpleNamespace
    ) -> None:
        response = _run(
            swarm_api.client.post(
                "/api/swarm?sync=1",
                headers=swarm_api.headers,
                json={
                    "brief": "Build the thing",
                    "working_dir": str(swarm_api.workdir),
                    "project_id": PROJECT_A,
                },
            )
        )
        assert response.status_code == 202, response.text
        body = response.json()

        run = swarm_api.dal.get_run(body["swarm_run_id"])
        assert run["project_id"] == PROJECT_A

        cards = swarm_api.collab._connection.execute(
            "SELECT id, project_id FROM board_tasks WHERE swarm_run_id = ?",
            (body["swarm_run_id"],),
        ).fetchall()
        assert len(cards) == 5  # root + 3 planned tasks + auto integration task
        assert {row["project_id"] for row in cards} == {PROJECT_A}

    def test_omitting_the_project_keeps_the_pre_m1_unscoped_behaviour(
        self, swarm_api: SimpleNamespace
    ) -> None:
        response = _run(
            swarm_api.client.post(
                "/api/swarm?sync=1",
                headers=swarm_api.headers,
                json={"brief": "Build the thing", "working_dir": str(swarm_api.workdir)},
            )
        )
        assert response.status_code == 202, response.text
        body = response.json()

        assert swarm_api.dal.get_run(body["swarm_run_id"])["project_id"] is None
        cards = swarm_api.collab._connection.execute(
            "SELECT project_id FROM board_tasks WHERE swarm_run_id = ?",
            (body["swarm_run_id"],),
        ).fetchall()
        assert cards
        assert {row["project_id"] for row in cards} == {None}

    def test_unknown_project_is_refused_before_anything_is_provisioned(
        self, swarm_api: SimpleNamespace
    ) -> None:
        response = _run(
            swarm_api.client.post(
                "/api/swarm?sync=1",
                headers=swarm_api.headers,
                json={
                    "brief": "Build the thing",
                    "working_dir": str(swarm_api.workdir),
                    "project_id": "proj_does_not_exist",
                },
            )
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert swarm_api.dal.list_runs() == []

    def test_unknown_project_is_refused_on_the_async_path_too(
        self, swarm_api: SimpleNamespace
    ) -> None:
        """Resolved on the request thread: the caller sees the 404, not a job."""
        response = _run(
            swarm_api.client.post(
                "/api/swarm",
                headers=swarm_api.headers,
                json={
                    "brief": "Build the thing",
                    "working_dir": str(swarm_api.workdir),
                    "project_id": "proj_does_not_exist",
                },
            )
        )
        assert response.status_code == 404
        assert "job_id" not in response.json()


# ---------------------------------------------------------------------------
# 3. Board tasks — the self-assignable board create path
# ---------------------------------------------------------------------------


class TestBoardTaskCreate:
    def test_create_board_task_persists_the_project(
        self, collab: CollabStore, sql_store: SqliteStore
    ) -> None:
        card = BoardTask(title="scoped card", project_id=PROJECT_B)
        collab.create_board_task(card)
        assert collab.get_board_task(card.id)["project_id"] == PROJECT_B

    def test_create_board_task_defaults_to_unscoped(
        self, collab: CollabStore, sql_store: SqliteStore
    ) -> None:
        """Every existing caller (no project_id) keeps working, and gets NULL."""
        card = BoardTask(title="legacy card")
        collab.create_board_task(card)
        assert collab.get_board_task(card.id)["project_id"] is None

    def test_blank_project_is_stored_as_null(
        self, collab: CollabStore, sql_store: SqliteStore
    ) -> None:
        card = BoardTask(title="blank card", project_id="  ")
        collab.create_board_task(card)
        assert collab.get_board_task(card.id)["project_id"] is None

    def test_post_board_scopes_the_card(
        self, collab: CollabStore, sql_store: SqliteStore
    ) -> None:
        async def request() -> None:
            app.dependency_overrides[get_store] = lambda: sql_store
            app.dependency_overrides[get_collab_store] = lambda: collab
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    created = await client.post(
                        "/api/collab/board",
                        json={"title": "via http", "project_id": PROJECT_A},
                    )
                    assert created.status_code == 201, created.text
                    assert created.json()["project_id"] == PROJECT_A

                    bare = await client.post("/api/collab/board", json={"title": "unscoped"})
                    assert bare.status_code == 201
                    assert bare.json()["project_id"] is None

                    unknown = await client.post(
                        "/api/collab/board",
                        json={"title": "bad", "project_id": "proj_nope"},
                    )
                    assert unknown.status_code == 404
                    assert unknown.json()["error"]["code"] == "not_found"
            finally:
                app.dependency_overrides.clear()

        _run(request())


# ---------------------------------------------------------------------------
# 4. Routines — migration 116 plus its create path
# ---------------------------------------------------------------------------


@pytest.fixture
def routine_store(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "routines-axis.db")
    _seed_projects(store)
    return store


@pytest.fixture
def routines(routine_store: SqliteStore) -> RoutinesStore:
    return RoutinesStore(routine_store)


class TestRoutineProjectScope:
    def test_migration_116_adds_the_column_and_its_index(
        self, routine_store: SqliteStore
    ) -> None:
        columns = {
            row["name"]
            for row in routine_store._connection.execute("PRAGMA table_info(routines)").fetchall()
        }
        assert "project_id" in columns
        indexes = routine_store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_routines_project",),
        ).fetchall()
        assert len(indexes) == 1

    def test_create_routine_persists_the_project(self, routines: RoutinesStore) -> None:
        created = routines.create_routine(valid_routine_payload(project_id=PROJECT_A))
        assert created["project_id"] == PROJECT_A
        assert routines.get_routine(created["id"])["project_id"] == PROJECT_A

    def test_create_routine_defaults_to_unscoped(self, routines: RoutinesStore) -> None:
        """Existing callers (built-in system routines included) keep working."""
        created = routines.create_routine(valid_routine_payload())
        assert created["project_id"] is None

    def test_blank_project_is_stored_as_null(self, routines: RoutinesStore) -> None:
        created = routines.create_routine(valid_routine_payload(project_id="   "))
        assert created["project_id"] is None

    def test_post_routines_scopes_the_routine(self, routine_store: SqliteStore) -> None:
        async def request() -> None:
            app.dependency_overrides[get_store] = lambda: routine_store
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    created = await client.post(
                        "/api/routines",
                        json=valid_routine_payload(project_id=PROJECT_B),
                    )
                    assert created.status_code == 201, created.text
                    assert created.json()["project_id"] == PROJECT_B

                    unknown = await client.post(
                        "/api/routines",
                        json=valid_routine_payload(name="other", project_id="proj_nope"),
                    )
                    assert unknown.status_code == 404
                    assert unknown.json()["error"]["code"] == "not_found"
            finally:
                app.dependency_overrides.clear()

        _run(request())

    def test_post_routines_without_a_project_is_unscoped(
        self, routine_store: SqliteStore
    ) -> None:
        async def request() -> None:
            app.dependency_overrides[get_store] = lambda: routine_store
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    created = await client.post("/api/routines", json=valid_routine_payload())
                    assert created.status_code == 201, created.text
                    assert created.json()["project_id"] is None
            finally:
                app.dependency_overrides.clear()

        _run(request())


class TestRoutineFiringInheritsProject:
    """A routine's project is what its firings are scoped to (M1)."""

    def test_task_inherits_the_routine_project(self) -> None:
        kwargs = _task_kwargs({"title": "work"}, PROJECT_A)
        assert kwargs["project_id"] == PROJECT_A

    def test_template_project_wins_over_the_routine(self) -> None:
        """Template is the more specific statement AND the pre-M1 field, so a
        routine written before migration 116 fires into exactly the project it
        always did."""
        kwargs = _task_kwargs({"title": "work", "project_id": PROJECT_B}, PROJECT_A)
        assert kwargs["project_id"] == PROJECT_B

    def test_neither_source_means_unscoped(self) -> None:
        assert _task_kwargs({"title": "work"}, None)["project_id"] is None
        assert _task_kwargs({"title": "work"})["project_id"] is None
