"""Adversarial tests for the structurally restricted dry-run goal controller."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omniagentos_loops.instances import goal_controller
from omniagentos_loops.templates.measure_gap_act import DryRunController, meets_setpoint

from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore

_STORE_WRITERS = frozenset(
    {
        "ack_alert",
        "ack_briefing",
        "append_goal_reading",
        "claim_suggestion",
        "create_alert",
        "create_or_bump_suggestion",
        "create_suggestion",
        "decide_suggestion",
        "insert_briefing",
        "insert_comms_message",
        "insert_goal",
        "insert_metric_snapshot",
        "link_fact_to_goal",
        "record_suggestion_outcome",
        "release_suggestion",
        "resolve_alert",
        "set_message_kb",
        "upsert_briefing",
        "upsert_comms_source",
        "upsert_goal",
    }
)

_SETPOINT = {
    "metric_source": "mission_direct",
    "comparator": ">=",
    "target": 0.9,
    "sustain": {"periods": 3, "window": 60},
    "effort": {"max_cycles": 5},
}


def _steward(tmp_path: Path) -> StewardStore:
    return StewardStore(SqliteStore(str(tmp_path / "goal_controller.db")))


def _make_goal(steward: StewardStore, **overrides: Any) -> dict[str, Any]:
    supplied = overrides.get("target", {})
    target = {
        **_SETPOINT,
        **supplied,
        "sustain": {**_SETPOINT["sustain"], **supplied.get("sustain", {})},
        "effort": {**_SETPOINT["effort"], **supplied.get("effort", {})},
    }
    return steward.upsert_goal(
        {"name": overrides.get("name", "demo-goal"), "north_star": {}, "target": target}
    )


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_rows(steward: StewardStore, table: str) -> list[tuple[Any, ...]]:
    rows = steward._connection.execute(
        f"SELECT * FROM {_quote_identifier(table)} ORDER BY rowid"
    ).fetchall()
    values = [tuple(row) for row in rows]
    # goal_readings uses AUTOINCREMENT; its sqlite_sequence entry is metadata
    # for the one excepted table, while every other sequence remains guarded.
    if table == "sqlite_sequence":
        values = [row for row in values if row[0] != "goal_readings"]
    return values


def _full_schema_digests(steward: StewardStore) -> dict[str, str]:
    tables = {
        str(row[0])
        for row in steward._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "goal_readings" in tables
    return {
        table: hashlib.sha256(repr(_table_rows(steward, table)).encode()).hexdigest()
        for table in sorted(tables - {"goal_readings"})
    }


def _isolated(steward: StewardStore, invocation: Callable[[], Any]) -> Any:
    """Assert every public controller invocation leaves non-reading tables untouched."""
    before = _full_schema_digests(steward)
    try:
        return invocation()
    finally:
        assert _full_schema_digests(steward) == before


def test_facade_exposes_only_the_explicit_read_grant_and_one_write(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    facade = goal_controller._DryRunGoalStore(steward)
    assert facade.ALLOWED_ATTRIBUTES == {
        "_connection",
        "append_goal_reading",
        "get_goal",
        "goal_reading_series",
        "snapshot_series",
    }
    with pytest.raises(goal_controller.CapabilityError, match="__steward"):
        getattr(facade, "_DryRunGoalStore__steward")  # noqa: B009 - pins the mangling escape
    for method in _STORE_WRITERS - {"append_goal_reading"}:
        with pytest.raises(goal_controller.CapabilityError, match=method):
            getattr(facade, method)
    for statement in (
        "UPDATE goals SET status = 'paused'",
        "WITH candidate AS (SELECT id FROM goals) UPDATE goals SET status = 'paused'",
    ):
        with pytest.raises(goal_controller.CapabilityError, match="read-only SQL"):
            facade._connection.execute(statement)
    cursor = facade._connection.execute("SELECT 1")
    with pytest.raises(goal_controller.CapabilityError, match="read-only SQL"):
        cursor.executescript("UPDATE goals SET status = 'paused'")
    with pytest.raises(goal_controller.CapabilityError, match="does not expose"):
        cursor.connection.execute("UPDATE goals SET status = 'paused'")
    raw_connection = object.__getattribute__(facade, "_DryRunGoalStore__connection")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        raw_connection.execute("UPDATE goals SET status = 'paused'")
    db_path = Path(steward._store._db_path).resolve()
    raw_connection.execute(f"ATTACH DATABASE '{db_path}' AS w")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        raw_connection.execute("UPDATE w.goals SET status = 'paused'")


def test_register_grants_no_tools() -> None:
    ctx = SimpleNamespace()
    goal_controller.register(ctx)
    assert ctx.tools.names() == frozenset()


def test_register_preserves_a_preexisting_registry() -> None:
    from omniagentos_loops.tools import ToolRegistry

    sentinel = ToolRegistry()
    ctx = SimpleNamespace(tools=sentinel)
    goal_controller.register(ctx)
    assert ctx.tools is sentinel


def test_one_dry_run_cycle_writes_exactly_one_goal_readings_row(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1}, "effort": {"max_cycles": 1}},
    )
    other = _make_goal(steward, name="other-goal")
    steward.append_goal_reading({"goal_id": other["id"], "cycle": 0, "value": 0.5, "met": 0})
    before_schema = _full_schema_digests(steward)
    before_rows = _table_rows(steward, "goal_readings")

    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))

    after_rows = _table_rows(steward, "goal_readings")
    assert len(result.receipts) == 1
    assert len(after_rows) == len(before_rows) + 1
    assert after_rows[: len(before_rows)] == before_rows
    assert _full_schema_digests(steward) == before_schema


def test_public_entry_constructs_the_restricted_facade_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1}, "effort": {"max_cycles": 1}},
    )
    real_facade = goal_controller._DryRunGoalStore
    constructions: list[StewardStore] = []

    def construct(candidate: StewardStore) -> goal_controller._DryRunGoalStore:
        constructions.append(candidate)
        return real_facade(candidate)

    monkeypatch.setattr(goal_controller, "_DryRunGoalStore", construct)

    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))

    assert len(result.receipts) == 1
    assert constructions == [steward]


def test_full_schema_digest_detects_an_update_without_a_row_count_change(
    tmp_path: Path,
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(steward)
    before_count = steward._connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    before = _full_schema_digests(steward)
    steward.upsert_goal({"name": goal["name"], "north_star": {}, "description": "rewritten mutant"})
    after_count = steward._connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    assert after_count == before_count
    assert _full_schema_digests(steward) != before


def test_absence_records_null_value_and_met_zero(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1}, "effort": {"max_cycles": 1}},
    )
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    row = steward.goal_reading_series(goal["id"], last_n=1)[0]
    assert row["value"] is None and row["met"] == 0
    assert result.receipts[0].captured_at == row["captured_at"]
    assert result.receipts[0].captured_at.endswith("Z")


def test_meets_setpoint_never_treats_absence_as_met() -> None:
    assert meets_setpoint(None, ">=", 0.0) is False
    assert meets_setpoint(0.95, ">=", 0.9) is True
    assert meets_setpoint(0.5, "<=", 0.9) is True
    assert meets_setpoint(1.0, "==", 1.0) is True


def test_lifetime_cap_persists_across_public_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 2, "window": 60}, "effort": {"max_cycles": 3}},
    )
    calls = {"n": 0}

    def count_measure(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(goal_controller, "get_metric_value", count_measure)
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    results = [
        _isolated(
            steward,
            partial(
                goal_controller.run_dry_run_cycle,
                steward,
                goal,
                now=start + timedelta(seconds=offset * 60),
            ),
        )
        for offset in range(4)
    ]
    rows = steward.goal_reading_series(goal["id"], last_n=100)
    assert [row["cycle"] for row in rows] == [0, 1, 2]
    assert [len(result.receipts) for result in results] == [1, 1, 1, 0]
    assert results[2].escalation is not None
    assert results[3].escalation is not None
    assert calls["n"] == 3
    assert results[-1].escalation.cycles_run == 0
    assert "already exhausted" in results[-1].escalation.detail


def test_cap_exhaustion_uses_escalation_receipt_shape() -> None:
    calls = {"n": 0}

    def measure() -> float | None:
        calls["n"] += 1
        return None

    def record(*, cycle: int, value: float | None, met: bool, captured_at: str) -> dict[str, Any]:
        return {"cycle": cycle, "value": value, "met": met, "captured_at": captured_at}

    controller = DryRunController(
        goal_id="gl_test",
        comparator=">=",
        target=1.0,
        sustain_periods=2,
        max_cycles=2,
        measure=measure,
        record=record,
        evaluate=lambda *, current_cycle: False,
    )
    assert len(controller.run()) == 2
    assert controller.escalation is not None
    assert controller.escalation.as_dict()["escalated"] is True


def test_cap_exhaustion_stops_and_emits_escalation_receipt(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1, "window": 0}, "effort": {"max_cycles": 2}},
    )
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    assert len(result.receipts) == 2
    assert result.escalation is not None
    assert result.escalation.max_cycles == 2
    assert result.escalation.as_dict()["escalated"] is True


def test_anchor_streak_ending_at_this_tick_graduates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 3, "window": 60}, "effort": {"max_cycles": 5}},
    )
    monkeypatch.setattr(goal_controller, "get_metric_value", lambda *args, **kwargs: 0.95)
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    results = [
        _isolated(
            steward,
            partial(
                goal_controller.run_dry_run_cycle,
                steward,
                goal,
                now=start + timedelta(seconds=offset * 60),
            ),
        )
        for offset in range(3)
    ]
    receipts = [receipt for result in results for receipt in result.receipts]
    assert [receipt.cycle for receipt in receipts] == [0, 1, 2]
    assert receipts[-1].sustained is True
    refreshed = steward.get_goal(goal["id"])
    assert refreshed is not None and refreshed["status"] == "active"
    assert refreshed.get("graduated_at") is None


def test_public_entry_point_uses_stored_target_not_caller_target(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1, "window": 0}, "effort": {"max_cycles": 3}},
    )
    counterfeit = {
        **goal,
        "target": {
            "metric_source": "mission_direct",
            "comparator": ">=",
            "target": 0.0,
            "sustain": {"periods": 1},
            "effort": {"max_cycles": 1},
        },
    }
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, counterfeit))
    assert len(result.receipts) == 3
    assert result.escalation is not None and result.escalation.max_cycles == 3


def test_paused_goal_refuses_without_measuring_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(steward)
    steward.upsert_goal({"name": goal["name"], "north_star": {}, "status": "paused"})
    calls = {"n": 0}
    monkeypatch.setattr(
        goal_controller,
        "get_metric_value",
        lambda *args, **kwargs: calls.__setitem__("n", calls["n"] + 1),
    )
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    assert result.receipts == [] and result.refusal is not None
    assert result.refusal.reason == "inactive"
    assert calls["n"] == 0
    assert steward.goal_reading_series(goal["id"], last_n=10) == []


def test_missing_goal_refuses_without_mutating_any_goal(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    bystander = _make_goal(steward)
    steward.upsert_goal(
        {"name": bystander["name"], "north_star": {}, "description": "operator text"}
    )
    before_description = steward.get_goal(bystander["id"])["description"]

    result = _isolated(
        steward,
        lambda: goal_controller.run_dry_run_cycle(steward, {"id": "gl_missing"}),
    )

    assert result.receipts == []
    assert result.refusal is not None and result.refusal.reason == "missing"
    archived = steward._connection.execute(
        "SELECT COUNT(*) FROM goals WHERE status = 'archived'"
    ).fetchone()[0]
    assert archived == 0
    assert steward.get_goal(bystander["id"])["description"] == before_description


def test_direct_builder_refuses_paused_stored_goal(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(steward)
    steward.upsert_goal({"name": goal["name"], "north_star": {}, "status": "paused"})
    restricted = goal_controller._DryRunGoalStore(steward)

    with pytest.raises(ValueError, match="not active"):
        _isolated(
            steward,
            lambda: goal_controller.build_dry_run_controller(restricted, goal["id"]),
        )


def test_deleted_routine_fault_records_durable_broken_reading(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={
            "metric_source": "gate_pass_rate:renamed",
            "sustain": {"periods": 1},
            "effort": {"max_cycles": 1},
        },
    )
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    row = steward.goal_reading_series(goal["id"], last_n=1)[0]
    assert row["value"] is None and row["met"] == 0
    assert "instrument fault" in result.receipts[0].detail
    assert "KeyError" in result.receipts[0].detail


def test_non_finite_metric_records_fault_instead_of_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1}, "effort": {"max_cycles": 1}},
    )
    monkeypatch.setattr(goal_controller, "get_metric_value", lambda *args, **kwargs: math.inf)
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    assert steward.goal_reading_series(goal["id"], last_n=1)[0]["value"] is None
    assert "non-finite" in result.receipts[0].detail


def test_window_refuses_early_tick_and_spaced_readings_can_graduate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={
            "metric_source": "metric_snapshot:probe:rate",
            "sustain": {"periods": 3, "window": 60},
            "effort": {"max_cycles": 5},
        },
    )
    steward.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "probe",
            "metric": "rate",
            "value": 0.95,
            "unit": "",
            "window": "minute",
        }
    )
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    first = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal, now=start))
    assert len(first.receipts) == 1 and first.pacing is not None

    real_get_metric_value = goal_controller.get_metric_value
    calls = {"n": 0}

    def count_measure(*args: Any, **kwargs: Any) -> float | None:
        calls["n"] += 1
        return real_get_metric_value(*args, **kwargs)

    monkeypatch.setattr(goal_controller, "get_metric_value", count_measure)
    early = _isolated(
        steward,
        lambda: goal_controller.run_dry_run_cycle(steward, goal, now=start + timedelta(seconds=59)),
    )
    assert early.receipts == [] and early.pacing is not None
    assert early.pacing.as_dict()["pacing_refused"] is True
    assert calls["n"] == 0
    assert len(steward.goal_reading_series(goal["id"], last_n=10)) == 1

    second = _isolated(
        steward,
        lambda: goal_controller.run_dry_run_cycle(steward, goal, now=start + timedelta(seconds=60)),
    )
    third = _isolated(
        steward,
        lambda: goal_controller.run_dry_run_cycle(
            steward, goal, now=start + timedelta(seconds=120)
        ),
    )
    assert len(second.receipts) == 1
    assert len(third.receipts) == 1 and third.receipts[0].sustained is True
    rows = steward.goal_reading_series(goal["id"], last_n=10)
    stamps = [datetime.fromisoformat(row["captured_at"].replace("Z", "+00:00")) for row in rows]
    assert [int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:], strict=False)] == [
        60,
        60,
    ]


def test_build_rejects_periods_greater_than_max_cycles(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 4, "window": 60}, "effort": {"max_cycles": 3}},
    )
    restricted = goal_controller._DryRunGoalStore(steward)
    with pytest.raises(ValueError, match="must not exceed"):
        _isolated(
            steward,
            lambda: goal_controller.build_dry_run_controller(restricted, goal["id"]),
        )


def test_next_cycle_resumes_from_stored_history(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(steward)
    steward.append_goal_reading({"goal_id": goal["id"], "cycle": 4, "value": None, "met": 0})
    restricted = goal_controller._DryRunGoalStore(steward)
    controller = _isolated(
        steward,
        lambda: goal_controller.build_dry_run_controller(restricted, goal["id"]),
    )
    assert controller.start_cycle == 5
    assert controller.max_cycles == 0


def test_direct_builder_uses_stored_effort_cap(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(
        steward,
        target={"sustain": {"periods": 1, "window": 0}, "effort": {"max_cycles": 3}},
    )
    counterfeit = {
        **goal,
        "target": {**goal["target"], "effort": {"max_cycles": 99}},
    }
    restricted = goal_controller._DryRunGoalStore(steward)

    controller = _isolated(
        steward,
        lambda: goal_controller.build_dry_run_controller(restricted, counterfeit["id"]),
    )

    assert controller.effort_cap == 3
    assert controller.max_cycles == 3


def test_invalid_stored_setpoint_returns_refusal(tmp_path: Path) -> None:
    steward = _steward(tmp_path)
    goal = _make_goal(steward)
    steward.upsert_goal(
        {
            "name": goal["name"],
            "north_star": {},
            "target": {**goal["target"], "sustain": {"periods": 3, "window": "60"}},
        }
    )
    result = _isolated(steward, lambda: goal_controller.run_dry_run_cycle(steward, goal))
    assert result.receipts == []
    assert result.refusal is not None and result.refusal.reason == "setpoint"
