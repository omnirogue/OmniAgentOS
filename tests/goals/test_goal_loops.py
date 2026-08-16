from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore, sustained_consecutive


def test_goal_loop_migrations_apply_to_fresh_database(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    migrate(str(db_path))
    connection = sqlite3.connect(db_path)
    try:
        goal_columns = {row[1] for row in connection.execute("PRAGMA table_info(goals)")}
        reading_columns = {row[1] for row in connection.execute("PRAGMA table_info(goal_readings)")}
    finally:
        connection.close()

    assert {
        "parent_goal_id",
        "routine_id",
        "origin",
        "graduated_at",
        "blocked_reason",
    } <= goal_columns
    assert reading_columns == {"id", "goal_id", "cycle", "value", "met", "captured_at"}


def test_goal_tree_reads_children_and_grandchild(tmp_path: Path) -> None:
    store = StewardStore(SqliteStore(str(tmp_path / "tree.db")))
    root = store.upsert_goal({"name": "root", "north_star": {}})
    child_a = store.upsert_goal({"name": "child-a", "north_star": {}, "parent_goal_id": root["id"]})
    child_b = store.upsert_goal({"name": "child-b", "north_star": {}, "parent_goal_id": root["id"]})
    grandchild = store.upsert_goal(
        {"name": "grandchild", "north_star": {}, "parent_goal_id": child_a["id"]}
    )

    tree = store.goal_tree(root["id"])
    children = {node["goal"]["id"]: node for node in tree["children"]}
    assert set(children) == {child_a["id"], child_b["id"]}
    assert children[child_a["id"]]["children"][0]["goal"]["id"] == grandchild["id"]
    assert children[child_b["id"]]["children"] == []
    assert "truncated" not in children[child_b["id"]]  # a genuine leaf is unmarked


def test_goal_tree_marks_depth_and_cycle_truncation(tmp_path: Path) -> None:
    store = StewardStore(SqliteStore(str(tmp_path / "deep.db")))
    prev_id: str | None = None
    root_id: str | None = None
    for i in range(8):
        goal = store.upsert_goal(
            {"name": f"g{i}", "north_star": {}, **({"parent_goal_id": prev_id} if prev_id else {})}
        )
        prev_id = goal["id"]
        root_id = root_id or goal["id"]

    node = store.goal_tree(root_id)
    depth = 0
    while node["children"]:
        node = node["children"][0]
        depth += 1
    assert depth == store._GOAL_TREE_DEPTH_CAP
    assert node["truncated"] is True and node["truncated_reason"] == "depth_cap"

    a = store.upsert_goal({"name": "cyc-a", "north_star": {}})
    b = store.upsert_goal({"name": "cyc-b", "north_star": {}, "parent_goal_id": a["id"]})
    store.upsert_goal({"name": "cyc-a", "north_star": {}, "id": a["id"], "parent_goal_id": b["id"]})
    b_node = store.goal_tree(a["id"])["children"][0]
    assert b_node["children"] == [] and b_node["truncated"] is True
    assert b_node["truncated_reason"] == "cycle"


def test_goal_reading_write_preserves_absence(tmp_path: Path) -> None:
    store = StewardStore(SqliteStore(str(tmp_path / "readings.db")))
    goal = store.upsert_goal({"name": "reading-goal", "north_star": {}})
    reading_id = store.append_goal_reading(
        {"goal_id": goal["id"], "cycle": 1, "value": None, "met": 1}
    )

    series = store.goal_reading_series(goal["id"])
    assert series[0]["captured_at"]
    assert [
        {key: value for key, value in row.items() if key != "captured_at"} for row in series
    ] == [{"id": reading_id, "goal_id": goal["id"], "cycle": 1, "value": None, "met": 0}]


def test_append_goal_reading_write_seam_validation(tmp_path: Path) -> None:
    """F5/F7: refuse NaN (never absent-but-met), a "false" string met, a
    non-numeric value, and an unknown goal_id."""
    store = StewardStore(SqliteStore(str(tmp_path / "coerce.db")))
    goal = store.upsert_goal({"name": "coerce", "north_star": {}})

    with pytest.raises(ValueError):
        store.append_goal_reading(
            {"goal_id": goal["id"], "cycle": 1, "value": float("nan"), "met": 1}
        )
    with pytest.raises(ValueError):
        store.append_goal_reading({"goal_id": goal["id"], "cycle": 2, "value": 1.0, "met": "false"})
    with pytest.raises(ValueError):
        store.append_goal_reading({"goal_id": goal["id"], "cycle": 3, "value": "", "met": 1})
    with pytest.raises(KeyError):
        store.append_goal_reading(
            {"goal_id": "gl_does_not_exist", "cycle": 1, "value": 1.0, "met": 1}
        )

    assert store.goal_reading_series(goal["id"]) == []


def test_append_goal_reading_upserts_same_cycle(tmp_path: Path) -> None:
    """F3b: a retried write for one cycle must upsert, not duplicate."""
    store = StewardStore(SqliteStore(str(tmp_path / "retry.db")))
    goal = store.upsert_goal({"name": "retry-goal", "north_star": {}})
    for _ in range(3):
        store.append_goal_reading({"goal_id": goal["id"], "cycle": 5, "value": 1.0, "met": 1})

    series = store.goal_reading_series(goal["id"])
    assert len(series) == 1 and series[0]["cycle"] == 5


def test_sustained_consecutive() -> None:
    readings = [
        {"cycle": 1, "value": 1.0, "met": 1},
        {"cycle": 2, "value": 2.0, "met": 1},
        {"cycle": 3, "value": 3.0, "met": 1},
    ]
    assert sustained_consecutive(readings, 3, current_cycle=3) is True
    # r3 anchor: a streak that does not END at the controller's present cycle
    # is a STALE streak — a dark metric source must never graduate a goal.
    assert sustained_consecutive(readings, 3, current_cycle=9) is False
    readings[-2] = {"cycle": 2, "value": None, "met": 1}
    assert sustained_consecutive(readings, 3, current_cycle=3) is False

    gapped = [{"cycle": c, "value": 1.0, "met": 1} for c in (1, 7, 99)]  # F3a: gaps
    assert sustained_consecutive(gapped, 3, current_cycle=99) is False

    duped = [{"cycle": 5, "value": 1.0, "met": 1} for _ in range(3)]  # F3b: dupes
    assert sustained_consecutive(duped, 3, current_cycle=5) is False


def test_reading_cycle_must_be_an_int(tmp_path: Path) -> None:
    """r3: a TEXT cycle would store fine and TypeError the sustain predicate."""
    store = StewardStore(SqliteStore(str(tmp_path / "cyc.db")))
    goal = store.upsert_goal({"name": "cycle-check", "north_star": {}})
    with pytest.raises(ValueError):
        store.append_goal_reading({"goal_id": goal["id"], "cycle": "abc", "value": 1.0, "met": 1})
    with pytest.raises(ValueError):
        store.append_goal_reading({"goal_id": goal["id"], "cycle": True, "value": 1.0, "met": 1})


def test_upsert_status_cannot_ungraduate_and_explicit_none_clears(tmp_path: Path) -> None:
    """r3/r4: the seed's status-bearing shape must not un-graduate a goal,
    a non-'active' status write on a graduated goal must refuse LOUDLY (never
    a silent partial write), and a graduation-aware caller can clear."""
    from omniagentos.goals.seed import seed

    db = SqliteStore(str(tmp_path / "grad.db"))
    store = StewardStore(db)
    seed(db)
    goal = store.upsert_goal(
        {
            "name": "increase-revenue",
            "north_star": {},
            "status": "graduated",
            "graduated_at": "2026-08-14T00:00:00Z",
        }
    )
    # THE REAL SEED re-run (not a hand-approximation): both survive.
    seed(db)
    after_seed = store.get_goal(goal["id"])
    assert after_seed is not None
    assert after_seed["status"] == "graduated"
    assert after_seed["graduated_at"] == "2026-08-14T00:00:00Z"
    # r4 loud guard: any NON-'active' status without managing graduation
    # refuses — a swallowed operator write is worse than an exception. And a
    # refused call writes NOTHING (no partial sibling-field landing).
    with pytest.raises(ValueError):
        store.upsert_goal(
            {
                "name": "increase-revenue",
                "north_star": {},
                "status": "blocked",
                "blocked_reason": "vendor outage",
            }
        )
    unchanged = store.get_goal(goal["id"])
    assert unchanged is not None
    assert unchanged["blocked_reason"] == after_seed["blocked_reason"]
    # r4 mirror guards: stranding either half of the pair refuses.
    with pytest.raises(ValueError):
        store.upsert_goal({"name": "increase-revenue", "north_star": {}, "graduated_at": None})
    store.upsert_goal({"name": "fresh-goal", "north_star": {}})
    with pytest.raises(ValueError):
        store.upsert_goal({"name": "fresh-goal", "north_star": {}, "status": "graduated"})
    # Graduation-aware caller passes BOTH: explicit None clears, status applies.
    cleared = store.upsert_goal(
        {
            "name": "increase-revenue",
            "north_star": {},
            "status": "active",
            "graduated_at": None,
        }
    )
    assert cleared["status"] == "active"
    assert cleared["graduated_at"] is None
    assert goal["id"] == cleared["id"]


def test_goal_updatable_matches_goal_fields() -> None:
    """r4 parity pin: a column added to _GOAL_FIELDS but not _GOAL_UPDATABLE
    would become silently non-updatable and read as 'preserved'."""
    from omniagentos.steward.store import _GOAL_FIELDS, _GOAL_UPDATABLE

    assert set(_GOAL_UPDATABLE) == set(_GOAL_FIELDS) - {
        "id",
        "name",
        "created_at",
        "updated_at",
    }


def test_sustain_anchor_validation_is_state_independent() -> None:
    """r4: a malformed current_cycle raises on long AND short/empty series."""
    long_series = [{"cycle": c, "value": 1.0, "met": 1} for c in (1, 2, 3)]
    for bad in ("abc", None, True, object()):
        with pytest.raises(ValueError):
            sustained_consecutive(long_series, 3, current_cycle=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            sustained_consecutive([], 3, current_cycle=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            sustained_consecutive(long_series, 0, current_cycle=bad)  # type: ignore[arg-type]


def test_upsert_goal_preserves_new_columns_on_legacy_reseed(tmp_path: Path) -> None:
    """F2: an idempotent re-upsert by name (goals/seed.py's shape) must never
    erase graduated_at/parent_goal_id/routine_id/origin/blocked_reason."""
    store = StewardStore(SqliteStore(str(tmp_path / "wipe.db")))
    parent = store.upsert_goal({"name": "increase-revenue", "north_star": {}})
    child = store.upsert_goal(
        {
            "name": "child-of-revenue",
            "north_star": {},
            "parent_goal_id": parent["id"],
            "routine_id": "rt_1",
            "origin": "decomposition",
            "graduated_at": "2026-08-14T00:00:00Z",
            "blocked_reason": "waiting-on-external",
        }
    )

    after = store.upsert_goal({"name": "child-of-revenue", "north_star": {}, "priority": 100})

    for key in ("parent_goal_id", "routine_id", "origin", "graduated_at", "blocked_reason"):
        assert after[key] == child[key], key
