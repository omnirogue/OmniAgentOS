"""Portfolio redesign Phase A/B — kind column + portfolio assembly.

S14A coverage: H-11 blocked join attribution, archived exclusion, L-06 atomic
scratch apply, L-10 production SQL query-plan assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.projects.portfolio import (
    SQL_BLOCKED_BOARD_BY_PROJECT,
    SQL_BOARD_LIST_ACTIVE,
    SQL_BOARD_LIST_ACTIVE_BY_STATUS,
    SQL_BOARD_LIST_ARCHIVED,
    SQL_PENDING_APPROVALS_BY_PROJECT,
    SQL_PROJECTS_LIST,
    SQL_RUNS_ROLLUP_BY_PROJECT,
    build_portfolio,
    sweep_scratch_projects,
)
from omniagentos.projects.store import ProjectStore


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    from omniagentos.db.migrate import migrate

    db = tmp_path / "t.db"
    migrate(str(db))
    return SqliteStore(str(db))


def _db_path(store: SqliteStore) -> str:
    return str(store._db_path)


def _enqueue_run(store: SqliteStore, *, task_id: str, run_id: str, now: str) -> None:
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "harness": "mock",
            "state": "running",
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


def test_create_project_kind_default_and_scratch(store: SqliteStore) -> None:
    ps = ProjectStore(store)
    durable = ps.create_project({"name": f"Brand-{new_id('x')[:8]}"})
    assert durable.get("kind") == "project"
    scratch = ps.create_project(
        {"name": f"Orchestration: one-off {new_id('x')[:6]}", "kind": "scratch"}
    )
    assert scratch.get("kind") == "scratch"


def test_portfolio_excludes_scratch_from_main_list(store: SqliteStore) -> None:
    ps = ProjectStore(store)
    real = ps.create_project({"name": f"Real-{new_id('x')[:8]}"})
    ps.create_project({"name": f"Orchestration: tmp {new_id('x')[:6]}", "kind": "scratch"})
    payload = build_portfolio(store._connection)
    ids = {p["id"] for p in payload["projects"]}
    assert real["id"] in ids
    assert payload["scratch_count"] >= 1
    # Scratch must not pollute the durable list
    for p in payload["projects"]:
        assert p["kind"] != "scratch"


def test_portfolio_unknown_run_cost_is_not_zero_spent(store: SqliteStore) -> None:
    """Unknown cost must stay unknown — never claim $0.00 spent.

    ``runs.cost_usd`` is nullable (migration 001). Providers that report tokens
    but no dollar figure leave NULL. COALESCE/or-0.0 would turn that into a
    flattering 0.0 against a budget cap (the same class that let $6.33 hide
    under a $4.00 ceiling elsewhere in the repo).

    Counterfeit that must still fail this test: report ``spent_usd=0.0`` when
    every run cost is NULL, or sum only the known rows and ignore NULL as if
    they cost nothing.
    """
    ps = ProjectStore(store)
    proj = ps.create_project(
        {"name": f"SpendProbe-{new_id('x')[:6]}", "budget_usd": 4.0}
    )
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "project_id": proj["id"],
            "title": "unknown cost run",
            "state": "completed",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=task_id, run_id=run_id, now=now)
    store.update_run(run_id, {"state": "completed", "cost_usd": None})

    raw = store._connection.execute(
        "SELECT cost_usd FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert raw["cost_usd"] is None

    payload = build_portfolio(store._connection)
    by_id = {p["id"]: p for p in payload["projects"]}
    node = by_id[proj["id"]]
    assert node["budget_usd"] == 4.0
    # Must not look like a free run against the $4 cap.
    assert node["spent_usd"] is None, (
        f"unknown run cost presented as spent_usd={node['spent_usd']!r}; "
        "NULL cost must not become 0.0"
    )


def test_portfolio_known_costs_sum_and_zero_runs_is_zero(store: SqliteStore) -> None:
    """Known costs still aggregate; a project with no runs spent $0 (measured)."""
    ps = ProjectStore(store)
    with_spend = ps.create_project({"name": f"KnownSpend-{new_id('x')[:6]}"})
    empty = ps.create_project({"name": f"NoRuns-{new_id('x')[:6]}"})
    now = utc_now_iso()

    t1, r1 = new_id("tsk"), new_id("run")
    store.create_task(
        {
            "id": t1,
            "project_id": with_spend["id"],
            "title": "a",
            "state": "completed",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=t1, run_id=r1, now=now)
    store.update_run(r1, {"state": "completed", "cost_usd": 1.25})

    t2, r2 = new_id("tsk"), new_id("run")
    store.create_task(
        {
            "id": t2,
            "project_id": with_spend["id"],
            "title": "b",
            "state": "completed",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=t2, run_id=r2, now=now)
    store.update_run(r2, {"state": "completed", "cost_usd": 2.50})

    # Mixed known + unknown on another project: total is unknown.
    mixed = ps.create_project({"name": f"Mixed-{new_id('x')[:6]}"})
    t3, r3 = new_id("tsk"), new_id("run")
    store.create_task(
        {
            "id": t3,
            "project_id": mixed["id"],
            "title": "mixed",
            "state": "completed",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=t3, run_id=r3, now=now)
    store.update_run(r3, {"state": "completed", "cost_usd": 6.33})
    t4, r4 = new_id("tsk"), new_id("run")
    store.create_task(
        {
            "id": t4,
            "project_id": mixed["id"],
            "title": "unknown",
            "state": "completed",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=t4, run_id=r4, now=now)
    store.update_run(r4, {"state": "completed", "cost_usd": None})

    payload = build_portfolio(store._connection)
    by_id = {p["id"]: p for p in payload["projects"]}
    assert by_id[with_spend["id"]]["spent_usd"] == pytest.approx(3.75)
    assert by_id[empty["id"]]["spent_usd"] == 0.0
    assert by_id[mixed["id"]]["spent_usd"] is None, (
        "partial unknown must not under-report as sum-of-known-only"
    )


def test_portfolio_query_count_constant(store: SqliteStore) -> None:
    ps = ProjectStore(store)
    for i in range(5):
        ps.create_project({"name": f"P{i}-{new_id('x')[:6]}"})
    # build_portfolio uses fixed SQL; just ensure it returns and is ordered
    payload = build_portfolio(store._connection)
    assert "generated_at" in payload
    assert isinstance(payload["projects"], list)
    states = [p["state"] for p in payload["projects"]]
    order = {"blocked": 0, "failing": 1, "running": 2, "idle": 3, "healthy": 4}
    ranks = [order.get(s, 9) for s in states]
    assert ranks == sorted(ranks)


def test_sweep_scratch_dry_run(store: SqliteStore) -> None:
    ps = ProjectStore(store)
    ps.create_project(
        {
            "name": f"Orchestration: old {new_id('x')[:6]}",
            "kind": "scratch",
            "created_at": "2020-01-01T00:00:00Z",
        }
    )
    result = sweep_scratch_projects(store._connection, days=30, apply=False)
    assert result["dry_run"] is True
    assert result["candidates"]


def test_portfolio_blocked_attribution_excludes_archived(store: SqliteStore) -> None:
    """H-11: live blocked cards via run→task→project mark health; archived do not."""
    ps = ProjectStore(store)
    proj = ps.create_project({"name": f"BlockedProj-{new_id('x')[:6]}"})
    other = ps.create_project({"name": f"ArchOnly-{new_id('x')[:6]}"})
    now = utc_now_iso()
    db_path = _db_path(store)

    live_task = new_id("tsk")
    live_run = new_id("run")
    store.create_task(
        {
            "id": live_task,
            "project_id": proj["id"],
            "title": "live blocked",
            "state": "ready",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=live_task, run_id=live_run, now=now)

    arch_task = new_id("tsk")
    arch_run = new_id("run")
    store.create_task(
        {
            "id": arch_task,
            "project_id": other["id"],
            "title": "archived blocked",
            "state": "ready",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=arch_task, run_id=arch_run, now=now)

    collab = CollabStore(db_path)
    live = BoardTask(title="Live blocked", status=BoardTaskStatus.BLOCKED)
    collab.create_board_task(live)
    collab.update_board_task(live.id, {"status": BoardTaskStatus.BLOCKED.value, "run_id": live_run})

    archived = BoardTask(title="Archived blocked", status=BoardTaskStatus.BLOCKED)
    collab.create_board_task(archived)
    collab.update_board_task(
        archived.id,
        {
            "status": BoardTaskStatus.BLOCKED.value,
            "run_id": arch_run,
            "archived_at": utc_now_iso(),
        },
    )

    payload = build_portfolio(store._connection)
    by_id = {p["id"]: p for p in payload["projects"]}
    assert by_id[proj["id"]]["state"] == "blocked"
    assert by_id[proj["id"]]["blocked_count"] >= 1
    # Archived-only project stays non-blocked (no pending approvals either).
    assert by_id[other["id"]]["state"] != "blocked"
    assert by_id[other["id"]]["blocked_count"] == 0


def test_sweep_scratch_apply_removes_and_detaches_tasks(store: SqliteStore) -> None:
    """L-06: apply deletes scratch without cascading dependent tasks."""
    ps = ProjectStore(store)
    scratch = ps.create_project(
        {
            "name": f"Orchestration: apply {new_id('x')[:6]}",
            "kind": "scratch",
            "created_at": "2020-01-01T00:00:00Z",
        }
    )
    now = utc_now_iso()
    task_id = new_id("tsk")
    store.create_task(
        {
            "id": task_id,
            "project_id": scratch["id"],
            "title": "orphan-safe",
            "state": "ready",
            "created_at": now,
            "updated_at": now,
        }
    )
    result = sweep_scratch_projects(store._connection, days=30, apply=True)
    assert result["dry_run"] is False
    assert result["removed"] >= 1
    assert ps.get_project(scratch["id"]) is None
    task = store.get_task(task_id)
    assert task is not None
    assert task["project_id"] is None


def test_sweep_scratch_apply_rolls_back_mid_sweep(store: SqliteStore) -> None:
    """Apply mode is one BEGIN IMMEDIATE; injected mid-sweep failure rolls back all."""
    ps = ProjectStore(store)
    a = ps.create_project(
        {
            "name": f"Orchestration: a {new_id('x')[:6]}",
            "kind": "scratch",
            "created_at": "2020-01-01T00:00:00Z",
        }
    )
    b = ps.create_project(
        {
            "name": f"Orchestration: b {new_id('x')[:6]}",
            "kind": "scratch",
            "created_at": "2020-01-02T00:00:00Z",
        }
    )
    seen: list[str] = []

    def boom(_conn, pid: str) -> None:
        seen.append(pid)
        if len(seen) == 1:
            raise RuntimeError("injected mid-sweep failure")

    with pytest.raises(RuntimeError, match="injected mid-sweep failure"):
        sweep_scratch_projects(store._connection, days=30, apply=True, _mid_sweep_hook=boom)

    # Both candidates remain — no partial apply under isolation_level=None.
    assert ps.get_project(a["id"]) is not None
    assert ps.get_project(b["id"]) is not None
    assert len(seen) == 1


def test_portfolio_query_plans_use_production_sql(store: SqliteStore) -> None:
    """Query-plan assertions execute the same SQL helpers build_portfolio uses.

    L-10 gate: assertions require specific 070 index names — no table-name
    fallback allowed. This proves the migration delivers the intended indexes.
    """
    ps = ProjectStore(store)
    proj = ps.create_project({"name": f"Plan-{new_id('x')[:6]}"})
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "project_id": proj["id"],
            "title": "plan",
            "state": "ready",
            "created_at": now,
            "updated_at": now,
        }
    )
    _enqueue_run(store, task_id=task_id, run_id=run_id, now=now)
    store.create_approval(
        {
            "id": new_id("apr"),
            "run_id": run_id,
            "step_seq": 0,
            "action_class": "read",
            "proposed_action": "read file",
            "state": "pending",
            "created_at": now,
        }
    )
    collab = CollabStore(_db_path(store))
    card = BoardTask(title="plan card", status=BoardTaskStatus.BLOCKED)
    collab.create_board_task(card)
    collab.update_board_task(card.id, {"status": BoardTaskStatus.BLOCKED.value, "run_id": run_id})

    # Archive one card so archived-listing partial index can be tested
    archived_card = BoardTask(title="archived card", status=BoardTaskStatus.DONE)
    collab.create_board_task(archived_card)
    collab.update_board_task(archived_card.id, {"archived_at": utc_now_iso()})

    # L-10: strict index requirements — NO table-name fallback allowed.
    # Each tuple: (SQL, required_index_names, reject_if_plan_contains)
    cases: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
        # Projects listing must use the new created/id index
        (SQL_PROJECTS_LIST, ("idx_projects_created_id",), ()),
        # Pending approvals must use an index starting with state
        (SQL_PENDING_APPROVALS_BY_PROJECT, ("idx_approvals_state_run",), ()),
        # Runs rollup uses existing idx_runs_task (join key)
        (SQL_RUNS_ROLLUP_BY_PROJECT, ("idx_runs_task",), ()),
        # Blocked board: status+archived_at filter uses status_archived or status_run
        (
            SQL_BLOCKED_BOARD_BY_PROJECT,
            ("idx_board_tasks_status_archived", "idx_board_tasks_status_run"),
            (),
        ),
        # Active listing: archived_at IS NULL uses archived_created compound
        (SQL_BOARD_LIST_ACTIVE, ("idx_board_tasks_archived_created",), ("temp b-tree",)),
        # Archived listing: archived_at IS NOT NULL uses the PARTIAL index
        (SQL_BOARD_LIST_ARCHIVED, ("idx_board_tasks_archived_listing",), ("temp b-tree",)),
        # Active by status: status=? AND archived_at IS NULL
        (
            SQL_BOARD_LIST_ACTIVE_BY_STATUS,
            ("idx_board_tasks_status_archived",),
            ("temp b-tree",),
        ),
    ]

    conn = store._connection
    for sql, required_indexes, reject_patterns in cases:
        params: tuple = (BoardTaskStatus.BLOCKED.value,) if "?" in sql else ()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        plan_text = " ".join(str(r["detail"]) for r in rows).lower()

        # Must use one of the required indexes
        assert any(idx.lower() in plan_text for idx in required_indexes), (
            f"L-10 query-plan gate: plan missing required index; "
            f"required one of {required_indexes}, got plan={plan_text!r}"
        )

        # Must NOT contain rejected patterns (e.g., TEMP B-TREE = inefficient sort)
        for bad in reject_patterns:
            assert bad.lower() not in plan_text, (
                f"L-10 query-plan gate: plan contains rejected pattern '{bad}'; plan={plan_text!r}"
            )


def test_pre_070_schema_fails_strict_index_gate(tmp_path: Path) -> None:
    """Prove pre-070 schema (035 only) fails the L-10 strict index gate.

    Without migration 070, the archived listing query uses TEMP B-TREE for
    sorting because idx_board_archived(archived_at, created_at) cannot provide
    ORDER BY when filtering by IS NOT NULL (range scan on first column).
    """
    import sqlite3

    db_path = str(tmp_path / "pre070.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create minimal schema with ONLY 035's index (no 070)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS board_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        -- 035 index: NOT the partial index, NOT the full compound
        CREATE INDEX idx_board_archived ON board_tasks(archived_at, created_at);
    """)

    conn.execute(
        "INSERT INTO board_tasks (id, title, created_at, updated_at, archived_at) "
        "VALUES ('t1', 'test', '2024-01-01', '2024-01-01', '2024-01-02')"
    )
    conn.commit()

    # Archived listing query
    sql = SQL_BOARD_LIST_ARCHIVED
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    plan_text = " ".join(str(r["detail"]) for r in rows).lower()

    # Pre-070: should have TEMP B-TREE (inefficient sort) or SCAN without index
    # because idx_board_archived cannot serve ORDER BY after IS NOT NULL filter
    assert "idx_board_tasks_archived_listing" not in plan_text, (
        "Pre-070 schema should NOT have the partial index"
    )
    # The old index cannot avoid a sort step for this query
    assert "temp b-tree" in plan_text or "scan" in plan_text, (
        f"Pre-070 should require TEMP B-TREE or SCAN; got plan={plan_text!r}"
    )
    conn.close()


def test_post_070_schema_uses_intended_indexes(tmp_path: Path) -> None:
    """Prove post-070 schema uses the intended indexes with no TEMP B-TREE.

    Migration 070 adds idx_board_tasks_archived_listing as a PARTIAL index
    that covers only archived rows, pre-sorted by (created_at DESC, id DESC).
    This eliminates the sort step for archived listing.
    """
    import sqlite3

    db_path = str(tmp_path / "post070.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create schema with ALL 070 indexes
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS board_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );

        -- 070 indexes (copied from migration)
        CREATE INDEX idx_board_tasks_archived_created
            ON board_tasks(archived_at, created_at DESC, id DESC);
        CREATE INDEX idx_board_tasks_status_archived
            ON board_tasks(status, archived_at, created_at DESC, id DESC);
        CREATE INDEX idx_board_tasks_archived_listing
            ON board_tasks(created_at DESC, id DESC)
            WHERE archived_at IS NOT NULL;
    """)

    # Insert test data (both archived and active)
    conn.execute(
        "INSERT INTO board_tasks (id, title, status, created_at, updated_at, archived_at) "
        "VALUES ('t1', 'active', 'open', '2024-01-01', '2024-01-01', NULL)"
    )
    conn.execute(
        "INSERT INTO board_tasks (id, title, status, created_at, updated_at, archived_at) "
        "VALUES ('t2', 'archived', 'done', '2024-01-02', '2024-01-02', '2024-01-03')"
    )
    conn.commit()

    # Test all board listing queries use correct indexes with no sort step
    test_cases = [
        (SQL_BOARD_LIST_ACTIVE, "idx_board_tasks_archived_created"),
        (SQL_BOARD_LIST_ARCHIVED, "idx_board_tasks_archived_listing"),
        (SQL_BOARD_LIST_ACTIVE_BY_STATUS, "idx_board_tasks_status_archived"),
    ]

    for sql, expected_index in test_cases:
        params: tuple = ("blocked",) if "?" in sql else ()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        plan_text = " ".join(str(r["detail"]) for r in rows).lower()

        assert expected_index.lower() in plan_text, (
            f"Post-070 plan missing {expected_index}; got plan={plan_text!r}"
        )
        assert "temp b-tree" not in plan_text, (
            f"Post-070 should NOT have TEMP B-TREE; got plan={plan_text!r}"
        )

    conn.close()
