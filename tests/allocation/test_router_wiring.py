from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from omniagentos.allocation.decisions import record_decision
from omniagentos.swarm.planner import build_plan


def _raw_task(
    task_id: str,
    deps: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    agent: int = 10,
    manual: int = 30,
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": list(paths),
        "est_agent_minutes": agent,
        "est_manual_minutes": manual,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }
    base.update(overrides)
    return base


def test_router_wiring_off(monkeypatch):
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "off")

    captured = []

    def sink(payload: Mapping[str, Any]) -> None:
        captured.append(payload)

    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", paths=("src/b.py",)),
        _raw_task("c", paths=("src/c.py",)),
        _raw_task("d", paths=("src/d.py",)),
    ]

    # Flag off (unset) vs. explicitly "off"
    # Same goal for both, or the dumps differ on `goal` alone and the
    # comparison proves nothing about the router.
    goal = "test flag off equivalence"

    monkeypatch.delenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", raising=False)
    plan_unset = build_plan(goal, tasks, decision_sink=sink)
    assert len(captured) == 0

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "off")
    plan_off = build_plan(goal, tasks, decision_sink=sink)
    assert len(captured) == 0

    # Ensure plans are byte-identical and hash is unchanged
    assert plan_unset.model_dump(mode="json") == plan_off.model_dump(mode="json")


def test_router_wiring_shadow(monkeypatch):
    # shadow mode: sink is called, applied is False, resulting plan is equal to off plan
    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", paths=("src/b.py",)),
        _raw_task("c", paths=("src/c.py",)),
        _raw_task("d", paths=("src/d.py",)),
    ]

    # Get the plan when flag is off
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "off")
    plan_off = build_plan("shadow goal", tasks)

    # Now run in shadow mode
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "shadow")
    captured = []

    def sink(payload: Mapping[str, Any]) -> None:
        captured.append(payload)

    plan_shadow = build_plan("shadow goal", tasks, decision_sink=sink)
    assert len(captured) == 1
    payload = captured[0]
    assert payload["applied"] is False
    assert payload["brief"] == "shadow goal"
    assert "route" in payload
    assert "topology" in payload

    # Plan should be identical to plan_off
    assert plan_shadow.model_dump(mode="json") == plan_off.model_dump(mode="json")


def test_router_wiring_enforce(monkeypatch):
    # enforce mode: sink is called, applied is True
    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", paths=("src/b.py",)),
        _raw_task("c", paths=("src/c.py",)),
        _raw_task("d", paths=("src/d.py",)),
    ]

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "enforce")
    captured = []

    def sink(payload: Mapping[str, Any]) -> None:
        captured.append(payload)

    plan_enforce = build_plan("enforce goal", tasks, decision_sink=sink)
    assert len(captured) == 1
    payload = captured[0]
    assert payload["applied"] is True

    # Check that enforce applied route decision note to assumptions/notes
    assert any(
        "router: applied task-shape route decision" in note for note in plan_enforce.assumptions
    )


def test_router_wiring_enforce_sequential_never_increases_target_n(monkeypatch):
    # enforce on a sequential-shaped plan never increases target_n
    # A sequential-shaped plan has tasks that are highly dependent (ratio is low / solo)
    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", deps=("a",), paths=("src/b.py",)),
        _raw_task("c", deps=("b",), paths=("src/c.py",)),
    ]

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "enforce")
    captured = []

    def sink(payload: Mapping[str, Any]) -> None:
        captured.append(payload)

    plan = build_plan("sequential goal", tasks, decision_sink=sink)
    # Since it is solo / sequential shaped, target_n must remain 1
    assert plan.target_n == 1
    if captured:
        assert captured[0]["worker_count"] == 1


def test_router_sink_exception_safety(monkeypatch):
    # a sink that raises must not break planning
    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", paths=("src/b.py",)),
        _raw_task("c", paths=("src/c.py",)),
        _raw_task("d", paths=("src/d.py",)),
    ]

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "enforce")

    def bad_sink(payload: Mapping[str, Any]) -> None:
        raise ValueError("simulated sink failure")

    # This should not raise and successfully return the plan
    plan = build_plan("enforce goal", tasks, decision_sink=bad_sink)
    assert plan is not None


def test_record_decision_from_sink_payload(monkeypatch):
    # Proves the keys in the payload produced by the router block
    # align perfectly with the task_shape_decisions table.
    tasks = [
        _raw_task("a", paths=("src/a.py",)),
        _raw_task("b", paths=("src/b.py",)),
        _raw_task("c", paths=("src/c.py",)),
        _raw_task("d", paths=("src/d.py",)),
    ]

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "enforce")
    captured = []

    def sink(payload: Mapping[str, Any]) -> None:
        captured.append(payload)

    build_plan("db record goal", tasks, decision_sink=sink)
    assert len(captured) == 1
    payload = captured[0]

    # Initialize sqlite memory db with schema
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    migration_path = root / "omniagentos" / "db" / "migrations" / "079_task_shape_decisions.sql"
    conn = sqlite3.connect(":memory:")
    conn.executescript(migration_path.read_text(encoding="utf-8"))

    # Record decision
    row_id = record_decision(conn, **payload)
    assert row_id is not None

    # Query and verify the record exists and fields match
    row = conn.execute(
        "SELECT id, brief_hash, route, topology, worker_count, applied FROM task_shape_decisions"
    ).fetchone()
    assert row is not None
    assert row[0] == row_id
    assert row[2] == payload["route"]
    assert row[3] == payload["topology"]
    assert row[4] == payload["worker_count"]
    assert row[5] == 1  # applied (True)
