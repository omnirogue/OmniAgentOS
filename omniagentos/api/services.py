"""Reusable control-plane creation services shared by HTTP and Steward flows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any, NoReturn

from omniagentos.api.models import StepSpec
from omniagentos.contracts import (
    Arm,
    BudgetSpec,
    Events,
    HarnessType,
    RunState,
    Store,
    TaskState,
    can_transition_task,
    new_id,
    utc_now_iso,
)
from omniagentos.policy import PolicyConfig, PolicyError, validate_tools

_DEFAULT_RUN_PRIORITY = 2
_BACKGROUND_RUN_ORIGINS = frozenset({"background", "routine", "scheduled"})
_TRUSTED_CRITICAL_IMPROVEMENT_ORIGINS = frozenset({"audit", "cto", "department", "human"})
_BOARD_PRIORITY_TO_RUN_PRIORITY = {
    "urgent": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, detail: Any = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def fail(status: int, code: str, message: str, detail: Any = None) -> NoReturn:
    raise ApiError(status, code, message, detail)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _row(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    out = dict(row)
    for key in ("paused", "usage_estimated"):
        if key in out and out[key] is not None:
            out[key] = bool(out[key])
    return out


def board_priority_to_run_priority(value: Any) -> int:
    """Map the durable human board field into the shared scheduler taxonomy.

    ``urgent`` maps to 0 rather than 1: it is the operator's explicit
    bottleneck signal. The service still verifies the board row before it will
    honor that zero, so copying the label into an arbitrary request is inert.
    """
    return _BOARD_PRIORITY_TO_RUN_PRIORITY.get(
        str(value or "").strip().lower(), _DEFAULT_RUN_PRIORITY
    )


def _priority_connection(store: Store) -> sqlite3.Connection | None:
    """Find the real SQLite provenance source without widening the Store contract."""
    candidate: Any = store
    for _ in range(3):
        connection = getattr(candidate, "_connection", None)
        if isinstance(connection, sqlite3.Connection):
            return connection
        candidate = getattr(candidate, "_store", None)
        if candidate is None:
            break
    return None


def _proposal_is_security_critical(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        proposal = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(proposal, dict):
        return False
    return proposal.get("security_critical") is True or str(
        proposal.get("criticality") or ""
    ).lower() in {"security", "security-critical", "security_critical"}


def _provenance_priority(
    store: Store,
    *,
    origin: str | None,
    provenance_id: str | None,
) -> tuple[int, bool]:
    """Return a derived priority and whether provenance authorizes priority zero.

    Zero is never authorized by a caller's label alone. It requires a durable
    urgent board row or a trusted, critical improvement row read from SQLite.
    This is why the HTTP run route can remain backward compatible without
    exposing a self-declared fast lane.
    """
    normalized_origin = str(origin or "").strip().lower()
    connection = _priority_connection(store)

    if normalized_origin == "board_task" and provenance_id and connection is not None:
        row = connection.execute(
            "SELECT priority FROM board_tasks WHERE id = ?", (provenance_id,)
        ).fetchone()
        if row is not None:
            priority = board_priority_to_run_priority(row["priority"])
            return priority, priority == 0

    if normalized_origin == "improvement" and provenance_id and connection is not None:
        row = connection.execute(
            "SELECT origin, kind, proposal_json FROM improvements WHERE id = ?",
            (provenance_id,),
        ).fetchone()
        if row is not None:
            kind = str(row["kind"] or "").strip().lower()
            improvement_origin = str(row["origin"] or "").strip().lower()
            if kind in {"fix", "optimization"}:
                return 1, False
            critical = kind == "architecture" or _proposal_is_security_critical(
                row["proposal_json"]
            )
            trusted = improvement_origin in _TRUSTED_CRITICAL_IMPROVEMENT_ORIGINS
            if critical and trusted:
                return 0, True
            return _DEFAULT_RUN_PRIORITY, False

    if normalized_origin in _BACKGROUND_RUN_ORIGINS:
        return 3, False
    return _DEFAULT_RUN_PRIORITY, False


def _resolve_run_priority(
    store: Store,
    *,
    priority: int | None,
    origin: str | None,
    provenance_id: str | None,
) -> int:
    derived, zero_authorized = _provenance_priority(
        store, origin=origin, provenance_id=provenance_id
    )
    if priority is None:
        return derived
    if isinstance(priority, bool) or not isinstance(priority, int) or priority not in range(4):
        fail(422, "validation", "priority must be an integer from 0 through 3")
    if priority == 0 and not zero_authorized:
        return derived
    return priority


def _emit(
    store: Any,
    event_type: str,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
    trace_id: str = "",
) -> None:
    store.insert_event(
        event_type,
        "api",
        action,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
        trace_id=trace_id,
    )


def create_task_service(
    store: Store,
    policy_cfg: PolicyConfig,
    *,
    title: str,
    discipline_id: str | None = None,
    project_id: str | None = None,
    input: dict[str, Any] | None = None,
    acceptance: dict[str, Any] | None = None,
    risk: str | None = None,
    tools_allowed: list[str] | None = None,
) -> dict[str, Any]:
    """Create and ready a task with the same guards as the HTTP endpoint."""
    if discipline_id and not any(
        row.get("id") == discipline_id for row in store.list_disciplines()
    ):
        fail(404, "not_found", "discipline not found", {"id": discipline_id})
    input_value = dict(input or {})
    if tools_allowed is not None:
        try:
            validate_tools(tools_allowed, policy_cfg)
        except PolicyError as exc:
            fail(
                422,
                "validation",
                str(exc),
                {"tools_allowed": tools_allowed},
            )
        input_value["tools_allowed"] = tools_allowed
    now = utc_now_iso()
    row: dict[str, Any] = {
        "id": new_id("tsk"),
        "discipline_id": discipline_id,
        "project_id": project_id,
        "title": title,
        "input_json": _json(input_value),
        "acceptance_json": _json(acceptance or {}),
        "state": TaskState.DRAFT.value,
        "risk": risk or "low",
        "created_at": now,
        "updated_at": now,
    }
    try:
        store.create_task(row)
    except sqlite3.IntegrityError as exc:
        # A task.project_id that references a non-existent project trips the FK
        # constraint. Translate that into a stable 404 instead of a raw 500 (F8).
        if project_id:
            fail(404, "not_found", "project not found", {"project_id": project_id})
        raise ApiError(409, "conflict", "task could not be created") from exc
    if not can_transition_task(TaskState.DRAFT, TaskState.READY) or not store.update_task_state(
        row["id"], TaskState.READY.value, expect=[TaskState.DRAFT.value]
    ):
        fail(409, "invalid_state", "task cannot transition to ready")
    task = _row(store.get_task(row["id"]))
    _emit(
        store,
        Events.TASK_UPDATED,
        "task.created",
        target_type="task",
        target_id=row["id"],
        payload={"task_id": row["id"], "state": TaskState.READY.value},
    )
    return task


def _default_plan() -> list[dict[str, Any]]:
    return [
        {
            "name": "agent",
            "kind": "agent",
            "action_class": "sandboxed_creation",
            "params": {},
        }
    ]


def _validated_plan(
    plan: Sequence[StepSpec | dict[str, Any]] | None,
    prompt: str | None,
    policy_cfg: PolicyConfig,
) -> list[dict[str, Any]]:
    normalized = (
        [
            (step if isinstance(step, StepSpec) else StepSpec.model_validate(step)).model_dump(
                mode="json"
            )
            for step in plan
        ]
        if plan is not None
        else _default_plan()
    )
    if prompt is not None:
        for step in normalized:
            if step["kind"] == "agent":
                params = step.setdefault("params", {})
                params.setdefault("prompt", prompt)
                break
    seen_validate = False
    for step in normalized:
        is_validate = step["kind"] == "validate"
        if seen_validate and not is_validate:
            fail(
                422,
                "validation",
                "validate steps must follow all non-validate steps",
                {"plan": normalized},
            )
        step_tools = step.get("params", {}).get("tools_allowed")
        if isinstance(step_tools, list):
            normalized_tools = [str(tool) for tool in step_tools]
            try:
                validate_tools(normalized_tools, policy_cfg)
            except PolicyError as exc:
                fail(
                    422,
                    "validation",
                    str(exc),
                    {"tools_allowed": step_tools},
                )
        seen_validate = seen_validate or is_validate
    return normalized


def create_run_service(
    store: Store,
    policy_cfg: PolicyConfig,
    *,
    task_id: str,
    harness: HarnessType | str,
    arm: Arm | str | None = None,
    model: str | None = None,
    plan: Sequence[StepSpec | dict[str, Any]] | None = None,
    budget: BudgetSpec | dict[str, Any] | None = None,
    prompt: str | None = None,
    priority: int | None = None,
    origin: str | None = None,
    provenance_id: str | None = None,
) -> dict[str, Any]:
    """Enqueue a run with provenance-derived, non-self-declared queue priority.

    ``priority`` is optional. Priority zero is honored only when ``origin`` and
    ``provenance_id`` resolve to a trusted durable row; unknown callers retain
    the historical normal priority (2). The public HTTP route intentionally
    supplies none of these internal provenance arguments.
    """
    task = store.get_task(task_id)
    if task is None:
        fail(404, "not_found", "task not found", {"id": task_id})
    if task.get("state") != TaskState.READY.value or not can_transition_task(
        TaskState.READY, TaskState.QUEUED
    ):
        fail(
            409,
            "invalid_state",
            "task is not ready to enqueue",
            {"state": task.get("state")},
        )
    validated_plan = _validated_plan(plan, prompt, policy_cfg)
    now = utc_now_iso()
    run_id = new_id("run")
    harness_value = harness.value if isinstance(harness, HarnessType) else harness
    arm_value = arm.value if isinstance(arm, Arm) else arm
    budget_value = (
        BudgetSpec.model_validate(budget).model_dump(mode="json") if budget is not None else {}
    )
    run_priority = _resolve_run_priority(
        store,
        priority=priority,
        origin=origin,
        provenance_id=provenance_id,
    )
    row: dict[str, Any] = {
        "id": run_id,
        "task_id": task_id,
        "discipline_id": task.get("discipline_id"),
        # Denormalized from the owning task (migration 058, NSC-C39-04) so
        # runs are project-scopable without a join back through tasks. A
        # task with no project (unscoped/global, pre-W2 default) yields
        # project_id=None on the run, matching the task's own NULL.
        "project_id": task.get("project_id"),
        "arm": arm_value,
        "harness": harness_value,
        "harness_version": "",
        "env_hash": "",
        "harness_params": _json({"prompt": prompt} if prompt is not None else {}),
        "agent": None,
        "model": model,
        "attempt": 1,
        "state": RunState.QUEUED.value,
        "priority": run_priority,
        "worker_id": None,
        "plan_json": _json(validated_plan),
        "output_text": None,
        "output_json": None,
        "error": None,
        "wall_ms": None,
        "turns": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "usage_estimated": True,
        "usage_source": "estimator",
        "budget_json": _json(budget_value),
        "cancel_requested": 0,
        "trace_id": new_id("trc"),
        "session_ref": None,
        "manifest_path": None,
        "vault_note_path": None,
        "queued_at": now,
        "started_at": None,
        "finished_at": None,
        "created_at": now,
        "updated_at": now,
    }
    store.enqueue_run(row)
    if not store.update_task_state(task_id, TaskState.QUEUED.value, expect=[TaskState.READY.value]):
        fail(409, "invalid_state", "task cannot transition to queued")
    _emit(
        store,
        Events.TASK_UPDATED,
        "task.queued",
        target_type="task",
        target_id=task_id,
        payload={"task_id": task_id, "state": TaskState.QUEUED.value},
        trace_id=row["trace_id"],
    )
    _emit(
        store,
        Events.RUN_UPDATED,
        "run.queued",
        target_type="run",
        target_id=run_id,
        payload={
            "run_id": run_id,
            "task_id": task_id,
            "state": RunState.QUEUED.value,
            "harness": row["harness"],
            "arm": row["arm"],
            "priority": row["priority"],
        },
        trace_id=row["trace_id"],
    )
    return _row(store.get_run(run_id))
