from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.migrate import _iter_sql_statements
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.store import RoutinesStore
from tests.support.db_template import make_store

# Lane-local Migration F (unnumbered). Applied in fixtures so store/API tests
# see scope/purpose without writing a numbered file under omniagentos/db/migrations.
_STAGING_META = (
    Path(__file__).resolve().parents[2] / "migrations-staging" / "routines_meta.sql"
)


def apply_routines_meta_migration(store: SqliteStore) -> None:
    """Apply Migration F SQL onto an already-migrated SqliteStore."""
    script = _STAGING_META.read_text(encoding="utf-8")
    with store._lock:
        connection = store._connection
        for statement in _iter_sql_statements(script):
            # ADD COLUMN is not idempotent; ignore duplicate-column on re-apply.
            try:
                connection.execute(statement)
            except Exception as exc:  # noqa: BLE001 — sqlite3.OperationalError variant
                msg = str(exc).lower()
                if "duplicate column" in msg:
                    continue
                raise
        connection.commit()


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "routines.db")
    apply_routines_meta_migration(store)
    return store


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


def valid_routine_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal routine payload that satisfies every required-field rule:
    trigger, task template, objective gate, hard stop-condition, notification
    target. Tests mutate/omit fields from this to probe validation.

    Includes a real (non-mock) harness so D5 activation can resolve it; gate
    command is ``git diff --check`` (fast, allowlisted, exits 0 on a clean tree).
    """
    payload: dict[str, Any] = {
        "name": "nightly-lint-fix",
        "description": "Run the linter and auto-fix on a schedule.",
        "trigger_type": "cron",
        "trigger_config": {"cron": "0 3 * * *"},
        "task_template": {
            "discipline_id": "eng",
            "title": "Fix lint errors",
            "harness": "cli-grok",
        },
        "gate_type": "exit_code",
        "gate_config": {"command": "git diff --check", "expected_exit_code": 0},
        "hard_cap_type": "max_iterations",
        "hard_cap_value": 5,
        "notification_target": {"channel": "slack"},
    }
    payload.update(overrides)
    return payload


def draft_routine_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal disabled draft — engine fields omitted (LOOPS1-E2)."""
    payload: dict[str, Any] = {
        "name": "draft-loop",
        "status": "disabled",
    }
    payload.update(overrides)
    return payload
