from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.pulse.store import PulseStore
from tests.support.db_template import make_store


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "pulse.db")
    # Migration 032 (skill library) seeds six proof-of-concept skills and their
    # skill_versions into EVERY migrated database. The aggregator tests pin
    # exact counts against what they seed themselves, so clear that ambient
    # seed: "freshly-migrated" here means an empty skills library.
    store._connection.execute("DELETE FROM skill_versions")
    store._connection.execute("DELETE FROM skills")
    store._connection.commit()
    return store


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """The X-Session-Token header the ``_authorized``-gated routes require.

    TOKEN_PATH is redirected to tmp_path so the real var/secrets file is
    never read or created (same pattern as tests/api/test_system_routes.py).
    """
    from omniagentos.sessions import token

    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def pulse(database: SqliteStore) -> PulseStore:
    return PulseStore(database)


def seed_skills(database: SqliteStore, n: int = 3, *, offset: int = 0) -> None:
    """Insert ``n`` active skills rows so the aggregator has live counts.

    ``offset`` shifts the id/slug so repeated calls don't PK-collide.
    """
    from omniagentos.contracts import utc_now_iso

    now = utc_now_iso()
    for i in range(n):
        idx = offset + i
        database._connection.execute(
            "INSERT INTO skills (id, slug, category, subcategory, title, summary, "
            "status, current_version, created_at, updated_at) "
            "VALUES (?, ?, 'cat', 'sub', ?, '', 'active', 1, ?, ?)",
            (f"sk_{idx}", f"skill-{idx}", f"Skill {idx}", now, now),
        )
    database._connection.commit()


def seed_improvements(database: SqliteStore, statuses: list[str] | None = None) -> None:
    """Insert one improvement per status so the aggregator counts terminal-good rows."""
    from omniagentos.contracts import utc_now_iso

    now = utc_now_iso()
    for idx, status in enumerate(statuses or ["applied", "monitoring", "proposed", "rejected"]):
        database._connection.execute(
            "INSERT INTO improvements (id, origin, kind, title, status, risk_level, "
            "created_at, updated_at) VALUES (?, 'realtime', 'fix', ?, ?, 2, ?, ?)",
            (f"imp_{idx}", f"imp {idx}", status, now, now),
        )
    database._connection.commit()


def seed_routine_runs(database: SqliteStore, *, today_count: int = 2, accepted: int = 1) -> None:
    """Insert routine + runs so loops.fires and loops.acceptance have values."""
    from omniagentos.contracts import utc_now_iso

    now = utc_now_iso()
    database._connection.execute(
        "INSERT INTO routines (id, name, description, trigger_type, trigger_config_json, "
        "task_template_json, gate_type, gate_config_json, hard_cap_type, hard_cap_value, "
        "notification_target_json, status, created_at, updated_at) "
        "VALUES (?, 'r', '', 'cron', '{}', '{}', 'exit_code', '{}', "
        "'max_iterations', 5, '{}', 'active', ?, ?)",
        ("rtn_seed", now, now),
    )
    for i in range(today_count):
        database._connection.execute(
            "INSERT INTO routine_runs (routine_id, iteration, accepted, cost_usd, "
            "created_at) VALUES (?, ?, ?, 0.0, ?)",
            ("rtn_seed", i + 1, 1 if i < accepted else 0, now),
        )
    database._connection.commit()


def seed_reliability(database: SqliteStore, *, open_critical: int = 1, open_other: int = 3) -> None:
    """Insert reliability events so reliability.score has a non-1.0 value."""
    from omniagentos.contracts import new_id, utc_now_iso

    now = utc_now_iso()
    for i in range(open_critical):
        database._connection.execute(
            "INSERT INTO reliability_events (id, failure_class, severity, signature, "
            "occurrence_key, source, status, detected_at, updated_at) "
            "VALUES (?, 'test', 'critical', ?, ?, 'unit', 'open', ?, ?)",
            (new_id("rle"), f"sig_crit_{i}", f"occ_crit_{i}", now, now),
        )
    for i in range(open_other):
        database._connection.execute(
            "INSERT INTO reliability_events (id, failure_class, severity, signature, "
            "occurrence_key, source, status, detected_at, updated_at) "
            "VALUES (?, 'test', 'warning', ?, ?, 'unit', 'open', ?, ?)",
            (new_id("rle"), f"sig_warn_{i}", f"occ_warn_{i}", now, now),
        )
    database._connection.commit()


def seed_board_tasks(database: SqliteStore, *, done: int = 0, archived: int = 0) -> None:
    """Insert board_tasks in terminal states for delta counts."""
    from omniagentos.contracts import new_id, utc_now_iso

    now = utc_now_iso()
    for i in range(done):
        database._connection.execute(
            "INSERT INTO board_tasks (id, title, description, priority, status, "
            "claim_version, created_at, updated_at) "
            "VALUES (?, ?, '', 'normal', 'done', 0, ?, ?)",
            (new_id("btk"), f"Task {i}", now, now),
        )
    for i in range(archived):
        database._connection.execute(
            "INSERT INTO board_tasks (id, title, description, priority, status, "
            "claim_version, created_at, updated_at) "
            "VALUES (?, ?, '', 'normal', 'archived', 0, ?, ?)",
            (new_id("btk"), f"Archive {i}", now, now),
        )
    database._connection.commit()


def seed_chats(database: SqliteStore, *, active: int = 0) -> None:
    """Insert chats rows for delta counts."""
    from omniagentos.contracts import new_id, utc_now_iso

    now = utc_now_iso()
    for i in range(active):
        btk_id = new_id("btk")
        database._connection.execute(
            "INSERT INTO board_tasks (id, title, description, priority, status, "
            "claim_version, created_at, updated_at, origin) "
            "VALUES (?, ?, '', 'normal', 'open', 0, ?, ?, 'chat')",
            (btk_id, f"Chat companion {i}", now, now),
        )
        database._connection.execute(
            "INSERT INTO chats (id, board_task_id, title, status, meta_json, "
            "created_at, updated_at) VALUES (?, ?, ?, 'active', '{}', ?, ?)",
            (new_id("cht"), btk_id, f"Chat {i}", now, now),
        )
    database._connection.commit()
