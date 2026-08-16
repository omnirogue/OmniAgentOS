"""L-10 remaining hot indexes (migration 071) — strict production-SQL EXPLAIN gates.

S14A migration 070 covers portfolio/board listing indexes. This module covers the
remaining L14-owned L-10 examples from the synthesis:

* approvals by (run_id, step_seq)
* swarm_attempts by session
* idempotency by run

Assertions require the specific 071 index names. Table-name fallbacks are
forbidden (S14A review residual).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import (
    SQL_GET_APPROVAL_FOR,
    SQL_IDEM_FOR_RUN,
    SQL_SWARM_ATTEMPT_BY_SESSION,
    SqliteStore,
)
from tests.support.db_template import make_store


def _plan_text(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " ".join(str(row["detail"] if isinstance(row, sqlite3.Row) else row[-1]) for row in rows)


def _assert_uses_required_index(
    plan: str, required_indexes: tuple[str, ...], *, label: str
) -> None:
    plan_l = plan.lower()
    assert any(idx.lower() in plan_l for idx in required_indexes), (
        f"L-10 query-plan gate ({label}): plan missing required index; "
        f"required one of {required_indexes}, got plan={plan!r}"
    )
    # Explicit ban on vacuous table-name-only acceptance: if the only match would
    # have been a bare table scan token, fail. We require INDEX / USING in plan.
    assert "using index" in plan_l or "using covering index" in plan_l or "index" in plan_l, (
        f"L-10 query-plan gate ({label}): plan does not show index use; plan={plan!r}"
    )


def test_swarm_attempt_sql_matches_production_source() -> None:
    """Pin SQL_SWARM_ATTEMPT_BY_SESSION to the L10 call site without editing it."""
    source = (Path(__file__).resolve().parents[2] / "omniagentos" / "swarm" / "dal.py").read_text(
        encoding="utf-8"
    )
    assert "SELECT * FROM swarm_attempts WHERE session_id = ?" in source
    assert "ORDER BY started_at DESC, seq DESC LIMIT 1" in source
    assert "SELECT * FROM swarm_attempts WHERE session_id = ?" in SQL_SWARM_ATTEMPT_BY_SESSION
    assert "ORDER BY started_at DESC, seq DESC LIMIT 1" in SQL_SWARM_ATTEMPT_BY_SESSION


def test_hot_lookup_query_plans_use_071_indexes(tmp_path: Path) -> None:
    """Post-071 schema: production SQL uses the intended indexes (no table fallback)."""
    store = make_store(
        SqliteStore, str(tmp_path / "hot.db"), wal_checkpoint_interval_s=0
    )
    now = utc_now_iso()
    run_id = new_id("run")
    task_id = new_id("tsk")
    session_id = new_id("ses")
    board_id = new_id("bt")
    swarm_run_id = new_id("swr")

    store.create_task(
        {
            "id": task_id,
            "title": "hot",
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
            "trace_id": "trace-hot",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    store.create_approval(
        {
            "id": new_id("apr"),
            "run_id": run_id,
            "task_id": task_id,
            "step_seq": 0,
            "action_class": "sandboxed_creation",
            "proposed_action": "do",
            "params_json": "{}",
            "state": "pending",
            "created_at": now,
        }
    )
    assert store.idem_insert("idem-key-1", run_id, "step-a")

    conn = store._connection
    # Seed swarm rows so the partial session index is selectable.
    conn.execute(
        "INSERT INTO swarm_runs (id, status, created_at, updated_at, source) "
        "VALUES (?, 'running', ?, ?, 'test')",
        (swarm_run_id, now, now),
    )
    conn.execute(
        "INSERT INTO board_tasks (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (board_id, "swarm card", now, now),
    )
    conn.execute(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, started_at) "
        "VALUES (?, ?, ?, 0, ?, 'grok', 'test', ?)",
        (new_id("swa"), swarm_run_id, board_id, session_id, now),
    )
    conn.commit()

    cases: list[tuple[str, str, tuple, tuple[str, ...]]] = [
        (
            "approvals_run_step",
            SQL_GET_APPROVAL_FOR,
            (run_id, 0),
            ("idx_approvals_run_step",),
        ),
        (
            "idempotency_run",
            SQL_IDEM_FOR_RUN,
            (run_id,),
            ("idx_idempotency_run",),
        ),
        (
            "swarm_attempt_session",
            SQL_SWARM_ATTEMPT_BY_SESSION,
            (session_id,),
            ("idx_swarm_attempts_session",),
        ),
    ]

    for label, sql, params, required in cases:
        plan = _plan_text(conn, sql, params)
        _assert_uses_required_index(plan, required, label=label)
        # No vacuous table-name-only pass: required name must appear, not merely
        # the underlying table.
        plan_l = plan.lower()
        for idx in required:
            assert idx.lower() in plan_l, (
                f"L-10 strict gate: required index {idx!r} absent from plan={plan!r}"
            )

    # Store methods exercise the same SQL constants.
    assert store.get_approval_for(run_id, 0) is not None
    assert store.idem_for_run(run_id)
    store.close()


def test_pre_071_schema_fails_strict_index_gate(tmp_path: Path) -> None:
    """Prove pre-071 schema cannot satisfy the strict index-name gate."""
    db_path = tmp_path / "pre071.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            step_seq INTEGER,
            state TEXT,
            created_at TEXT
        );
        CREATE INDEX idx_approvals_state ON approvals(state);

        CREATE TABLE idempotency (
            key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE swarm_attempts (
            id TEXT PRIMARY KEY,
            swarm_run_id TEXT NOT NULL,
            board_task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            session_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            started_at TEXT NOT NULL
        );
        CREATE INDEX idx_swarm_attempts_run ON swarm_attempts(swarm_run_id);
        """
    )
    conn.execute(
        "INSERT INTO approvals(id, run_id, step_seq, state, created_at) "
        "VALUES ('a1', 'r1', 0, 'pending', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO idempotency(key, run_id, step_name, created_at) "
        "VALUES ('k1', 'r1', 's', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO swarm_attempts"
        "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, started_at) "
        "VALUES ('swa1', 'swr1', 'bt1', 0, 'ses1', 'grok', 'm', '2024-01-01')"
    )
    conn.commit()

    cases = [
        (SQL_GET_APPROVAL_FOR, ("r1", 0), "idx_approvals_run_step"),
        (SQL_IDEM_FOR_RUN, ("r1",), "idx_idempotency_run"),
        (SQL_SWARM_ATTEMPT_BY_SESSION, ("ses1",), "idx_swarm_attempts_session"),
    ]
    for sql, params, required in cases:
        plan = _plan_text(conn, sql, params)
        assert required.lower() not in plan.lower(), (
            f"Pre-071 schema must not already have {required}; plan={plan!r}"
        )
    conn.close()


def test_post_071_schema_uses_intended_indexes(tmp_path: Path) -> None:
    """Minimal post-071 schema: each production SQL uses its dedicated index."""
    db_path = tmp_path / "post071.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            step_seq INTEGER,
            state TEXT,
            created_at TEXT
        );
        CREATE INDEX idx_approvals_run_step ON approvals(run_id, step_seq);

        CREATE TABLE idempotency (
            key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX idx_idempotency_run
            ON idempotency(run_id, created_at ASC, key ASC);

        CREATE TABLE swarm_attempts (
            id TEXT PRIMARY KEY,
            swarm_run_id TEXT NOT NULL,
            board_task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            session_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            started_at TEXT NOT NULL
        );
        CREATE INDEX idx_swarm_attempts_session
            ON swarm_attempts(session_id, started_at DESC, seq DESC)
            WHERE session_id IS NOT NULL;
        """
    )
    conn.execute(
        "INSERT INTO approvals(id, run_id, step_seq, state, created_at) "
        "VALUES ('a1', 'r1', 0, 'pending', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO idempotency(key, run_id, step_name, created_at) "
        "VALUES ('k1', 'r1', 's', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO swarm_attempts"
        "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, started_at) "
        "VALUES ('swa1', 'swr1', 'bt1', 0, 'ses1', 'grok', 'm', '2024-01-01')"
    )
    conn.commit()

    expected = [
        (SQL_GET_APPROVAL_FOR, ("r1", 0), "idx_approvals_run_step"),
        (SQL_IDEM_FOR_RUN, ("r1",), "idx_idempotency_run"),
        (SQL_SWARM_ATTEMPT_BY_SESSION, ("ses1",), "idx_swarm_attempts_session"),
    ]
    for sql, params, index_name in expected:
        plan = _plan_text(conn, sql, params)
        assert index_name.lower() in plan.lower(), (
            f"Post-071 plan missing {index_name}; plan={plan!r}"
        )
        # Reject vacuous table-name-only: plan must name the index, not just SCAN table.
        assert "scan approvals" not in plan.lower() or index_name.lower() in plan.lower()
    conn.close()


def test_migrations_070_and_071_are_consolidated_in_order() -> None:
    """The S14A 070 and parent 071 migrations coexist without version reuse."""
    root = Path(__file__).resolve().parents[2]
    migration_root = root / "omniagentos" / "db" / "migrations"
    path_070 = migration_root / "070_portfolio_board_indexes.sql"
    assert path_070.is_file()
    text_070 = path_070.read_text(encoding="utf-8")
    assert "idx_board_tasks_archived_listing" in text_070

    path = migration_root / "071_hot_lookup_indexes.sql"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "idx_approvals_run_step" in text
    assert "idx_swarm_attempts_session" in text
    assert "idx_idempotency_run" in text

    numbered = sorted(p.name for p in migration_root.glob("[0-9][0-9][0-9]_*.sql"))
    assert numbered.index(path_070.name) < numbered.index(path.name)
