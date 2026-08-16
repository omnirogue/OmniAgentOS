"""Shapes and closed vocabularies for the company-goals spine (JG2-BE).

Migration 098 keeps ``horizon`` and ``status`` CHECK-free on purpose: the
vocabulary is enforced HERE, app-side, the same way ``routines.scope`` is
validated in :mod:`omniagentos.scheduler.routines` (Migration F). That keeps the
vocabulary extensible without a SQLite table rebuild, at the cost of making this
module the one seam a writer must not bypass.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Goal horizons. A short_term goal is always a step toward a long_term one, so
# it REQUIRES a parent (see CompanyGoalsService — the decisive rule of this lane).
LONG_TERM = "long_term"
SHORT_TERM = "short_term"
HORIZONS: tuple[str, ...] = (LONG_TERM, SHORT_TERM)

# Goal lifecycle. 'active' is the migration default.
GOAL_STATUSES: tuple[str, ...] = ("active", "paused", "achieved", "archived")

# Employee lifecycle. 'active' is the migration default.
EMPLOYEE_STATUSES: tuple[str, ...] = ("active", "inactive")

GOAL_ID_PREFIX = "cgl"
EMPLOYEE_ID_PREFIX = "emp"
JIRA_LINK_ID_PREFIX = "cgj"


class CompanyGoalValidationError(ValueError):
    """Raised when a company-goal payload violates an app-side rule.

    ``errors`` holds every violation found (not just the first) so an operator
    fixing a form sees all of them in one round trip — same contract as
    :class:`omniagentos.scheduler.routines.RoutineValidationError`.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(errors))


class CompanyGoalNotFound(CompanyGoalValidationError):
    """The goal ADDRESSED by the caller does not exist.

    A subclass so that any caller guarding against a bad payload still catches
    it, while HTTP routes can map the addressed-resource case to 404 and leave
    bad *body* references (unknown ``org_company_id`` / ``parent_goal_id``) at
    400. That split is the REST-shaped reading of the same failure.
    """

    def __init__(self, goal_id: str) -> None:
        self.goal_id = goal_id
        super().__init__([f"goal not found: {goal_id}"])


class Employee(BaseModel):
    """A person whose transcripts feed the pipeline.

    ``jira_account_id`` is NULL until an operator maps the person to a live Jira
    account (JG2-E13); nothing in this lane invents one.
    """

    id: str
    name: str
    role: str | None = None
    jira_account_id: str | None = None
    status: str = "active"
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Employee:
        return cls.model_validate(dict(row))


class CompanyGoal(BaseModel):
    id: str
    org_company_id: str
    title: str
    horizon: str
    parent_goal_id: str | None = None
    status: str = "active"
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CompanyGoal:
        return cls.model_validate(dict(row))


class CompanyGoalJiraLink(BaseModel):
    id: str
    goal_id: str
    jira_project_key: str
    jira_issue_key: str | None = None
    link_kind: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CompanyGoalJiraLink:
        return cls.model_validate(dict(row))


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_goal_shape(
    *,
    title: Any,
    horizon: Any,
    parent_goal_id: Any,
    status: Any,
) -> list[str]:
    """Validate a goal's EFFECTIVE shape (post-patch), returning every error.

    Callers pass the row as it would exist after the write, so the same rules
    bind create and PATCH: a goal that BECOMES ``short_term``, or whose parent is
    cleared while it is ``short_term``, is refused exactly like a bad create.
    """
    errors: list[str] = []
    if not _nonempty(title):
        errors.append("title is required")
    if horizon not in HORIZONS:
        errors.append(f"horizon must be one of {sorted(HORIZONS)}")
    if status not in GOAL_STATUSES:
        errors.append(f"status must be one of {sorted(GOAL_STATUSES)}")
    if parent_goal_id is not None and not _nonempty(parent_goal_id):
        errors.append("parent_goal_id must be a non-empty id or null")
    if horizon == SHORT_TERM and not _nonempty(parent_goal_id):
        errors.append(
            "a short_term goal requires parent_goal_id (it must ladder up to a long_term goal)"
        )
    return errors


def validate_jira_link_shape(
    *,
    jira_project_key: Any,
    jira_issue_key: Any,
    link_kind: Any,
) -> list[str]:
    """Shape rules for one goal↔Jira link.

    A Jira issue key CONTAINS its project key (``HOO-42`` is an issue of
    ``HOO``), so an issue key whose prefix disagrees with ``jira_project_key``
    is not an alternative spelling — it is a link that lies about which project
    the work lives in, and it defeats every per-project roll-up downstream.
    """
    errors: list[str] = []
    if not _nonempty(jira_project_key):
        errors.append("jira_project_key is required")
    if not _nonempty(link_kind):
        errors.append("link_kind is required")
    if jira_issue_key is not None:
        if not _nonempty(jira_issue_key):
            errors.append("jira_issue_key must be a non-empty key or null")
        else:
            prefix, dash, number = str(jira_issue_key).strip().partition("-")
            if not dash or not number:
                errors.append(
                    f"jira_issue_key {jira_issue_key!r} must look like <PROJECT>-<number>"
                )
            elif _nonempty(jira_project_key) and prefix != str(jira_project_key).strip():
                errors.append(
                    f"jira_issue_key {jira_issue_key!r} does not belong to project "
                    f"{jira_project_key!r}"
                )
    return errors


def validate_employee_shape(*, name: Any, status: Any) -> list[str]:
    errors: list[str] = []
    if not _nonempty(name):
        errors.append("name is required")
    if status not in EMPLOYEE_STATUSES:
        errors.append(f"status must be one of {sorted(EMPLOYEE_STATUSES)}")
    return errors
