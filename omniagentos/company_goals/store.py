"""SQLite data access for the company-goals spine (migration 098).

Composed over an already-migrated :class:`omniagentos.db.store.SqliteStore`,
exactly like :class:`omniagentos.scheduler.store.RoutinesStore`: this DAL owns
``employees``, ``company_goals`` and ``company_goal_jira_links`` EXCLUSIVELY and
reuses the base store's per-thread connection, lock, and write transaction
rather than opening a second connection.

The steward ``goals`` table (``007_steward.sql``) is NOT ours. It belongs to the
Horizon-4 steward and holds a different schema and different rows; nothing in
this module may read or write it. The table names below are the single seam
where that could go wrong, which is why they are named constants and why
``tests/goals/test_company_goals.py::test_store_never_touches_steward_goals_table``
pins the steward table's exact row-id set across company-goal writes
(counterfeit ``cf-steward-goals-repointed``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from omniagentos.company_goals.models import (
    EMPLOYEE_ID_PREFIX,
    GOAL_ID_PREFIX,
    JIRA_LINK_ID_PREFIX,
)
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore, _row, _rows

# Owned tables. NEVER point any of these at the steward 'goals' table.
EMPLOYEES_TABLE = "employees"
COMPANY_GOALS_TABLE = "company_goals"
JIRA_LINKS_TABLE = "company_goal_jira_links"

# Read-only far side of company_goals.org_company_id (owned by orgdims).
ORG_COMPANIES_TABLE = "org_companies"

_GOAL_UPDATE_FIELDS = frozenset({"title", "horizon", "parent_goal_id", "status"})
_EMPLOYEE_UPDATE_FIELDS = frozenset({"name", "role", "jira_account_id", "status"})

# Given the goal row as it exists INSIDE the write transaction, return the
# columns to write (or raise to abort the whole transaction).
GoalPatchPreparer = Callable[[dict[str, Any]], dict[str, Any]]


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        dal = cast("CompanyGoalsStore", args[0])
        with dal._store._lock:
            return method(*args, **kwargs)

    return wrapped


class CompanyGoalsStore:
    """Company-goals DAL composed over a configured :class:`SqliteStore`."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance — ``SqliteStore`` hands out one
        connection per thread, so a handle captured at construction time would
        silently interleave this DAL's statements into another thread's
        transaction (RoutinesStore carries the same warning).
        """
        return self._store._connection

    # ------------------------------------------------------------------
    # employees
    # ------------------------------------------------------------------

    @_serialized
    def create_employee(
        self,
        *,
        name: str,
        role: str | None = None,
        jira_account_id: str | None = None,
        status: str = "active",
        employee_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = employee_id or new_id(EMPLOYEE_ID_PREFIX)
        self._store._write(
            f"INSERT INTO {EMPLOYEES_TABLE} "
            "(id, name, role, jira_account_id, status, created_at) VALUES (?,?,?,?,?,?)",
            (row_id, name, role, jira_account_id, status, utc_now_iso()),
        )
        created = self.get_employee(row_id)
        assert created is not None
        return created

    @_serialized
    def ensure_employee(
        self,
        *,
        employee_id: str,
        name: str,
        role: str | None = None,
        jira_account_id: str | None = None,
        status: str = "active",
    ) -> bool:
        """Insert the row unless an id OR name conflict already covers it.

        ``ON CONFLICT DO NOTHING`` (no conflict target = any uniqueness
        conflict) rather than check-then-insert: two API processes booting at
        the same instant both see "absent", and the loser of a check-then-insert
        takes an IntegrityError on UNIQUE(name) — a crash in the startup path.
        Returns True when THIS call inserted the row.
        """
        inserted = self._store._write_count(
            f"INSERT INTO {EMPLOYEES_TABLE} "
            "(id, name, role, jira_account_id, status, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (employee_id, name, role, jira_account_id, status, utc_now_iso()),
        )
        return inserted > 0

    @_serialized
    def get_employee(self, employee_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {EMPLOYEES_TABLE} WHERE id = ?", (employee_id,)
            ).fetchone()
        )

    @_serialized
    def get_employee_by_name(self, name: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {EMPLOYEES_TABLE} WHERE name = ?", (name,)
            ).fetchone()
        )

    @_serialized
    def list_employees(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {EMPLOYEES_TABLE}"
        parameters: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            parameters.append(status)
        sql += " ORDER BY name ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_serialized
    def update_employee(self, employee_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        unknown = values.keys() - _EMPLOYEE_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown employee columns: {', '.join(sorted(unknown))}")
        if not values:
            return self.get_employee(employee_id)
        assignments = ", ".join(f"{column} = ?" for column in sorted(values))
        parameters = [values[column] for column in sorted(values)]
        parameters.append(employee_id)
        changed = self._store._write_count(
            f"UPDATE {EMPLOYEES_TABLE} SET {assignments} WHERE id = ?", parameters
        )
        if changed == 0:
            return None
        return self.get_employee(employee_id)

    # ------------------------------------------------------------------
    # company goals
    # ------------------------------------------------------------------

    @_serialized
    def company_exists(self, org_company_id: str) -> bool:
        """Read-only probe of the FK far side (org_companies is owned by orgdims)."""
        return (
            self._connection.execute(
                f"SELECT 1 FROM {ORG_COMPANIES_TABLE} WHERE id = ?", (org_company_id,)
            ).fetchone()
            is not None
        )

    @_serialized
    def create_goal(
        self,
        *,
        org_company_id: str,
        title: str,
        horizon: str,
        parent_goal_id: str | None = None,
        status: str = "active",
        goal_id: str | None = None,
        owner_employee_id: str | None = None,
    ) -> dict[str, Any]:
        # ``owner_employee_id`` is migration 123's accountable-person column;
        # additive with a None default so every pre-123 caller is unchanged.
        row_id = goal_id or new_id(GOAL_ID_PREFIX)
        now = utc_now_iso()
        self._store._write(
            f"INSERT INTO {COMPANY_GOALS_TABLE} "
            "(id, org_company_id, title, horizon, parent_goal_id, status, owner_employee_id, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                org_company_id,
                title,
                horizon,
                parent_goal_id,
                status,
                owner_employee_id,
                now,
                now,
            ),
        )
        created = self.get_goal(row_id)
        assert created is not None
        return created

    @_serialized
    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {COMPANY_GOALS_TABLE} WHERE id = ?", (goal_id,)
            ).fetchone()
        )

    @_serialized
    def list_goals(
        self,
        *,
        org_company_id: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        parent_goal_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if org_company_id is not None:
            clauses.append("org_company_id = ?")
            parameters.append(org_company_id)
        if horizon is not None:
            clauses.append("horizon = ?")
            parameters.append(horizon)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if parent_goal_id is not None:
            clauses.append("parent_goal_id = ?")
            parameters.append(parent_goal_id)
        sql = f"SELECT * FROM {COMPANY_GOALS_TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_serialized
    def update_goal(self, goal_id: str, prepare: GoalPatchPreparer) -> dict[str, Any] | None:
        """Re-read → prepare/validate → UPDATE, all inside ONE ``BEGIN IMMEDIATE``.

        This is the ONLY goal-update path, deliberately: a
        read-merge-validate-write sequence whose read happens outside the write
        transaction admits a lost-update race that defeats the lane's decisive
        invariant. Two concurrent PATCHes — one flipping ``horizon`` to
        ``short_term``, one clearing ``parent_goal_id`` — each validate a stale
        snapshot, each touch a DIFFERENT column, and together commit a
        parentless ``short_term`` row that neither writer ever proposed.

        ``prepare`` receives the row as it exists INSIDE the transaction and
        returns the columns to write, or raises to abort (the transaction rolls
        back and nothing is written). Because SQLite's write lock is already
        held when the row is re-read, that read is authoritative against every
        other writer — this process and any other — which is what a bare
        ``WHERE id = ?`` cannot give. A CAS on ``updated_at`` is NOT a
        substitute: the token is second-resolution, so two writes inside the
        same second are indistinguishable (the defect migration 097 fixed for
        routines by adding an integer revision).

        Returns None when the row does not exist.
        """

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {COMPANY_GOALS_TABLE} WHERE id = ?", (goal_id,)
                ).fetchone()
            )
            if current is None:
                return None
            values = prepare(current)
            unknown = values.keys() - _GOAL_UPDATE_FIELDS
            if unknown:
                raise ValueError(f"unknown goal columns: {', '.join(sorted(unknown))}")
            if values:
                columns = sorted(values)
                assignments = ", ".join(f"{column} = ?" for column in columns)
                parameters: list[Any] = [values[column] for column in columns]
                parameters.append(utc_now_iso())
                parameters.append(goal_id)
                connection.execute(
                    f"UPDATE {COMPANY_GOALS_TABLE} SET {assignments}, updated_at = ? WHERE id = ?",
                    parameters,
                )
            return _row(
                connection.execute(
                    f"SELECT * FROM {COMPANY_GOALS_TABLE} WHERE id = ?", (goal_id,)
                ).fetchone()
            )

        return self._store._execute_write_txn(body, op="company_goals.update_goal")

    # ------------------------------------------------------------------
    # goal <-> jira links
    # ------------------------------------------------------------------

    @_serialized
    def create_jira_link(
        self,
        *,
        goal_id: str,
        jira_project_key: str,
        link_kind: str,
        jira_issue_key: str | None = None,
        link_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = link_id or new_id(JIRA_LINK_ID_PREFIX)
        self._store._write(
            f"INSERT INTO {JIRA_LINKS_TABLE} "
            "(id, goal_id, jira_project_key, jira_issue_key, link_kind, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (row_id, goal_id, jira_project_key, jira_issue_key, link_kind, utc_now_iso()),
        )
        created = self.get_jira_link(row_id)
        assert created is not None
        return created

    @_serialized
    def get_jira_link(self, link_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {JIRA_LINKS_TABLE} WHERE id = ?", (link_id,)
            ).fetchone()
        )

    @_serialized
    def list_jira_links(self, goal_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {JIRA_LINKS_TABLE} WHERE goal_id = ? ORDER BY created_at ASC, id ASC"
        parameters: list[Any] = [goal_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_serialized
    def delete_jira_link(self, link_id: str, *, goal_id: str | None = None) -> bool:
        """Delete one link. ``goal_id`` scopes the delete to its owning goal."""
        if goal_id is None:
            return (
                self._store._write_count(f"DELETE FROM {JIRA_LINKS_TABLE} WHERE id = ?", (link_id,))
                > 0
            )
        return (
            self._store._write_count(
                f"DELETE FROM {JIRA_LINKS_TABLE} WHERE id = ? AND goal_id = ?",
                (link_id, goal_id),
            )
            > 0
        )
