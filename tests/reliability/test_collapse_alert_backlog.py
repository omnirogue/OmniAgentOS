"""Tests for the pass-4 generalized-fingerprint detox in
``scripts/reliability/collapse-alert-backlog.py``.

The script's filename is not a valid module name (it has a hyphen), so it is
loaded by path, the same idiom ``tests/scripts/test_health_sentinel.py`` uses
for its own standalone script under test.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.db.migrate import migrate
from omniagentos.reflection.watchdog import _condition_key

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "reliability" / "collapse-alert-backlog.py"
)


def _load() -> Any:
    name = "collapse_alert_backlog_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CAB = _load()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    db_file = tmp_path / "mock_state.sqlite3"
    migrate(str(db_file))
    return str(db_file)


def _condition_description(reason: str, occurrences: int, first_seen: str, last_seen: str) -> str:
    key = _condition_key(reason)
    return (
        f"Reason for failure: {reason}\n\n"
        f"Occurrences: {occurrences} (first: {first_seen}, last: {last_seen})\n\n"
        f"Condition: {key}\n\n"
        "Please investigate the logs."
    )


def _make_task(
    *,
    task_id: str,
    title: str,
    description: str = "",
    discipline: str | None = "reflection",
    created_at: str,
    status: BoardTaskStatus = BoardTaskStatus.OPEN,
    blocked_reason: str = "",
    lane: str | None = None,
    owner_employee_id: str | None = None,
    claimed_by: str | None = None,
) -> BoardTask:
    return BoardTask(
        id=task_id,
        title=title,
        description=description,
        discipline=discipline,
        status=status,
        created_at=created_at,
        updated_at=created_at,
        blocked_reason=blocked_reason,
        owner_employee_id=owner_employee_id,
        claimed_by=claimed_by,
    )


def _set_lane(db_path: str, task_id: str, lane: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE board_tasks SET lane = ? WHERE id = ?", (lane, task_id))
        connection.commit()
    finally:
        connection.close()


def test_dup_mass_collapses_to_one_survivor_with_occurrence_rollup(db: str) -> None:
    store = CollabStore(db)
    reason = "Last reflection run status is 'running' since 2026-08-01T00:00:00Z"
    timestamps = [
        "2026-08-01T00:00:00Z",
        "2026-08-01T01:00:00Z",
        "2026-08-01T02:00:00Z",
    ]
    ids = ["btk_r1", "btk_r2", "btk_r3"]
    for task_id, ts in zip(ids, timestamps, strict=True):
        store.create_board_task(
            _make_task(
                task_id=task_id,
                title="reflection loop broken: Last reflection run status is 'running'...",
                description=_condition_description(reason, 1, ts, ts),
                discipline="reflection",
                created_at=ts,
            )
        )

    receipt_path = str(Path(db).parent / "receipt.json")
    written = CAB.collapse(db, apply=True, receipt_path=receipt_path)
    assert written == 2  # the two older duplicates; the survivor is not "written away"

    survivor = store.get_board_task("btk_r3")
    assert survivor is not None
    assert survivor["status"] == "open"
    assert "Collapsed occurrences: 3 (first: 2026-08-01T00:00:00Z, last: 2026-08-01T02:00:00Z)" in (
        survivor["description"] or ""
    )

    for older_id in ("btk_r1", "btk_r2"):
        row = store.get_board_task(older_id)
        assert row is not None
        assert row["status"] == "cancelled"

    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    assert receipt["collapsed_row_ids"]["btk_r1"] == "btk_r3"
    assert receipt["collapsed_row_ids"]["btk_r2"] == "btk_r3"
    assert receipt["collapsed_groups"]
    group = receipt["collapsed_groups"][0]
    assert group["survivor_id"] == "btk_r3"
    assert sorted(group["collapsed_ids"]) == ["btk_r1", "btk_r2"]
    assert group["occurrences"] == 3


def test_dry_run_makes_no_writes(db: str) -> None:
    store = CollabStore(db)
    reason = "Last reflection run status is 'running' since 2026-08-01T00:00:00Z"
    for task_id, ts in (
        ("btk_d1", "2026-08-01T00:00:00Z"),
        ("btk_d2", "2026-08-01T01:00:00Z"),
    ):
        store.create_board_task(
            _make_task(
                task_id=task_id,
                title="reflection loop broken: dup",
                description=_condition_description(reason, 1, ts, ts),
                discipline="reflection",
                created_at=ts,
            )
        )

    to_write = CAB.collapse(db, apply=False)
    assert to_write == 1

    for task_id in ("btk_d1", "btk_d2"):
        row = store.get_board_task(task_id)
        assert row is not None
        assert row["status"] == "open"


def test_non_reflection_same_title_different_discipline_untouched(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(
        _make_task(
            task_id="btk_x1",
            title="Companion task for chat: shared-title",
            discipline="billing",
            created_at="2026-08-01T00:00:00Z",
        )
    )
    store.create_board_task(
        _make_task(
            task_id="btk_x2",
            title="Companion task for chat: shared-title",
            discipline="ops",
            created_at="2026-08-01T01:00:00Z",
        )
    )

    CAB.collapse(db, apply=True)

    for task_id in ("btk_x1", "btk_x2"):
        row = store.get_board_task(task_id)
        assert row is not None
        assert row["status"] == "open"


def test_generalized_pass_collapses_same_discipline_non_reflection_duplicates(db: str) -> None:
    """The generalization point: NOT scoped to discipline='reflection' any more."""
    store = CollabStore(db)
    for task_id, ts in (
        ("btk_g1", "2026-08-01T00:00:00Z"),
        ("btk_g2", "2026-08-01T01:00:00Z"),
    ):
        store.create_board_task(
            _make_task(
                task_id=task_id,
                title="Disk usage critical on host-7",
                discipline="ops",
                created_at=ts,
            )
        )

    CAB.collapse(db, apply=True)

    assert store.get_board_task("btk_g1")["status"] == "cancelled"
    assert store.get_board_task("btk_g2")["status"] == "open"


def test_apply_is_idempotent(db: str) -> None:
    store = CollabStore(db)
    reason = "Last reflection run status is 'running' since 2026-08-01T00:00:00Z"
    for task_id, ts in (
        ("btk_i1", "2026-08-01T00:00:00Z"),
        ("btk_i2", "2026-08-01T01:00:00Z"),
    ):
        store.create_board_task(
            _make_task(
                task_id=task_id,
                title="reflection loop broken: dup",
                description=_condition_description(reason, 1, ts, ts),
                discipline="reflection",
                created_at=ts,
            )
        )

    receipt_path = str(Path(db).parent / "receipt.json")
    first = CAB.collapse(db, apply=True, receipt_path=receipt_path)
    assert first == 1

    second = CAB.collapse(db, apply=True, receipt_path=receipt_path)
    assert second == 0

    survivor = store.get_board_task("btk_i2")
    assert survivor is not None
    description = survivor["description"] or ""
    assert description.count("Collapsed occurrences:") == 1


def test_blocked_backfill_fills_unrouted_blocked_reason(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(
        _make_task(
            task_id="btk_b1",
            title="stuck card",
            discipline="ops",
            created_at="2026-08-01T00:00:00Z",
            status=BoardTaskStatus.BLOCKED,
            blocked_reason="",
        )
    )
    # Already routed (has a lane): must NOT be touched.
    store.create_board_task(
        _make_task(
            task_id="btk_b2",
            title="another stuck card",
            discipline="ops",
            created_at="2026-08-01T00:00:00Z",
            status=BoardTaskStatus.BLOCKED,
            blocked_reason="",
        )
    )
    _set_lane(db, "btk_b2", "fast")

    CAB.collapse(db, apply=True)

    routed = store.get_board_task("btk_b1")
    assert routed is not None
    assert routed["blocked_reason"] == CAB.BLOCKED_BACKFILL_REASON

    lane_owned = store.get_board_task("btk_b2")
    assert lane_owned is not None
    assert lane_owned["blocked_reason"] == ""


def test_receipt_shape(db: str) -> None:
    store = CollabStore(db)
    reason = "Last reflection run status is 'running' since 2026-08-01T00:00:00Z"
    for task_id, ts in (
        ("btk_s1", "2026-08-01T00:00:00Z"),
        ("btk_s2", "2026-08-01T01:00:00Z"),
    ):
        store.create_board_task(
            _make_task(
                task_id=task_id,
                title="reflection loop broken: dup",
                description=_condition_description(reason, 1, ts, ts),
                discipline="reflection",
                created_at=ts,
            )
        )
    store.create_board_task(
        _make_task(
            task_id="btk_s3",
            title="stuck card",
            discipline="ops",
            created_at="2026-08-01T00:00:00Z",
            status=BoardTaskStatus.BLOCKED,
            blocked_reason="",
        )
    )

    receipt_path = str(Path(db).parent / "receipt.json")
    CAB.collapse(db, apply=True, receipt_path=receipt_path)

    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    assert set(receipt) == {
        "applied_at",
        "db",
        "collapsed_groups",
        "collapsed_row_ids",
        "blocked_backfilled_ids",
        "blocked_backfill_reason",
    }
    assert receipt["blocked_backfilled_ids"] == ["btk_s3"]
    assert receipt["blocked_backfill_reason"] == CAB.BLOCKED_BACKFILL_REASON
    for group in receipt["collapsed_groups"]:
        assert {
            "fingerprint",
            "sample_title",
            "discipline",
            "survivor_id",
            "collapsed_ids",
            "occurrences",
            "first_seen",
            "last_seen",
        } <= set(group)
