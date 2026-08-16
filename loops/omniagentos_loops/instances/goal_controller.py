"""Dry-run goal controller with a structurally restricted store capability.

The public entry point reloads the authoritative goal, refuses inactive goals,
and returns cycle, pacing, refusal, and escalation evidence. The controller's
only write capability is ``append_goal_reading``; graduation remains a report,
never a goal-status mutation.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from omniagentos.goals.metrics import get_metric_value
from omniagentos.steward.store import StewardStore, sustained_consecutive
from omniagentos_loops.templates.measure_gap_act import (
    CycleReceipt,
    DryRunController,
    EscalationReceipt,
    Measurement,
    PacingReceipt,
    RefusalReceipt,
)
from omniagentos_loops.tools import ToolRegistry

__all__ = [
    "CapabilityError",
    "DryRunResult",
    "build_dry_run_controller",
    "register",
    "run_dry_run_cycle",
]


class CapabilityError(RuntimeError):
    """Raised when the dry-run controller reaches outside its explicit grant."""


class _ReadOnlyCursor:
    """Expose query results without exposing cursor write affordances."""

    __slots__ = ("__cursor",)

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.__cursor = cursor

    @property
    def description(self) -> Any:
        return self.__cursor.description

    @property
    def connection(self) -> sqlite3.Connection:
        raise CapabilityError("dry-run metric cursor does not expose its connection")

    def fetchone(self) -> sqlite3.Row | None:
        return self.__cursor.fetchone()

    def fetchall(self) -> list[sqlite3.Row]:
        return self.__cursor.fetchall()

    def executescript(self, _sql: str) -> None:
        raise CapabilityError("dry-run metric cursor permits read-only SQL")

    def __iter__(self) -> _ReadOnlyCursor:
        return self

    def __next__(self) -> sqlite3.Row:
        return next(self.__cursor)


class _ReadOnlyConnection:
    """Small SQL-read seam needed by two closed metric implementations."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.__connection = connection

    def execute(self, sql: str, parameters: Any = ()) -> _ReadOnlyCursor:
        statement = sql.lstrip().upper()
        # The closed metric registry needs SELECT only. Allowing a generic WITH
        # prefix would also admit ``WITH ... UPDATE`` and counterfeit read-only.
        if not statement.startswith("SELECT"):
            raise CapabilityError("dry-run metric connection permits read-only SQL")
        return _ReadOnlyCursor(self.__connection.execute(sql, parameters))


def _append_capability(steward: StewardStore) -> Callable[[dict[str, Any]], int]:
    """Capture the sole write grant without retaining the store on the facade."""
    append = steward.append_goal_reading

    def invoke(reading: dict[str, Any]) -> int:
        return append(reading)

    return invoke


def _decode_json(value: object, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _decode_row(row: sqlite3.Row, fields: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for name, default in fields.items():
        decoded[name] = _decode_json(decoded.pop(f"{name}_json", None), default)
    return decoded


class _DryRunGoalStore:
    """Facade exposing the exact store capabilities required by this controller."""

    ALLOWED_ATTRIBUTES = frozenset(
        {
            "_connection",
            "append_goal_reading",
            "get_goal",
            "goal_reading_series",
            "snapshot_series",
        }
    )

    __slots__ = ("__append", "__connection")

    def __init__(self, steward: StewardStore) -> None:
        db_path = steward._store._db_path
        if db_path == ":memory:":
            raise CapabilityError("dry-run goal store requires a file-backed read-only database")
        append = _append_capability(steward)
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        object.__setattr__(self, "_DryRunGoalStore__append", append)
        object.__setattr__(self, "_DryRunGoalStore__connection", connection)

    def __getattribute__(self, name: str) -> Any:
        allowed = object.__getattribute__(self, "ALLOWED_ATTRIBUTES")
        if name == "ALLOWED_ATTRIBUTES" or name in allowed:
            return object.__getattribute__(self, name)
        raise CapabilityError(f"dry-run goal store capability denied: {name}")

    @property
    def _connection(self) -> _ReadOnlyConnection:
        connection = object.__getattribute__(self, "_DryRunGoalStore__connection")
        return _ReadOnlyConnection(connection)

    def append_goal_reading(self, reading: dict[str, Any]) -> int:
        append = object.__getattribute__(self, "_DryRunGoalStore__append")
        return append(reading)

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        connection = object.__getattribute__(self, "_DryRunGoalStore__connection")
        row = connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return (
            None
            if row is None
            else _decode_row(row, {"north_star": {}, "target": {}, "keywords": []})
        )

    def goal_reading_series(self, goal_id: str, *, last_n: int = 20) -> list[dict[str, Any]]:
        if last_n <= 0:
            return []
        connection = object.__getattribute__(self, "_DryRunGoalStore__connection")
        rows = connection.execute(
            "SELECT id, goal_id, cycle, value, met, captured_at FROM "
            "(SELECT id, goal_id, cycle, value, met, captured_at FROM goal_readings "
            "WHERE goal_id = ? ORDER BY cycle DESC, id DESC LIMIT ?) "
            "ORDER BY cycle ASC, id ASC",
            (goal_id, last_n),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_series(
        self,
        metric: str,
        *,
        goal_id: str | None = None,
        source: str | None = None,
        days: int = 14,
    ) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses = ["metric = ?", "captured_at >= ?"]
        parameters: list[Any] = [metric, cutoff]
        if goal_id is not None:
            clauses.append("goal_id = ?")
            parameters.append(goal_id)
        if source is not None:
            clauses.append("source = ?")
            parameters.append(source)
        connection = object.__getattribute__(self, "_DryRunGoalStore__connection")
        rows = connection.execute(
            "SELECT * FROM metric_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at ASC, id ASC",
            parameters,
        ).fetchall()
        return [_decode_row(row, {"meta": {}}) for row in rows]

    def __getattr__(self, name: str) -> Any:
        if name in self.ALLOWED_ATTRIBUTES:
            return object.__getattribute__(self, name)
        raise CapabilityError(f"dry-run goal store capability denied: {name}")

    def __del__(self) -> None:
        try:
            connection = object.__getattribute__(self, "_DryRunGoalStore__connection")
        except (AttributeError, CapabilityError):
            return
        connection.close()


@dataclass(frozen=True)
class DryRunResult:
    receipts: list[CycleReceipt]
    escalation: EscalationReceipt | None = None
    pacing: PacingReceipt | None = None
    refusal: RefusalReceipt | None = None


def _setpoint(goal: dict[str, Any]) -> tuple[str, str, float, int, int, float]:
    """Read and fail-closed validate the controller's stored setpoint."""
    target = goal.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"goal {goal.get('id')!r}: target is not an object")
    source = target.get("metric_source")
    comparator = target.get("comparator")
    setpoint = target.get("target")
    sustain = target.get("sustain")
    effort = target.get("effort")
    if not isinstance(source, str) or not source:
        raise ValueError(f"goal {goal.get('id')!r}: target.metric_source missing")
    if comparator not in (">=", "<=", "=="):
        raise ValueError(f"goal {goal.get('id')!r}: target.comparator missing/invalid")
    if isinstance(setpoint, bool) or not isinstance(setpoint, (int, float)):
        raise ValueError(f"goal {goal.get('id')!r}: target.target missing/invalid")
    setpoint_number = float(setpoint)
    if not math.isfinite(setpoint_number):
        raise ValueError(f"goal {goal.get('id')!r}: target.target must be finite")
    if not isinstance(sustain, dict):
        raise ValueError(f"goal {goal.get('id')!r}: target.sustain missing")
    periods = sustain.get("periods")
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 1:
        raise ValueError(f"goal {goal.get('id')!r}: target.sustain.periods missing/invalid")
    window = sustain.get("window", 0)
    if isinstance(window, bool) or not isinstance(window, (int, float)):
        raise ValueError(f"goal {goal.get('id')!r}: target.sustain.window invalid")
    window_seconds = float(window)
    if not math.isfinite(window_seconds) or window_seconds < 0:
        raise ValueError(f"goal {goal.get('id')!r}: target.sustain.window invalid")
    if periods > 1 and window_seconds < 1:
        raise ValueError("target.sustain.window must be at least 1 second for multiple periods")
    if not isinstance(effort, dict):
        raise ValueError(f"goal {goal.get('id')!r}: target.effort missing")
    max_cycles = effort.get("max_cycles")
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1:
        raise ValueError(f"goal {goal.get('id')!r}: target.effort.max_cycles missing/invalid")
    if periods > max_cycles:
        raise ValueError("target.sustain.periods must not exceed target.effort.max_cycles")
    return source, comparator, setpoint_number, periods, max_cycles, window_seconds


def _next_cycle(restricted: _DryRunGoalStore, goal_id: str) -> int:
    series = restricted.goal_reading_series(goal_id, last_n=1)
    if not series:
        return 0
    cycle = series[-1].get("cycle")
    if not isinstance(cycle, int) or isinstance(cycle, bool):
        raise ValueError(f"goal {goal_id!r}: latest reading has invalid cycle {cycle!r}")
    return cycle + 1


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_dry_run_controller(
    restricted: _DryRunGoalStore,
    goal_id: str,
    *,
    repo_path: str | None = None,
    cert_db_path: str | None = None,
    now: datetime | None = None,
) -> DryRunController:
    """Build from the authoritative active goal through the restricted facade."""
    goal = restricted.get_goal(goal_id)
    if goal is None:
        raise ValueError(f"goal {goal_id}: not found")
    status = goal.get("status")
    if status != "active":
        raise ValueError(f"goal {goal_id}: status {status!r} is not active")
    source, comparator, target, periods, max_cycles, window = _setpoint(goal)
    start_cycle = _next_cycle(restricted, goal_id)
    remaining = max_cycles - start_cycle

    def measure() -> Measurement:
        try:
            value = get_metric_value(
                cast(StewardStore, restricted),
                source,
                goal_id=goal_id,
                repo_path=repo_path,
                cert_db_path=cert_db_path,
            )
            if value is not None and not math.isfinite(value):
                return Measurement(None, f"non-finite value {value!r}")
            return Measurement(value)
        except Exception as exc:  # live instrument drift must remain visible
            return Measurement(None, f"{type(exc).__name__}: {exc}")

    def record(*, cycle: int, value: float | None, met: bool, captured_at: str) -> dict[str, Any]:
        written_id = restricted.append_goal_reading(
            {
                "goal_id": goal_id,
                "cycle": cycle,
                "value": value,
                "met": met,
                "captured_at": captured_at,
            }
        )
        rows = restricted.goal_reading_series(goal_id, last_n=1)
        if not rows or rows[-1].get("id") != written_id or rows[-1].get("cycle") != cycle:
            raise RuntimeError(f"goal {goal_id}: stored reading could not be correlated")
        return rows[-1]

    def evaluate(*, current_cycle: int) -> bool:
        readings = restricted.goal_reading_series(goal_id, last_n=max(20, periods))
        return sustained_consecutive(readings, periods, current_cycle=current_cycle)

    def pace(*, cycle: int, attempted_at: datetime) -> PacingReceipt | None:
        if window <= 0:
            return None
        rows = restricted.goal_reading_series(goal_id, last_n=1)
        if not rows:
            return None
        previous_raw = rows[-1].get("captured_at")
        previous = _parse_timestamp(previous_raw)
        elapsed = (
            None if previous is None else (attempted_at.astimezone(UTC) - previous).total_seconds()
        )
        if elapsed is not None and elapsed >= window:
            return None
        why = "unparseable previous timestamp" if elapsed is None else f"only {elapsed:g}s elapsed"
        return PacingReceipt(
            goal_id=goal_id,
            cycle=cycle,
            previous_captured_at=str(previous_raw),
            attempted_at=_stamp(attempted_at),
            window_seconds=window,
            detail=f"goal {goal_id}: pacing refused cycle {cycle}; {why}, requires {window:g}s",
        )

    return DryRunController(
        goal_id=goal_id,
        comparator=comparator,
        target=target,
        sustain_periods=periods,
        max_cycles=max(0, remaining),
        effort_cap=max_cycles,
        measure=measure,
        record=record,
        evaluate=evaluate,
        pace=pace,
        start_cycle=start_cycle,
        now=now,
    )


def run_dry_run_cycle(
    steward: StewardStore,
    goal: dict[str, Any],
    **kwargs: Any,
) -> DryRunResult:
    """Restrict the raw store once, then run outside its lexical scope."""
    restricted = _DryRunGoalStore(steward)
    goal_id = goal.get("id")
    if not isinstance(goal_id, str) or not goal_id:
        raise ValueError("goal id must be a non-empty string")
    return _run_restricted_dry_run_cycle(restricted, goal_id, **kwargs)


def _run_restricted_dry_run_cycle(
    restricted: _DryRunGoalStore,
    goal_id: str,
    **kwargs: Any,
) -> DryRunResult:
    """Run from the freshly loaded active goal and surface every outcome."""
    stored = restricted.get_goal(goal_id)
    if stored is None:
        return DryRunResult(
            [], refusal=RefusalReceipt(goal_id, "missing", f"goal {goal_id}: not found")
        )
    status = stored.get("status")
    if status != "active":
        return DryRunResult(
            [],
            refusal=RefusalReceipt(
                goal_id,
                "inactive",
                f"goal {goal_id}: status {status!r} is not active; measurement refused",
            ),
        )
    try:
        controller = build_dry_run_controller(restricted, goal_id, **kwargs)
    except ValueError as exc:
        return DryRunResult(
            [],
            refusal=RefusalReceipt(goal_id, "setpoint", f"goal {goal_id}: {exc}"),
        )
    receipts = controller.run()
    return DryRunResult(receipts, controller.escalation, controller.pacing)


def register(ctx: Any) -> None:
    """Keep the policy-gated tool registry empty; store authority is separate."""
    if not hasattr(ctx, "tools"):
        ctx.tools = ToolRegistry()
