"""Fail-closed validation for goal-loop setpoints."""

from __future__ import annotations

import math
from typing import Any

from omniagentos.goals.metrics import get_metric_value
from omniagentos.steward.store import StewardStore

_COMPARATORS = frozenset({">=", "<=", "=="})


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _setpoint(goal: dict[str, Any]) -> tuple[str, str, float]:
    target = goal.get("target")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    source = target.get("metric_source")
    if not isinstance(source, str) or not source:
        raise ValueError("target.metric_source must be a non-empty string")
    comparator = target.get("comparator")
    if comparator not in _COMPARATORS:
        raise ValueError("target.comparator is not supported")
    return source, comparator, _number(target.get("target"), "target.target")


def _ancestors(
    spec: dict[str, Any], store: StewardStore, parent: dict[str, Any] | None
) -> list[dict[str, Any]]:
    parent_id = spec.get("parent_goal_id")
    if parent_id is None:
        if parent is not None:
            raise ValueError("parent supplied without parent_goal_id")
        return []
    if not isinstance(parent_id, str) or not parent_id:
        raise ValueError("parent_goal_id must be a non-empty string")
    current = parent or store.get_goal(parent_id)
    if current is None or current.get("id") != parent_id:
        raise ValueError("parent goal does not exist")
    ancestors: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current is not None:
        current_id = current.get("id")
        if not isinstance(current_id, str) or current_id in seen:
            raise ValueError("goal ancestry is malformed")
        seen.add(current_id)
        ancestors.append(current)
        next_id = current.get("parent_goal_id")
        if next_id is None:
            break
        if not isinstance(next_id, str) or not next_id:
            raise ValueError("goal ancestry is malformed")
        current = store.get_goal(next_id)
        if current is None:
            raise ValueError("goal ancestry references a missing parent")
    return ancestors


def _weakens(ancestor: tuple[str, str, float], child: tuple[str, str, float]) -> bool:
    _, ancestor_comparator, ancestor_target = ancestor
    _, child_comparator, child_target = child
    if ancestor_comparator == ">=":
        return child_comparator not in {">=", "=="} or child_target < ancestor_target
    if ancestor_comparator == "<=":
        return child_comparator not in {"<=", "=="} or child_target > ancestor_target
    return child_comparator != "==" or child_target != ancestor_target


def validate_setpoint(
    spec: dict[str, Any], *, store: StewardStore, parent: dict[str, Any] | None = None
) -> None:
    """Refuse an ambiguous, unmeasurable, over-deep, or weakened setpoint."""
    if not isinstance(spec, dict):
        raise ValueError("goal specification must be an object")
    source, comparator, target = _setpoint(spec)
    try:
        get_metric_value(store, source, goal_id=str(spec.get("id") or "__validation__"))
    except KeyError as exc:
        raise ValueError(f"target.metric_source is not registered: {source}") from exc

    setpoint = spec["target"]
    assert isinstance(setpoint, dict)
    sustain = setpoint.get("sustain")
    if not isinstance(sustain, dict):
        raise ValueError("target.sustain must be an object")
    periods = _positive_int(sustain.get("periods"), "target.sustain.periods")
    window_raw = sustain.get("window", 0)
    window = _number(window_raw, "target.sustain.window")
    if window < 0:
        raise ValueError("target.sustain.window must not be negative")
    if periods > 1 and window < 1:
        raise ValueError("target.sustain.window must be at least 1 second for multiple periods")
    effort = setpoint.get("effort")
    if not isinstance(effort, dict):
        raise ValueError("target.effort must be an object")
    max_cycles = _positive_int(effort.get("max_cycles"), "target.effort.max_cycles")
    if periods > max_cycles:
        raise ValueError("target.sustain.periods must not exceed target.effort.max_cycles")
    work_items = effort.get("max_work_items")
    budget = effort.get("budget_usd")
    if work_items is not None:
        _positive_int(work_items, "target.effort.max_work_items")
    if budget is not None and _number(budget, "target.effort.budget_usd") <= 0:
        raise ValueError("target.effort.budget_usd must be positive")
    if work_items is None and budget is None:
        raise ValueError("target.effort requires max_work_items or budget_usd")

    ancestors = _ancestors(spec, store, parent)
    if len(ancestors) > 2:
        raise ValueError("sub-goal depth may not exceed 2")
    child = (source, comparator, target)
    for ancestor_goal in ancestors:
        ancestor = _setpoint(ancestor_goal)
        if ancestor[0] == source and _weakens(ancestor, child):
            raise ValueError("sub-goal may not weaken an ancestor setpoint")
