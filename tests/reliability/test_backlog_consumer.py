"""Tests for ``scripts/reliability/backlog-consumer.py`` (bounded backlog drain).

The script's filename is not a valid module name (it has a hyphen), so it is
loaded by path, the same idiom ``tests/scripts/test_health_sentinel.py`` uses
for its own standalone script under test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.db.migrate import migrate

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reliability" / "backlog-consumer.py"


def _load() -> Any:
    name = "backlog_consumer_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BC = _load()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    db_file = tmp_path / "mock_state.sqlite3"
    migrate(str(db_file))
    return str(db_file)


def _open_task(task_id: str, created_at: str, title: str = "drainable card") -> BoardTask:
    return BoardTask(
        id=task_id,
        title=title,
        discipline="ops",
        status=BoardTaskStatus.OPEN,
        created_at=created_at,
        updated_at=created_at,
    )


def test_default_executor_names_a_refusal_per_task(db: str) -> None:
    store = CollabStore(db)
    for index in range(3):
        store.create_board_task(_open_task(f"btk_c{index}", f"2026-08-01T0{index}:00:00Z"))

    receipts = BC.run(db, max_tasks=10, max_seconds=600.0)

    assert len(receipts) == 3
    for receipt in receipts:
        assert receipt["outcome"] == "refused"
        assert receipt["reason"] == BC.NO_EXECUTOR_REFUSAL
        assert receipt["task_id"].startswith("btk_c")


def test_max_tasks_bound_is_respected(db: str) -> None:
    store = CollabStore(db)
    for index in range(15):
        store.create_board_task(
            _open_task(f"btk_m{index:02d}", f"2026-08-01T{index % 24:02d}:00:00Z")
        )

    receipts = BC.run(db, max_tasks=10, max_seconds=600.0)

    assert len(receipts) <= 10
    assert len(receipts) == 10


def test_receipt_emitted_per_selection(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(_open_task("btk_r1", "2026-08-01T00:00:00Z"))
    store.create_board_task(_open_task("btk_r2", "2026-08-01T01:00:00Z"))

    receipts = BC.run(db, max_tasks=10, max_seconds=600.0)

    assert len(receipts) == 2
    for receipt in receipts:
        assert {"task_id", "title", "selected_at", "stage", "outcome", "reason"} <= set(receipt)
        assert receipt["outcome"] in ("merged", "refused")


def test_wall_clock_cap_names_a_refusal_without_running_the_executor(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(_open_task("btk_w1", "2026-08-01T00:00:00Z"))
    store.create_board_task(_open_task("btk_w2", "2026-08-01T01:00:00Z"))

    calls: list[str] = []

    def _tracking_executor(task: dict[str, Any]) -> dict[str, Any]:
        calls.append(task["id"])
        return {"stage": "merge", "outcome": "merged", "reason": None}

    receipts = BC.run(db, max_tasks=10, max_seconds=0.0, executor=_tracking_executor)

    assert len(receipts) == 2
    assert calls == []
    for receipt in receipts:
        assert receipt["outcome"] == "refused"
        assert receipt["reason"] == BC.WALL_CLOCK_REFUSAL


def test_injected_executor_can_end_in_merge(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(_open_task("btk_e1", "2026-08-01T00:00:00Z"))

    def _merging_executor(task: dict[str, Any]) -> dict[str, Any]:
        return {"stage": "merge", "outcome": "merged", "reason": None}

    receipts = BC.run(db, max_tasks=10, max_seconds=600.0, executor=_merging_executor)

    assert len(receipts) == 1
    assert receipts[0]["stage"] == "merge"
    assert receipts[0]["outcome"] == "merged"


def test_executor_error_is_a_named_refusal_not_a_crash(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(_open_task("btk_x1", "2026-08-01T00:00:00Z"))

    def _broken_executor(task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("backend unreachable")

    receipts = BC.run(db, max_tasks=10, max_seconds=600.0, executor=_broken_executor)

    assert len(receipts) == 1
    assert receipts[0]["outcome"] == "refused"
    assert "backend unreachable" in receipts[0]["reason"]


def test_zero_max_tasks_selects_nothing(db: str) -> None:
    store = CollabStore(db)
    store.create_board_task(_open_task("btk_z1", "2026-08-01T00:00:00Z"))

    receipts = BC.run(db, max_tasks=0, max_seconds=600.0)

    assert receipts == []
