"""Company goals + employees HTTP surface (JG2-BE).

Two routers because the two resources are siblings, not nested:
``/api/company-goals`` (goals and their Jira links) and ``/api/employees``.

Error style follows the rest of the API: :func:`fail` for every non-2xx, 400 for
a payload the service refuses, 404 for an addressed resource that does not
exist. The service — never a route — owns the rules, so the same guarantees hold
for a direct caller.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.company_goals.models import (
    CompanyGoalNotFound,
    CompanyGoalValidationError,
)
from omniagentos.company_goals.service import CompanyGoalsService
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore

router = APIRouter(prefix="/api/company-goals", tags=["company-goals"])
employees_router = APIRouter(prefix="/api/employees", tags=["employees"])


def _service(store: StoreDep) -> CompanyGoalsService:
    return CompanyGoalsService(CompanyGoalsStore(cast(SqliteStore, store)))


class CreateGoalBody(BaseModel):
    org_company_id: str
    title: str
    horizon: str
    parent_goal_id: str | None = None
    status: str = "active"


class PatchGoalBody(BaseModel):
    title: str | None = Field(default=None)
    horizon: str | None = Field(default=None)
    parent_goal_id: str | None = Field(default=None)
    status: str | None = Field(default=None)


class CreateJiraLinkBody(BaseModel):
    jira_project_key: str
    link_kind: str
    jira_issue_key: str | None = None


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------


@router.get("")
def _list_company_goals(
    store: StoreDep,
    org_company_id: str | None = None,
    horizon: str | None = None,
    status: str | None = None,
    parent_goal_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    goals = _service(store).list_goals(
        org_company_id=org_company_id,
        horizon=horizon,
        status=status,
        parent_goal_id=parent_goal_id,
        limit=limit,
    )
    return {"goals": [goal.model_dump() for goal in goals]}


@router.post("", status_code=201)
def _create_company_goal(body: CreateGoalBody, store: StoreDep) -> dict[str, Any]:
    try:
        goal = _service(store).create_goal(
            org_company_id=body.org_company_id,
            title=body.title,
            horizon=body.horizon,
            parent_goal_id=body.parent_goal_id,
            status=body.status,
        )
    except CompanyGoalValidationError as exc:
        fail(400, "validation", str(exc), {"errors": exc.errors})
    return goal.model_dump()


@router.get("/{goal_id}")
def _get_company_goal(goal_id: str, store: StoreDep) -> dict[str, Any]:
    goal = _service(store).get_goal(goal_id)
    if goal is None:
        fail(404, "not_found", "company goal not found", {"id": goal_id})
    return goal.model_dump()


@router.patch("/{goal_id}")
def _patch_company_goal(goal_id: str, body: PatchGoalBody, store: StoreDep) -> dict[str, Any]:
    provided = body.model_fields_set
    patch: dict[str, Any] = {
        field: getattr(body, field)
        for field in ("title", "horizon", "parent_goal_id", "status")
        if field in provided
    }
    try:
        goal = _service(store).update_goal(goal_id, **patch)
    except CompanyGoalNotFound as exc:
        fail(404, "not_found", "company goal not found", {"id": exc.goal_id})
    except CompanyGoalValidationError as exc:
        fail(400, "validation", str(exc), {"errors": exc.errors})
    return goal.model_dump()


# ---------------------------------------------------------------------------
# goal <-> jira links
# ---------------------------------------------------------------------------


@router.get("/{goal_id}/jira-links")
def _list_goal_jira_links(
    goal_id: str,
    store: StoreDep,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    try:
        links = _service(store).list_jira_links(goal_id, limit=limit)
    except CompanyGoalNotFound as exc:
        fail(404, "not_found", "company goal not found", {"id": exc.goal_id})
    return {"links": [link.model_dump() for link in links]}


@router.post("/{goal_id}/jira-links", status_code=201)
def _create_goal_jira_link(
    goal_id: str, body: CreateJiraLinkBody, store: StoreDep
) -> dict[str, Any]:
    try:
        link = _service(store).create_jira_link(
            goal_id=goal_id,
            jira_project_key=body.jira_project_key,
            jira_issue_key=body.jira_issue_key,
            link_kind=body.link_kind,
        )
    except CompanyGoalNotFound as exc:
        fail(404, "not_found", "company goal not found", {"id": exc.goal_id})
    except CompanyGoalValidationError as exc:
        fail(400, "validation", str(exc), {"errors": exc.errors})
    return link.model_dump()


@router.delete("/{goal_id}/jira-links/{link_id}")
def _delete_goal_jira_link(goal_id: str, link_id: str, store: StoreDep) -> dict[str, Any]:
    deleted = _service(store).delete_jira_link(goal_id, link_id)
    if not deleted:
        fail(
            404,
            "not_found",
            "jira link not found for this goal",
            {"goal_id": goal_id, "link_id": link_id},
        )
    return {"ok": True, "goal_id": goal_id, "link_id": link_id}


# ---------------------------------------------------------------------------
# employees
# ---------------------------------------------------------------------------


@employees_router.get("")
def _list_employees(
    store: StoreDep,
    status: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    employees = _service(store).list_employees(status=status, limit=limit)
    return {"employees": [employee.model_dump() for employee in employees]}


__all__ = ["employees_router", "router"]
