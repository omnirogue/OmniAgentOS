"""Shapes and closed vocabularies for the durable plans spine (LANE A / t/cb-plans).

Migration 100 keeps ``status`` CHECK-free on purpose: the vocabulary is
enforced HERE, app-side, the same way :mod:`omniagentos.company_goals.models`
validates goal horizons and statuses (Migration 098) and
:mod:`omniagentos.scheduler.routines` validates routine scopes. That keeps the
vocabulary extensible without a SQLite table rebuild, at the cost of making
this module the one seam a writer must not bypass.

:func:`validate_plan_shape` is applied by :class:`omniagentos.plans.store.PlansStore`
to the EFFECTIVE row on create AND on the merged post-patch row inside the
update transaction, so "validated app-side" is a property of every write path,
not of the create path only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Plan lifecycle. 'draft' is the initial state; 'ready' means planning is done
# and the operator can approve; 'approved' means the plan is live; 'rejected'
# and 'superseded' are terminal.
PLAN_STATUSES: tuple[str, ...] = ("draft", "ready", "approved", "rejected", "superseded")

# Non-terminal statuses (can still be changed / moved to a different version).
PLAN_NONTERMINAL_STATUSES: tuple[str, ...] = ("draft", "ready")

# Terminal statuses (the plan's fate is decided).
PLAN_TERMINAL_STATUSES: tuple[str, ...] = ("approved", "rejected", "superseded")

# The ONE move allowed out of a terminal state: an approved plan is retired when
# a newer plan takes over as the current one. Everything else out of a terminal
# state is refused — an audit row whose decision can be overwritten (rejected ->
# approved silently clearing the rejection reason) is not an audit row.
PLAN_TERMINAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "approved": frozenset({"superseded"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
}

PLAN_ID_PREFIX = "pln"


class PlanValidationError(ValueError):
    """Raised when a plan payload violates an app-side rule.

    ``errors`` holds every violation found (not just the first) so an operator
    fixing a form sees all of them in one round trip — same contract as
    :class:`omniagentos.company_goals.models.CompanyGoalValidationError`.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(errors))


class PlanNotFound(PlanValidationError):
    """The plan ADDRESSED by the caller does not exist."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        super().__init__([f"plan not found: {plan_id}"])


class Plan(BaseModel):
    """A durable record of what was planned, approved, and executed.

    ``chat_id`` and ``board_task_id`` are the sources the plan addresses. At
    least one must be non-null. Multiple plans can exist for the same chat
    (different versions), but only one non-terminal (draft|ready) plan is
    allowed per chat, enforced by the database's partial unique index.

    ``org_company_id`` and ``work_folder`` are optional metadata binding the
    plan to an organizational context and a resolved ~/Work-relative path.

    Every column of the ``plans`` table is declared here. A read model that
    silently drops a column (``execution_metadata`` did) makes that column
    write-only in practice, which is how a durable field ends up with no reader.
    """

    id: str
    chat_id: str | None = None
    board_task_id: str | None = None
    version: int
    status: str
    plan_json: str  # ProjectPlan.model_dump() as JSON
    content_md: str | None = None
    goal: str | None = None
    harness: str | None = None
    execute_mode: str | None = None
    speed: str | None = None
    route_json: str | None = None  # RouteDecision.model_dump() as JSON
    route_target_name: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    reason: str | None = None
    org_company_id: str | None = None
    work_folder: str | None = None
    execution_metadata: str | None = None  # approved execution parameters, JSON
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Plan:
        return cls.model_validate(dict(row))


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_plan_shape(
    *,
    chat_id: Any,
    board_task_id: Any,
    version: Any,
    status: Any,
    plan_json: Any,
    org_company_id: Any = None,
    work_folder: Any = None,
) -> list[str]:
    """Validate a plan's EFFECTIVE shape (post-patch), returning every error.

    Callers pass the row as it would exist after the write, so the same rules
    bind create and update.

    chat_id and board_task_id can both be null (for provisional plans created
    during planning that haven't been assigned a destination yet). They will be
    set at confirmation time.
    """
    errors: list[str] = []

    # Both can be null (provisional), but if either is provided, it must be non-empty
    if chat_id is not None and not _nonempty(chat_id):
        errors.append("chat_id must be a non-empty id or null")
    if board_task_id is not None and not _nonempty(board_task_id):
        errors.append("board_task_id must be a non-empty id or null")

    # version must be a positive integer.
    try:
        v = int(version) if version is not None else None
        if v is None or v < 1:
            errors.append("version must be a positive integer")
    except (TypeError, ValueError):
        errors.append("version must be a positive integer")

    # status must be in the allowed vocabulary.
    if status not in PLAN_STATUSES:
        errors.append(f"status must be one of {sorted(PLAN_STATUSES)}")

    # plan_json must be non-empty.
    if not _nonempty(plan_json):
        errors.append("plan_json is required")

    return errors


def validate_plan_transition(current: Any, new: Any) -> list[str]:
    """Validate a status MOVE, returning every violation.

    Terminal means terminal: once a plan is approved, rejected or superseded the
    only move left is ``approved -> superseded`` (a newer plan takes over). A
    no-op (``current == new``) is always allowed so an idempotent re-write of an
    already-decided row is not an error.
    """
    errors: list[str] = []
    if new is None or current == new:
        return errors
    if current in PLAN_TERMINAL_STATUSES and new not in PLAN_TERMINAL_TRANSITIONS.get(
        str(current), frozenset()
    ):
        allowed = sorted(PLAN_TERMINAL_TRANSITIONS.get(str(current), frozenset()))
        errors.append(
            f"{current} is terminal: refusing transition to {new} "
            f"(allowed from {current}: {allowed or 'nothing'})"
        )
    return errors
