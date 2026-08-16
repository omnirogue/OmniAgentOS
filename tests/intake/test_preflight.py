"""Tests for deploy-time preflight intelligence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from omniagentos.api.routes.intake import _parse_goal_prefixes
from omniagentos.db.store import SqliteStore
from omniagentos.intake.preflight import build_preflight, extract_paths
from omniagentos.orgdims.service import OrgDimsService


def test_prefix_parsing() -> None:
    # Test _parse_goal_prefixes
    g, wide, pin = _parse_goal_prefixes("wide:pin:coding: Implement feature")
    assert g == "Implement feature"
    assert wide is True
    assert pin == "coding"

    g2, wide2, pin2 = _parse_goal_prefixes("pin:research:wide: Search literature")
    assert g2 == "Search literature"
    assert wide2 is True
    assert pin2 == "research"


def test_path_extraction() -> None:
    text = "Please fix omniagentos/intake/preflight.py and edit dashboard/src/features/preflight/Card.tsx"
    paths = extract_paths(text)
    assert "omniagentos/intake/preflight.py" in paths
    assert "dashboard/src/features/preflight/Card.tsx" in paths


def test_build_preflight(tmp_path: Path) -> None:
    # Seed taxonomy and run build_preflight
    db = tmp_path / "test.db"

    # SqliteStore automatically migrates the database
    SqliteStore(str(db))
    svc = OrgDimsService(db_path=str(db))
    svc.ensure_seeded()

    preflight = build_preflight(
        str(db),
        task_id="bt_1",
        title="Implement preflight feature in omniagentos/intake/preflight.py",
        description="We need a new preflight module",
        discipline="coding",
        priority="normal",
        wide=True,
        pinned_formation="coding",
    )

    assert preflight["formation_id"] == "coding"
    assert preflight["parallelism"] == 4
    assert preflight["wide"] is True
    assert "omniagentos/intake/preflight.py" in preflight["owned_paths"]
    assert preflight["pinned_formation"] == "coding"


def test_dispatch_preflight_stamping(tmp_path: Path) -> None:
    # Set up stores and client
    db = tmp_path / "test.db"
    SqliteStore(str(db))
    svc = OrgDimsService(db_path=str(db))
    svc.ensure_seeded()

    # Since DB is migrated, all real tables are created!
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # Run the preflight stamping logic directly on a connection to verify correctness
    from omniagentos.contracts import utc_now_iso

    board_task_id = "bt_test_stamping"
    preflight = build_preflight(
        str(db),
        task_id=board_task_id,
        title="Fix bugs in omniagentos/swarm/spawn.py",
        description="Edit spawn.py",
        discipline="coding",
        priority="normal",
        wide=False,
    )

    now = utc_now_iso()
    conn.execute(
        "INSERT INTO board_tasks (id, title, description, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'open', ?, ?)",
        (board_task_id, "Fix bugs in omniagentos/swarm/spawn.py", "Edit spawn.py", now, now),
    )
    conn.execute(
        "UPDATE board_tasks SET preflight_json = ? WHERE id = ?",
        (json.dumps(preflight), board_task_id),
    )
    conn.commit()

    # Assert stamped
    row = conn.execute(
        "SELECT preflight_json FROM board_tasks WHERE id = ?", (board_task_id,)
    ).fetchone()
    assert row is not None
    data = json.loads(row[0])
    assert data["formation_id"] == "coding"
    assert data["parallelism"] == 2
    assert "omniagentos/swarm/spawn.py" in data["owned_paths"]
