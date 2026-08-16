"""Goal dashboard HTTP surface."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.api.routes.improvements import AuthenticatedPrincipalDep
from omniagentos.db.store import SqliteStore
from omniagentos.goals.provision import validate_setpoint
from omniagentos.goals.service import goal_detail, goal_summary
from omniagentos.steward.store import GoalExistsError, StewardStore

router = APIRouter(prefix="/api/goals", tags=["goals"])


class FactLinkRequest(BaseModel):
    fact_id: int = Field(ge=1)


class TargetRequest(BaseModel):
    target: dict[str, Any]


class CreateGoalRequest(BaseModel):
    """Narrow, caller-settable surface for POST /api/goals.

    Excludes status/graduated_at/routine_id/origin/id -- server-derived only
    (see create_goal), so a caller cannot mass-assign a forged graduation or
    spoof provenance through create.
    """

    name: str = Field(min_length=1)
    description: str | None = None
    north_star: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any]
    parent_goal_id: str | None = None
    keywords: list[str] = Field(default_factory=list)
    priority: int = 100


def _stores(store: StoreDep) -> tuple[SqliteStore, StewardStore]:
    database = cast(SqliteStore, store)
    return database, StewardStore(database)


@router.get("")
def list_goals(store: StoreDep) -> list[dict[str, Any]]:
    _, steward = _stores(store)
    return [goal_summary(steward, goal) for goal in steward.list_goals()]


@router.get("/{goal_id}")
def get_goal(goal_id: str, store: StoreDep) -> dict[str, Any]:
    database, steward = _stores(store)
    goal = steward.get_goal(goal_id)
    if goal is None:
        fail(404, "not_found", "goal not found", {"id": goal_id})
    return goal_detail(steward, goal, database)


def _sustain_progress(steward: StewardStore, goal: dict[str, Any]) -> dict[str, Any]:
    target = goal.get("target")
    sustain = target.get("sustain") if isinstance(target, dict) else None
    periods = sustain.get("periods") if isinstance(sustain, dict) else 0
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 1:
        periods = 0
    window_raw = sustain.get("window", 0) if isinstance(sustain, dict) else 0
    window_value = (
        float(window_raw)
        if not isinstance(window_raw, bool) and isinstance(window_raw, (int, float))
        else None
    )
    window = window_value if window_value is not None and math.isfinite(window_value) else None
    readings = steward.goal_reading_series(goal["id"], last_n=max(20, periods))
    consecutive = 0
    expected_cycle: int | None = None
    later_timestamp: datetime | None = None
    for reading in reversed(readings):
        cycle = reading.get("cycle")
        if (
            reading.get("value") is None
            or not reading.get("met")
            or isinstance(cycle, bool)
            or not isinstance(cycle, int)
            or expected_cycle is not None
            and cycle != expected_cycle
        ):
            break
        if periods > 1:
            if window is None or window < 1:
                break
            captured_at = reading.get("captured_at")
            timestamp: datetime | None
            try:
                timestamp = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
                timestamp = timestamp.astimezone(UTC) if timestamp.tzinfo is not None else None
            except ValueError:
                timestamp = None
            if timestamp is None or (
                later_timestamp is not None
                and (later_timestamp - timestamp).total_seconds() < window
            ):
                break
            later_timestamp = timestamp
        consecutive += 1
        expected_cycle = cycle - 1
    return {
        "latest_reading": readings[-1] if readings else None,
        "sustain_progress": {"consecutive_met": min(consecutive, periods), "periods": periods},
    }


def _enrich_tree(steward: StewardStore, node: dict[str, Any]) -> dict[str, Any]:
    goal = node["goal"]
    node.update(_sustain_progress(steward, goal))
    node["children"] = [_enrich_tree(steward, child) for child in node["children"]]
    return node


@router.get("/tree/{goal_id}")
def get_goal_tree(goal_id: str, store: StoreDep) -> dict[str, Any]:
    _, steward = _stores(store)
    if steward.get_goal(goal_id) is None:
        fail(404, "not_found", "goal not found", {"id": goal_id})
    return _enrich_tree(steward, steward.goal_tree(goal_id))


@router.post("", status_code=201)
def create_goal(
    body: CreateGoalRequest, store: StoreDep, principal: AuthenticatedPrincipalDep
) -> dict[str, Any]:
    if principal is None:
        fail(403, "forbidden", "an authenticated principal is required to create a goal")
    _, steward = _stores(store)
    # Explicit allowlist; the write is CREATE-ONLY and atomic (r3): a plain
    # INSERT whose UNIQUE(name) refusal raises GoalExistsError inside one
    # serialized write — a concurrent duplicate can never take upsert_goal's
    # update path (which would clear lineage / lower bars past the guard).
    goal: dict[str, Any] = {
        "name": body.name,
        "description": body.description or "",
        "north_star": body.north_star,
        "target": body.target,
        "parent_goal_id": body.parent_goal_id,
        "keywords": body.keywords,
        "priority": body.priority,
        "origin": "human",
    }
    try:
        validate_setpoint(goal, store=steward)
        return steward.insert_goal(goal)
    except GoalExistsError:
        fail(409, "already_exists", "a goal with this name already exists", {"name": body.name})
    except (KeyError, ValueError) as exc:
        fail(422, "invalid_goal", str(exc))


@router.post("/{goal_id}/pause")
def pause_goal(
    goal_id: str, store: StoreDep, principal: AuthenticatedPrincipalDep
) -> dict[str, Any]:
    if principal is None:
        fail(403, "forbidden", "an authenticated principal is required to pause a goal")
    _, steward = _stores(store)
    goal = steward.get_goal(goal_id)
    if goal is None:
        fail(404, "not_found", "goal not found", {"id": goal_id})
    try:
        return steward.upsert_goal(
            {"name": goal["name"], "north_star": goal["north_star"], "status": "paused"}
        )
    except ValueError as exc:
        fail(422, "invalid_goal", str(exc))


@router.patch("/{goal_id}/target")
def update_goal_target(
    goal_id: str,
    body: TargetRequest,
    store: StoreDep,
    principal: AuthenticatedPrincipalDep,
) -> dict[str, Any]:
    if principal is None:
        fail(403, "forbidden", "an authenticated principal is required to update a goal target")
    _, steward = _stores(store)
    goal = steward.get_goal(goal_id)
    if goal is None:
        fail(404, "not_found", "goal not found", {"id": goal_id})
    updated = dict(goal)
    updated["target"] = body.target
    try:
        validate_setpoint(updated, store=steward)
        return steward.upsert_goal(
            {"name": goal["name"], "north_star": goal["north_star"], "target": body.target}
        )
    except (KeyError, ValueError) as exc:
        fail(422, "invalid_goal", str(exc))


@router.post("/{goal_id}/facts")
def link_fact(goal_id: str, body: FactLinkRequest, store: StoreDep) -> dict[str, Any]:
    _, steward = _stores(store)
    if steward.get_goal(goal_id) is None:
        fail(404, "not_found", "goal not found", {"id": goal_id})
    steward.link_fact_to_goal(goal_id, body.fact_id, "operator")
    return {"goal_id": goal_id, "fact_id": body.fact_id}
