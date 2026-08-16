"""AC-6 / IF-1: sessions table migration is additive."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.db import migrate as migrate_module
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore


def test_migration_010_creates_sessions_table(tmp_path: Path) -> None:
    db = tmp_path / "copy.db"
    version = migrate(str(db))
    assert version >= 10

    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert {
            "id",
            "source",
            "project_dir",
            "provider",
            "session_ref",
            "state",
            "pid",
            "model",
            "title",
            "budget_usd_max",
            "cost_usd",
            "kill_requested",
            "last_activity_at",
            "created_at",
            "updated_at",
            "granted_roots",
            "company_override",
            "agent_name",
            "agent_status",
            "agent_profile",
            "agent_session_id",
        } <= cols
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sessions'"
            ).fetchall()
        }
        assert "idx_sessions_state" in indexes
        assert "idx_sessions_source" in indexes


def test_migration_is_additive_on_existing_store(tmp_path: Path) -> None:
    """Applying on a DB that already has approvals.session_id does not destroy data."""
    db = tmp_path / "existing.db"
    store = SqliteStore(str(db))
    from omniagentos.contracts import new_id, utc_now_iso

    now = utc_now_iso()
    disc_id = "test-disc"
    store.create_discipline(
        {
            "id": disc_id,
            "name": "Test",
            "metric_contract": "{}",
            "status": "active",
            "created_at": now,
        }
    )
    task_id = new_id("tsk")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": disc_id,
            "title": "t",
            "input_json": "{}",
            "acceptance_json": "{}",
            "state": "draft",
            "risk": "low",
            "deadline": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    store.create_approval(
        {
            "id": new_id("apr"),
            "run_id": None,
            "task_id": task_id,
            "step_seq": None,
            "action_class": "consequential",
            "proposed_action": "x",
            "params_json": "{}",
            "risk": "high",
            "evidence": "",
            "state": "pending",
            "expires_at": None,
            "created_at": now,
            "session_id": None,
        }
    )
    # SessionsDal construction re-runs migrations including 010.
    from omniagentos.sessions.dal import SessionsDal

    dal = SessionsDal(str(db))
    assert dal.list_sessions() == []
    remaining = store.list_approvals(limit=10)
    assert len(remaining) == 1
    dal.close()


def test_migration_121_marks_preexisting_pending_approvals_unattempted(
    tmp_path: Path, monkeypatch
) -> None:
    """An absent pre-121 carrier must read as never-delivered, never delivered."""
    db = tmp_path / "pre121.db"
    files = migrate_module._scan_migration_files()  # noqa: SLF001 - migration boundary fixture
    pre_121 = [(version, path) for version, path in files if version <= 120]
    monkeypatch.setattr(migrate_module, "_migration_files", lambda: pre_121)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        migrate_module.migrate_connection(conn)
        conn.execute(
            "INSERT INTO approvals (id, session_id, action_class, proposed_action, "
            "params_json, risk, evidence, state, created_at) VALUES "
            "('apr_pre121', 'ses_pre121', 'consequential', 'risky', '{}', 'high', '', "
            "'pending', '2026-08-01T00:00:00Z')"
        )
        conn.commit()

        monkeypatch.setattr(migrate_module, "_migration_files", lambda: files)
        migrate_module.migrate_connection(conn)
        row = conn.execute(
            "SELECT delivery_state, delivered_at FROM approvals WHERE id = 'apr_pre121'"
        ).fetchone()
        assert row is not None
        assert (row["delivery_state"], row["delivered_at"]) == ("unattempted", None)
        indexes = {entry["name"] for entry in conn.execute("PRAGMA index_list(approvals)")}
        assert "idx_approvals_session_delivery" in indexes
