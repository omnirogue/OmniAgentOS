"""Migration 100 — durable plans spine (LANE A / t/cb-plans).

Plans survive API restart (no in-memory dict) and the DAL enforces, on every
write path: the app-side status vocabulary, terminal states that stay terminal,
and one current (approved) plan per source. The partial unique indexes carry the
non-terminal invariant at the schema level for any writer that roots a plan on a
chat or a card.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate
from omniagentos.db.store import SqliteStore
from omniagentos.plans.store import PlansStore

MIGRATION_VERSION = 100
MIGRATION_NAME = "100_plans_durable.sql"
NEW_TABLES = ("plans",)
NOW = "2026-07-31T00:00:00Z"


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        str(row["name"]): row
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _seed_chat(connection: sqlite3.Connection, chat_id: str = "cht_seed") -> str:
    """Create a chat with a bound board_task (chats.board_task_id is NOT NULL UNIQUE)."""
    # First create the board_task.
    board_task_id = chat_id.replace("cht_", "btk_")
    connection.execute(
        "INSERT INTO board_tasks (id, title, description, priority, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (board_task_id, "test task", "", "normal", "open", NOW, NOW),
    )
    # Then create the chat.
    connection.execute(
        "INSERT INTO chats (id, board_task_id, title, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (chat_id, board_task_id, "test chat", "active", NOW, NOW),
    )
    return chat_id


def _seed_company(connection: sqlite3.Connection, company_id: str = "co_seed") -> str:
    connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
        (company_id, company_id.replace("co_", ""), company_id, "active", NOW),
    )
    return company_id


def _migrated_db(tmp_path: Path, name: str = "plans.db") -> str:
    db_path = str(tmp_path / name)
    assert migrate(db_path) >= MIGRATION_VERSION
    return db_path


# ---------------------------------------------------------------------------
# Packaging / upgrade path
# ---------------------------------------------------------------------------


def test_100_is_packaged_under_its_own_name() -> None:
    packaged = dict(_migration_files())
    assert MIGRATION_VERSION in packaged, "100_plans_durable.sql must be packaged"
    assert packaged[MIGRATION_VERSION].name == MIGRATION_NAME


def test_100_adds_plans_table_to_a_v099_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade path: a v099 DB gains the plans table, nothing else moves."""
    db_path = str(tmp_path / "upgrade-101.db")
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(99))
    assert migrate(db_path) == 99

    connection = _connect(db_path)
    try:
        assert not (set(NEW_TABLES) & _tables(connection))
    finally:
        connection.close()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(100))
    assert migrate(db_path) == 100

    connection = _connect(db_path)
    try:
        assert set(NEW_TABLES) <= _tables(connection)
        plans = _columns(connection, "plans")
        assert set(plans) == {
            "id",
            "chat_id",
            "board_task_id",
            "version",
            "status",
            "plan_json",
            "content_md",
            "goal",
            "harness",
            "execute_mode",
            "speed",
            "route_json",
            "route_target_name",
            "decided_by",
            "decided_at",
            "reason",
            "org_company_id",
            "work_folder",
            "execution_metadata",
            "created_at",
            "updated_at",
        }
        assert plans["id"]["pk"] == 1
        assert plans["chat_id"]["notnull"] == 0  # nullable
        assert plans["board_task_id"]["notnull"] == 0  # nullable
        assert plans["version"]["notnull"] == 1
        assert plans["status"]["notnull"] == 1
        assert str(plans["status"]["dflt_value"]).strip("'") == "draft"
        assert plans["plan_json"]["notnull"] == 1
        assert plans["content_md"]["notnull"] == 0  # nullable
        assert plans["org_company_id"]["notnull"] == 0  # nullable
        assert plans["work_folder"]["notnull"] == 0  # nullable
        assert plans["created_at"]["notnull"] == 1
        assert plans["updated_at"]["notnull"] == 1
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Durable storage (survives API restart)
# ---------------------------------------------------------------------------


def test_plan_survives_api_restart(tmp_path: Path) -> None:
    """Red-first: write a plan, close the DB, re-read it. Without the table, this fails."""
    db_path = _migrated_db(tmp_path)

    # Write a plan.
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_restart_test")
    plan_data = {
        "chat_id": "cht_restart_test",
        "version": 1,
        "status": "draft",
        "plan_json": json.dumps({"project_name": "Test", "description": "test plan"}),
        "content_md": "# Test Plan",
    }
    created = plan_store.create_plan(**plan_data)
    plan_id = created["id"]
    store._connection.close()

    # Simulate API restart: drop the in-memory store, re-open the DB, re-read.
    store2 = SqliteStore(db_path)
    plan_store2 = PlansStore(store2)
    retrieved = plan_store2.get_plan(plan_id)
    store2._connection.close()

    assert retrieved is not None
    assert retrieved["plan_json"] == plan_data["plan_json"]
    assert retrieved["status"] == "draft"
    assert retrieved["chat_id"] == "cht_restart_test"


# ---------------------------------------------------------------------------
# Concurrency: prevent planner races
# ---------------------------------------------------------------------------


def test_two_concurrent_planners_on_same_chat_race(tmp_path: Path) -> None:
    """Red-first: two planners create draft plans on the same chat; only one succeeds."""
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_race_test")

    plan_json = json.dumps({"project_name": "Test", "description": "test plan"})

    # Planner 1 creates a draft plan.
    plan1 = plan_store.create_plan(
        chat_id="cht_race_test",
        version=1,
        status="draft",
        plan_json=plan_json,
    )
    assert plan1["status"] == "draft"

    # Planner 2 tries to create another draft plan for the same chat → IntegrityError.
    with pytest.raises(sqlite3.IntegrityError):
        plan_store.create_plan(
            chat_id="cht_race_test",
            version=1,
            status="draft",
            plan_json=plan_json,
        )

    store._connection.close()


def test_two_concurrent_planners_on_same_task_race(tmp_path: Path) -> None:
    """Two planners create draft plans on the same board_task; only one succeeds."""
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)

    # Create a board task.
    store._connection.execute(
        "INSERT INTO board_tasks (id, title, description, priority, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("btk_race_test", "test task", "", "normal", "open", NOW, NOW),
    )

    plan_json = json.dumps({"project_name": "Test", "description": "test plan"})

    # Planner 1 creates a draft plan.
    plan1 = plan_store.create_plan(
        board_task_id="btk_race_test",
        version=1,
        status="draft",
        plan_json=plan_json,
    )
    assert plan1["status"] == "draft"

    # Planner 2 tries to create another draft plan for the same task → IntegrityError.
    with pytest.raises(sqlite3.IntegrityError):
        plan_store.create_plan(
            board_task_id="btk_race_test",
            version=1,
            status="draft",
            plan_json=plan_json,
        )

    store._connection.close()


def test_multiple_versions_allowed_nonterminal_race_still_prevented(tmp_path: Path) -> None:
    """Multiple versions (v1, v2, v3) allowed; but only one non-terminal per chat."""
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_multi_version")

    plan_json_v1 = json.dumps({"project_name": "V1", "description": "v1 plan"})
    plan_json_v2 = json.dumps({"project_name": "V2", "description": "v2 plan"})

    # Create v1 as draft.
    plan_v1 = plan_store.create_plan(
        chat_id="cht_multi_version",
        version=1,
        status="draft",
        plan_json=plan_json_v1,
    )

    # Transition v1 to ready (now it's non-terminal but different from draft).
    def prepare_ready(row):
        return {"status": "ready"}

    plan_store.update_plan(plan_v1["id"], prepare_ready)

    # Approve v1 (now it's terminal).
    def prepare_approved(row):
        return {"status": "approved"}

    plan_store.update_plan(plan_v1["id"], prepare_approved)

    # Now v2 can be created as draft (v1 is terminal).
    plan_v2 = plan_store.create_plan(
        chat_id="cht_multi_version",
        version=2,
        status="draft",
        plan_json=plan_json_v2,
    )
    assert plan_v2["version"] == 2

    # But a second draft for v2 still fails.
    with pytest.raises(sqlite3.IntegrityError):
        plan_store.create_plan(
            chat_id="cht_multi_version",
            version=2,
            status="draft",
            plan_json=plan_json_v2,
        )

    store._connection.close()


# ---------------------------------------------------------------------------
# Company/folder binding
# ---------------------------------------------------------------------------


def test_plan_round_trip_with_org_company_and_folder(tmp_path: Path) -> None:
    """Company and folder metadata survive round-trip to DB."""
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_company_test")
    _seed_company(store._connection, "co_test")

    plan_json = json.dumps({"project_name": "Company Work", "description": "company plan"})

    created = plan_store.create_plan(
        chat_id="cht_company_test",
        version=1,
        status="draft",
        plan_json=plan_json,
        org_company_id="co_test",
        work_folder="~/Company/Project",
    )

    retrieved = plan_store.get_plan(created["id"])
    assert retrieved is not None
    assert retrieved["org_company_id"] == "co_test"
    assert retrieved["work_folder"] == "~/Company/Project"

    store._connection.close()


# ---------------------------------------------------------------------------
# Vocabulary validation
# ---------------------------------------------------------------------------


def test_plan_create_refuses_bad_status(tmp_path: Path) -> None:
    """Red-first: bad status at create time is REFUSED, store unchanged."""
    from omniagentos.plans import PlanValidationError

    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_bad_status")

    plan_json = json.dumps({"project_name": "Test", "description": "test"})

    # Try to create with an invalid status.
    with pytest.raises(PlanValidationError) as exc_info:
        plan_store.create_plan(
            chat_id="cht_bad_status",
            version=1,
            status="INVALID_STATUS",
            plan_json=plan_json,
        )

    assert "status must be one of" in str(exc_info.value)

    # Store is unchanged: no row was created.
    plans = plan_store.list_plans(chat_id="cht_bad_status")
    assert len(plans) == 0

    store._connection.close()


def test_plan_create_allows_both_refs_null(tmp_path: Path) -> None:
    """Both chat_id and board_task_id can be null for provisional plans.

    Plans created during the planning phase (before confirm) have not yet been
    assigned to a specific chat or task, so both refs can be null. They get
    populated at confirm time when the plan is assigned to a destination.
    """
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)

    plan_json = json.dumps({"project_name": "Test", "description": "test"})

    # Creating a plan with neither chat_id nor board_task_id should succeed
    # (this is for provisional/in-planning plans)
    created = plan_store.create_plan(
        chat_id=None,
        board_task_id=None,
        version=1,
        status="draft",
        plan_json=plan_json,
    )

    assert created is not None
    assert created["chat_id"] is None
    assert created["board_task_id"] is None

    store._connection.close()


def test_plan_update_refuses_bad_status(tmp_path: Path) -> None:
    """Bad status at UPDATE time is refused by the store, row byte-unchanged.

    The vocabulary has to bind the update path too. A callback is free to hand
    back anything; the store — not the caller — is what refuses it, otherwise
    "validated app-side" only means "validated by callers who remembered to".
    """
    from omniagentos.plans import PlanValidationError

    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_update_bad_status")

    plan_json = json.dumps({"project_name": "Test", "description": "test"})
    created = plan_store.create_plan(
        chat_id="cht_update_bad_status",
        version=1,
        status="draft",
        plan_json=plan_json,
    )
    before = plan_store.get_plan(created["id"])
    assert before is not None

    def prepare_bad(row: dict) -> dict:
        return {"status": "TOTALLY_BOGUS", "content_md": "# should not land"}

    with pytest.raises(PlanValidationError) as exc_info:
        plan_store.update_plan(created["id"], prepare_bad)
    assert "status must be one of" in str(exc_info.value)

    # The whole transaction rolled back: not the status, not the co-written column.
    after = plan_store.get_plan(created["id"])
    assert after == before
    assert after is not None
    assert after["status"] == "draft"
    assert after["content_md"] is None

    store._connection.close()


def test_plan_update_refuses_transition_out_of_a_terminal_state(tmp_path: Path) -> None:
    """Terminal means terminal: superseded -> ready and rejected -> approved are refused.

    A rejection whose reason can be silently cleared by a later approval is not
    an audit record, and two live approved plans for one source is exactly the
    "you approve the wrong plan" failure.
    """
    from omniagentos.plans import PlanValidationError

    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)

    plan_json = json.dumps({"project_name": "Test", "description": "test"})
    rejected = plan_store.create_plan(version=1, status="ready", plan_json=plan_json)
    plan_store.reject_plan(rejected["id"], decided_by="operator", reason="not what I asked for")

    with pytest.raises(PlanValidationError, match="rejected is terminal"):
        plan_store.update_plan(rejected["id"], lambda row: {"status": "approved", "reason": None})
    still = plan_store.get_plan(rejected["id"])
    assert still is not None
    assert still["status"] == "rejected"
    assert still["reason"] == "not what I asked for"

    superseded = plan_store.create_plan(version=1, status="ready", plan_json=plan_json)
    plan_store.supersede_plan(superseded["id"], reason="v2 took over")
    with pytest.raises(PlanValidationError, match="superseded is terminal"):
        plan_store.update_plan(superseded["id"], lambda row: {"status": "ready"})
    assert plan_store.get_plan(superseded["id"])["status"] == "superseded"

    # The ONE allowed exit: an approved plan is retired when a newer one wins.
    approved = plan_store.create_plan(version=1, status="ready", plan_json=plan_json)
    plan_store.approve_plan(approved["id"], decided_by="operator")
    assert plan_store.supersede_plan(approved["id"], reason="v2")["status"] == "superseded"

    store._connection.close()


def test_one_current_approved_plan_per_chat(tmp_path: Path) -> None:
    """A chat cannot end up with two approved plans and no rule for which is live."""
    from omniagentos.plans import PlanValidationError

    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_two_approvals")

    plan_json = json.dumps({"project_name": "Test", "description": "test"})
    v1 = plan_store.create_plan(
        chat_id="cht_two_approvals", version=1, status="ready", plan_json=plan_json
    )
    plan_store.approve_plan(v1["id"], decided_by="operator")

    v2 = plan_store.create_plan(
        chat_id="cht_two_approvals", version=2, status="ready", plan_json=plan_json
    )
    with pytest.raises(PlanValidationError, match="already has an approved plan"):
        plan_store.approve_plan(v2["id"], decided_by="operator")
    assert plan_store.get_plan(v2["id"])["status"] == "ready"

    # Superseding the incumbent is what makes room for the new one.
    plan_store.supersede_plan(v1["id"], reason="v2 approved")
    assert plan_store.approve_plan(v2["id"], decided_by="operator")["status"] == "approved"
    approved = plan_store.list_plans(chat_id="cht_two_approvals", status="approved")
    assert [row["id"] for row in approved] == [v2["id"]]

    store._connection.close()


def test_confirm_columns_round_trip(tmp_path: Path) -> None:
    """The row carries everything an approval after a restart needs.

    Without route_json/execute_mode/speed/harness/goal on the row, a rehydrated
    plan has no route and confirm cannot run it — the plan is visible and dead.
    """
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)

    created = plan_store.create_plan(
        version=1,
        status="ready",
        plan_json=json.dumps({"project_name": "Test"}),
        goal="ship the thing",
        harness="claude",
        execute_mode="session",
        speed="ultra",
        route_json=json.dumps({"decision": "new", "project_name": "Test"}),
        route_target_name="Test",
    )
    store._connection.close()

    reopened = SqliteStore(db_path)
    row = PlansStore(reopened).get_plan(created["id"])
    assert row is not None
    assert row["goal"] == "ship the thing"
    assert row["harness"] == "claude"
    assert row["execute_mode"] == "session"
    assert row["speed"] == "ultra"
    assert json.loads(row["route_json"])["decision"] == "new"
    assert row["route_target_name"] == "Test"
    reopened._connection.close()


def test_plan_read_model_exposes_every_column(tmp_path: Path) -> None:
    """``Plan`` must not silently drop a column — that is how a field goes write-only."""
    from omniagentos.plans import Plan

    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    created = plan_store.create_plan(
        version=1,
        status="ready",
        plan_json=json.dumps({"project_name": "Test"}),
        execution_metadata=json.dumps({"owned_paths": ["omniagentos/plans/"]}),
    )
    row = plan_store.get_plan(created["id"])
    assert row is not None

    model = Plan.from_row(row)
    assert set(model.model_dump()) == set(row)
    assert json.loads(model.execution_metadata)["owned_paths"] == ["omniagentos/plans/"]

    store._connection.close()


def test_plan_update_validation_via_callback_exception(tmp_path: Path) -> None:
    """Update validation: callback can RAISE to abort the transaction."""
    db_path = _migrated_db(tmp_path)
    store = SqliteStore(db_path)
    plan_store = PlansStore(store)
    _seed_chat(store._connection, "cht_update_callback_exception")

    plan_json = json.dumps({"project_name": "Test", "description": "test"})

    created = plan_store.create_plan(
        chat_id="cht_update_callback_exception",
        version=1,
        status="draft",
        plan_json=plan_json,
    )

    # A callback that raises will abort the transaction.
    def prepare_with_validation(row):
        # Validate that we can only transition from draft to ready.
        current_status = row.get("status")
        if current_status != "draft":
            raise ValueError(f"can only transition from draft, got {current_status}")
        return {"status": "ready"}

    # This should succeed (draft -> ready is valid).
    updated = plan_store.update_plan(created["id"], prepare_with_validation)
    assert updated["status"] == "ready"

    # Try to transition ready -> draft (invalid) via a callback that validates.
    def prepare_invalid_transition(row):
        current_status = row.get("status")
        if current_status == "ready":
            raise ValueError("cannot go backwards from ready")
        return {"status": "draft"}

    with pytest.raises(ValueError, match="cannot go backwards"):
        plan_store.update_plan(created["id"], prepare_invalid_transition)

    # Store is unchanged: status is still "ready".
    retrieved = plan_store.get_plan(created["id"])
    assert retrieved["status"] == "ready"

    store._connection.close()


# ---------------------------------------------------------------------------
# Counterfeits for this lane are executable, not narrated: see
# tests/counterfeits/corpus.d/cb-plans.toml. Both entries mutate the PRODUCT
# seam (the durable sync call and the approval write in
# omniagentos/api/routes/intake.py) and point at route-level tests in
# tests/api/test_intake_plans_durable.py.
# ---------------------------------------------------------------------------
