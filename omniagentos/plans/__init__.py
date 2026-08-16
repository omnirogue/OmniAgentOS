"""Durable plans spine: plan creation, approval, and tracking (LANE A / t/cb-plans).

This package replaces the in-memory _PLAN_JOBS dictionary that was in
:mod:`omniagentos.api.routes.intake` with a durable, queryable database
abstraction. Plans survive API restarts and form an audit trail of what was
planned, approved, and executed.
"""

from omniagentos.plans.models import (
    PLAN_ID_PREFIX,
    PLAN_NONTERMINAL_STATUSES,
    PLAN_STATUSES,
    PLAN_TERMINAL_STATUSES,
    Plan,
    PlanNotFound,
    PlanValidationError,
)
from omniagentos.plans.store import PlansStore

__all__ = [
    "Plan",
    "PlansStore",
    "PlanValidationError",
    "PlanNotFound",
    "PLAN_ID_PREFIX",
    "PLAN_STATUSES",
    "PLAN_NONTERMINAL_STATUSES",
    "PLAN_TERMINAL_STATUSES",
]
