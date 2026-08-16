"""Company-goals business rules (JG2-BE).

THE decisive rule of this lane: a ``short_term`` goal REQUIRES
``parent_goal_id``. It is enforced here, before the store is touched, so a
refused create/patch leaves the database byte-for-byte unchanged — validate
first, write second, never write-then-repair.

The rule binds the effective ROW, not the create path: patching a goal to
``short_term`` without a parent, or clearing the parent of a ``short_term``
goal, is refused exactly like a bad create.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from omniagentos.company_goals.models import (
    CompanyGoal,
    CompanyGoalJiraLink,
    CompanyGoalNotFound,
    CompanyGoalValidationError,
    Employee,
    validate_employee_shape,
    validate_goal_shape,
    validate_jira_link_shape,
)
from omniagentos.company_goals.store import CompanyGoalsStore

# Sentinel for PATCH: distinguishes "field absent" from "field set to null",
# which matters because parent_goal_id = null is itself a meaningful edit.
_UNSET: Any = object()


def _cross_company_errors(parent: dict[str, Any], org_company_id: str) -> list[str]:
    """A goal ladders up INSIDE its company; a foreign parent is not a ladder.

    Without this, a short_term goal can satisfy "has a parent" by pointing at
    another company's tree, which makes every per-company roll-up wrong while
    still looking valid row by row.
    """
    parent_company = str(parent.get("org_company_id") or "")
    if parent_company != org_company_id:
        return [
            f"parent_goal_id {parent.get('id')!r} belongs to company "
            f"{parent_company!r}, not {org_company_id!r} — goals ladder within one company"
        ]
    return []


class CompanyGoalsService:
    """Validation seam in front of :class:`CompanyGoalsStore`.

    Every write path in the API goes through here. The store stays a dumb DAL so
    the rules live in exactly one place.
    """

    def __init__(self, store: CompanyGoalsStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # employees
    # ------------------------------------------------------------------

    def list_employees(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[Employee]:
        return [
            Employee.from_row(row) for row in self._store.list_employees(status=status, limit=limit)
        ]

    def get_employee(self, employee_id: str) -> Employee | None:
        row = self._store.get_employee(employee_id)
        return None if row is None else Employee.from_row(row)

    def create_employee(
        self,
        *,
        name: str,
        role: str | None = None,
        jira_account_id: str | None = None,
        status: str = "active",
    ) -> Employee:
        errors = validate_employee_shape(name=name, status=status)
        if self._store.get_employee_by_name(name) is not None:
            errors.append(f"employee already exists: {name}")
        if errors:
            raise CompanyGoalValidationError(errors)
        return Employee.from_row(
            self._store.create_employee(
                name=name, role=role, jira_account_id=jira_account_id, status=status
            )
        )

    # ------------------------------------------------------------------
    # goals
    # ------------------------------------------------------------------

    def list_goals(
        self,
        *,
        org_company_id: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        parent_goal_id: str | None = None,
        limit: int | None = None,
    ) -> list[CompanyGoal]:
        rows = self._store.list_goals(
            org_company_id=org_company_id,
            horizon=horizon,
            status=status,
            parent_goal_id=parent_goal_id,
            limit=limit,
        )
        return [CompanyGoal.from_row(row) for row in rows]

    def get_goal(self, goal_id: str) -> CompanyGoal | None:
        row = self._store.get_goal(goal_id)
        return None if row is None else CompanyGoal.from_row(row)

    def create_goal(
        self,
        *,
        org_company_id: str,
        title: str,
        horizon: str,
        parent_goal_id: str | None = None,
        status: str = "active",
    ) -> CompanyGoal:
        errors = validate_goal_shape(
            title=title,
            horizon=horizon,
            parent_goal_id=parent_goal_id,
            status=status,
        )
        # Far-side existence checks BEFORE the write: a clean 400 beats letting
        # SQLite's FK raise IntegrityError out of the write transaction.
        if not org_company_id or not self._store.company_exists(org_company_id):
            errors.append(f"unknown org_company_id: {org_company_id!r}")
        if parent_goal_id:
            parent = self._store.get_goal(parent_goal_id)
            if parent is None:
                errors.append(f"unknown parent_goal_id: {parent_goal_id!r}")
            else:
                errors.extend(_cross_company_errors(parent, org_company_id))
        if errors:
            raise CompanyGoalValidationError(errors)

        return CompanyGoal.from_row(
            self._store.create_goal(
                org_company_id=org_company_id,
                title=title.strip(),
                horizon=horizon,
                parent_goal_id=parent_goal_id,
                status=status,
            )
        )

    def update_goal(
        self,
        goal_id: str,
        *,
        title: Any = _UNSET,
        horizon: Any = _UNSET,
        parent_goal_id: Any = _UNSET,
        status: Any = _UNSET,
    ) -> CompanyGoal:
        """Patch a goal. Only fields explicitly passed are changed.

        ``parent_goal_id=None`` CLEARS the parent (and is therefore refused for a
        goal that is — or becomes — ``short_term``); omitting it leaves the
        current parent alone.

        The merge, the validation and the write all happen INSIDE one write
        transaction (see :meth:`CompanyGoalsStore.update_goal`), because
        validating a snapshot read outside the transaction is exactly how two
        concurrent PATCHes strand a ``short_term`` goal without a parent.
        """
        patch: dict[str, Any] = {}
        for field, value in (
            ("title", title),
            ("horizon", horizon),
            ("parent_goal_id", parent_goal_id),
            ("status", status),
        ):
            if value is not _UNSET:
                patch[field] = value

        def prepare(current: dict[str, Any]) -> dict[str, Any]:
            """Runs under the write lock, on the row as it exists RIGHT NOW."""
            return self._validated_patch(goal_id, current, patch)

        updated = self._store.update_goal(goal_id, prepare)
        if updated is None:
            raise CompanyGoalNotFound(goal_id)
        return CompanyGoal.from_row(updated)

    def _validated_patch(
        self, goal_id: str, current: dict[str, Any], patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge + validate the EFFECTIVE row; raise to abort the transaction."""
        effective = {**current, **patch}
        errors = validate_goal_shape(
            title=effective.get("title"),
            horizon=effective.get("horizon"),
            parent_goal_id=effective.get("parent_goal_id"),
            status=effective.get("status"),
        )
        new_parent = effective.get("parent_goal_id")
        if new_parent and new_parent != current.get("parent_goal_id"):
            if new_parent == goal_id:
                errors.append("a goal cannot be its own parent")
            else:
                parent = self._store.get_goal(new_parent)
                if parent is None:
                    errors.append(f"unknown parent_goal_id: {new_parent!r}")
                else:
                    errors.extend(
                        _cross_company_errors(parent, str(effective.get("org_company_id") or ""))
                    )
                    if self._is_descendant(candidate_id=new_parent, ancestor_id=goal_id):
                        errors.append(
                            f"parent_goal_id {new_parent!r} is a descendant of {goal_id!r} "
                            "(would close a cycle)"
                        )
        if errors:
            raise CompanyGoalValidationError(errors)

        prepared = dict(patch)
        if isinstance(prepared.get("title"), str):
            prepared["title"] = prepared["title"].strip()
        return prepared

    def _is_descendant(self, *, candidate_id: str, ancestor_id: str) -> bool:
        """Walk candidate's parent chain; True when ``ancestor_id`` is on it.

        Bounded by a seen-set so a pre-existing cycle (or a hand-written row)
        can never spin this forever.
        """
        seen: set[str] = set()
        cursor: str | None = candidate_id
        while cursor and cursor not in seen:
            seen.add(cursor)
            if cursor == ancestor_id:
                return True
            row = self._store.get_goal(cursor)
            if row is None:
                return False
            cursor = row.get("parent_goal_id")
        return False

    # ------------------------------------------------------------------
    # goal <-> jira links
    # ------------------------------------------------------------------

    def list_jira_links(
        self, goal_id: str, *, limit: int | None = None
    ) -> list[CompanyGoalJiraLink]:
        if self._store.get_goal(goal_id) is None:
            raise CompanyGoalNotFound(goal_id)
        return [
            CompanyGoalJiraLink.from_row(row)
            for row in self._store.list_jira_links(goal_id, limit=limit)
        ]

    def create_jira_link(
        self,
        *,
        goal_id: str,
        jira_project_key: str,
        link_kind: str,
        jira_issue_key: str | None = None,
    ) -> CompanyGoalJiraLink:
        if self._store.get_goal(goal_id) is None:
            raise CompanyGoalNotFound(goal_id)
        errors = validate_jira_link_shape(
            jira_project_key=jira_project_key,
            jira_issue_key=jira_issue_key,
            link_kind=link_kind,
        )
        if errors:
            raise CompanyGoalValidationError(errors)

        # Mirror migration 098's partial unique indexes as a friendly 400 instead
        # of an IntegrityError; the indexes remain the real enforcement. Note the
        # issue-level check ignores the project key on purpose — an issue key
        # already names its project, so the SAME issue must not be linked twice
        # to one goal under a different (necessarily wrong) project key.
        for existing in self._store.list_jira_links(goal_id):
            if jira_issue_key is None:
                duplicate = (
                    existing["jira_issue_key"] is None
                    and existing["jira_project_key"] == jira_project_key
                )
            else:
                duplicate = existing["jira_issue_key"] == jira_issue_key
            if duplicate:
                errors.append(
                    "link already exists for "
                    f"goal={goal_id!r} project={existing['jira_project_key']!r} "
                    f"issue={jira_issue_key!r}"
                )
                break
        if errors:
            raise CompanyGoalValidationError(errors)

        try:
            row = self._store.create_jira_link(
                goal_id=goal_id,
                jira_project_key=jira_project_key,
                link_kind=link_kind,
                jira_issue_key=jira_issue_key,
            )
        except sqlite3.IntegrityError as exc:  # concurrent duplicate — index won
            raise CompanyGoalValidationError(
                [f"jira link rejected by the database: {exc}"]
            ) from exc
        return CompanyGoalJiraLink.from_row(row)

    def delete_jira_link(self, goal_id: str, link_id: str) -> bool:
        return self._store.delete_jira_link(link_id, goal_id=goal_id)
