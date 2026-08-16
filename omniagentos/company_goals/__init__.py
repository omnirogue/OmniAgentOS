"""Company goals spine — employees, company goals, and goal↔Jira links (JG2).

Schema lives in migration ``098_company_goals.sql`` (Migration B). This package
owns those three tables and nothing else: the Horizon-4 steward ``goals`` table
from ``007_steward.sql`` is a DIFFERENT, pre-existing surface and is never read
or written here.
"""

from __future__ import annotations

from omniagentos.company_goals.models import (
    GOAL_STATUSES,
    HORIZONS,
    CompanyGoal,
    CompanyGoalJiraLink,
    CompanyGoalValidationError,
    Employee,
)
from omniagentos.company_goals.seed_employees import SEED_EMPLOYEES, seed_employees
from omniagentos.company_goals.service import CompanyGoalsService
from omniagentos.company_goals.store import CompanyGoalsStore

__all__ = [
    "GOAL_STATUSES",
    "HORIZONS",
    "SEED_EMPLOYEES",
    "CompanyGoal",
    "CompanyGoalJiraLink",
    "CompanyGoalValidationError",
    "CompanyGoalsService",
    "CompanyGoalsStore",
    "Employee",
    "seed_employees",
]
