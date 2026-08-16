"""SQLite data access for the durable plans spine (migration 100, LANE A / t/cb-plans).

Composed over an already-migrated :class:`omniagentos.db.store.SqliteStore`,
exactly like :class:`omniagentos.company_goals.store.CompanyGoalsStore`: this
DAL owns the ``plans`` table EXCLUSIVELY and reuses the base store's per-thread
connection, lock, and write transaction rather than opening a second connection.
Callers pass the store they already have (in the API: the process-wide
``get_store()`` singleton); constructing a ``SqliteStore`` per call would re-run
the whole migration ladder on every request, and ``SqliteStore(":memory:")``
would additionally build a brand-new EMPTY database each time.

This module is the one seam a plan writer must not bypass, and it enforces
three things on EVERY write, not just on create:

* the closed status vocabulary, validated against the MERGED post-patch row
  inside the write transaction (:func:`validate_plan_shape`);
* terminal states that are actually terminal, with ``approved -> superseded``
  as the single allowed exit (:func:`validate_plan_transition`);
* at most one ``approved`` plan per source, so "which plan is current for this
  chat/card" always has exactly one answer.

Partial unique indexes carry the non-terminal invariant at the schema level: at
most one draft|ready plan per chat_id and one per board_task_id, so two planners
racing on the same source cannot both leave a pending plan behind — the loser
takes sqlite3.IntegrityError at COMMIT. That binds any writer that roots a plan
on a chat or a card; see the scope note in
``omniagentos/db/migrations/100_plans_durable.sql`` for which paths do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore, _row, _rows
from omniagentos.plans.models import (
    PLAN_ID_PREFIX,
    PlanValidationError,
    validate_plan_shape,
    validate_plan_transition,
)

# Owned tables.
PLANS_TABLE = "plans"

# Read-only far sides.
CHATS_TABLE = "chats"
BOARD_TASKS_TABLE = "board_tasks"
ORG_COMPANIES_TABLE = "org_companies"

# The columns a prepare-callback may write. Anything else is a caller bug and
# aborts the transaction (never a silently-dropped field).
_PLAN_UPDATE_FIELDS = frozenset(
    {
        "chat_id",
        "board_task_id",
        "status",
        "plan_json",
        "content_md",
        "goal",
        "harness",
        "execute_mode",
        "speed",
        "route_json",
        "route_target_name",
        "decided_by",
        "decided_at",
        "reason",
        "org_company_id",
        "work_folder",
        "execution_metadata",
    }
)

# Columns re-validated as a whole row after a patch is merged.
_PLAN_SHAPE_FIELDS = (
    "chat_id",
    "board_task_id",
    "version",
    "status",
    "plan_json",
    "org_company_id",
    "work_folder",
)

# Given the plan row as it exists INSIDE the write transaction, return the
# columns to write (or raise to abort the whole transaction).
PlanPatchPreparer = Callable[[dict[str, Any]], dict[str, Any]]


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        dal = cast("PlansStore", args[0])
        with dal._store._lock:
            return method(*args, **kwargs)

    return wrapped


class PlansStore:
    """Plans DAL composed over a configured :class:`SqliteStore`."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance — ``SqliteStore`` hands out one
        connection per thread, so a handle captured at construction time would
        silently interleave this DAL's statements into another thread's
        transaction.
        """
        return self._store._connection

    # ------------------------------------------------------------------
    # plans
    # ------------------------------------------------------------------

    @_serialized
    def create_plan(
        self,
        *,
        chat_id: str | None = None,
        board_task_id: str | None = None,
        version: int = 1,
        status: str = "draft",
        plan_json: str,
        content_md: str | None = None,
        goal: str | None = None,
        harness: str | None = None,
        execute_mode: str | None = None,
        speed: str | None = None,
        route_json: str | None = None,
        route_target_name: str | None = None,
        org_company_id: str | None = None,
        work_folder: str | None = None,
        execution_metadata: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new plan row.

        Validates shape and raises PlanValidationError if validation fails.
        May raise sqlite3.IntegrityError if a non-terminal (draft|ready) plan
        already exists for this source (chat_id or board_task_id).
        """
        errors = validate_plan_shape(
            chat_id=chat_id,
            board_task_id=board_task_id,
            version=version,
            status=status,
            plan_json=plan_json,
            org_company_id=org_company_id,
            work_folder=work_folder,
        )
        if errors:
            raise PlanValidationError(errors)

        row_id = plan_id or new_id(PLAN_ID_PREFIX)
        now = utc_now_iso()
        self._store._write(
            f"INSERT INTO {PLANS_TABLE} "
            "(id, chat_id, board_task_id, version, status, plan_json, content_md, "
            "goal, harness, execute_mode, speed, route_json, route_target_name, "
            "org_company_id, work_folder, execution_metadata, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                chat_id,
                board_task_id,
                version,
                status,
                plan_json,
                content_md,
                goal,
                harness,
                execute_mode,
                speed,
                route_json,
                route_target_name,
                org_company_id,
                work_folder,
                execution_metadata,
                now,
                now,
            ),
        )
        created = self.get_plan(row_id)
        assert created is not None
        return created

    @_serialized
    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Retrieve a single plan by id."""
        return _row(
            self._connection.execute(
                f"SELECT * FROM {PLANS_TABLE} WHERE id = ?", (plan_id,)
            ).fetchone()
        )

    @_serialized
    def list_plans(
        self,
        *,
        chat_id: str | None = None,
        board_task_id: str | None = None,
        org_company_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List plans matching the given filters.

        Results are ordered by created_at ascending, then id ascending.
        """
        clauses: list[str] = []
        parameters: list[Any] = []

        if chat_id is not None:
            clauses.append("chat_id = ?")
            parameters.append(chat_id)
        if board_task_id is not None:
            clauses.append("board_task_id = ?")
            parameters.append(board_task_id)
        if org_company_id is not None:
            clauses.append("org_company_id = ?")
            parameters.append(org_company_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)

        sql = f"SELECT * FROM {PLANS_TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"

        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))

        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_serialized
    def get_latest_plan_for_chat(self, chat_id: str) -> dict[str, Any] | None:
        """Get the most recent plan for a chat, regardless of status."""
        return _row(
            self._connection.execute(
                f"SELECT * FROM {PLANS_TABLE} "
                "WHERE chat_id = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        )

    @_serialized
    def get_nonterminal_plan_for_chat(self, chat_id: str) -> dict[str, Any] | None:
        """Get the current draft or ready plan for a chat, if any.

        Returns None if the chat has no non-terminal plan.
        """
        return _row(
            self._connection.execute(
                f"SELECT * FROM {PLANS_TABLE} "
                "WHERE chat_id = ? AND status IN ('draft', 'ready') "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        )

    def _validate_merged(
        self,
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        current: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        """Validate the row as it WILL exist after the patch, inside the txn.

        Validating the patch alone is not enough — a partial patch is only ever
        meaningful merged onto the row it lands on, and the row can only be read
        authoritatively while the write lock is held. Raising here aborts the
        whole transaction, so a refused update leaves the row byte-identical.
        """
        merged = {**current, **values}
        errors = validate_plan_shape(**{key: merged.get(key) for key in _PLAN_SHAPE_FIELDS})
        errors += validate_plan_transition(current.get("status"), merged.get("status"))
        if errors:
            raise PlanValidationError(errors)

        # One current (approved) plan per source. Without this two plans on one
        # chat can both read 'approved' and nothing says which one is live —
        # exactly the "you approve the wrong plan" failure. Checked inside the
        # write transaction, so it is race-safe rather than advisory.
        if merged.get("status") == "approved" and current.get("status") != "approved":
            for column in ("chat_id", "board_task_id"):
                source = merged.get(column)
                if not source:
                    continue
                clash = connection.execute(
                    f"SELECT id FROM {PLANS_TABLE} "
                    f"WHERE {column} = ? AND status = 'approved' AND id <> ? LIMIT 1",
                    (source, plan_id),
                ).fetchone()
                if clash is not None:
                    raise PlanValidationError(
                        [
                            f"{column} {source} already has an approved plan "
                            f"({clash['id']}); supersede it before approving another"
                        ]
                    )

    @_serialized
    def update_plan(self, plan_id: str, prepare: PlanPatchPreparer) -> dict[str, Any] | None:
        """Re-read → prepare/validate → UPDATE, all inside ONE ``BEGIN IMMEDIATE``.

        This is the ONLY plan-update path, deliberately: a read-merge-validate-
        write sequence whose read happens outside the write transaction admits
        a lost-update race that defeats the lane's decisive invariant. Two
        concurrent PATCH operations must not silently overwrite each other's
        decisions.

        ``prepare`` receives the row as it exists INSIDE the transaction and
        returns the columns to write, or raises to abort (the transaction rolls
        back and nothing is written). Because SQLite's write lock is already
        held when the row is re-read, that read is authoritative against every
        other writer.

        The MERGED result is then validated by this store, not by the callback:
        a vocabulary that only binds callers who remember to call it is not a
        vocabulary. A violation raises :class:`PlanValidationError` and the row
        is left unchanged.

        Returns None when the row does not exist.
        """

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {PLANS_TABLE} WHERE id = ?", (plan_id,)
                ).fetchone()
            )
            if current is None:
                return None
            values = prepare(current)
            unknown = values.keys() - _PLAN_UPDATE_FIELDS
            if unknown:
                raise ValueError(f"unknown plan columns: {', '.join(sorted(unknown))}")
            self._validate_merged(connection, plan_id=plan_id, current=current, values=values)
            if values:
                columns = sorted(values)
                assignments = ", ".join(f"{column} = ?" for column in columns)
                parameters: list[Any] = [values[column] for column in columns]
                parameters.append(utc_now_iso())
                parameters.append(plan_id)
                connection.execute(
                    f"UPDATE {PLANS_TABLE} SET {assignments}, updated_at = ? WHERE id = ?",
                    parameters,
                )
            return _row(
                connection.execute(
                    f"SELECT * FROM {PLANS_TABLE} WHERE id = ?", (plan_id,)
                ).fetchone()
            )

        return self._store._execute_write_txn(body, op="plans.update_plan")

    @_serialized
    def approve_plan(
        self, plan_id: str, *, decided_by: str, reason: str | None = None
    ) -> dict[str, Any] | None:
        """Transition a plan to 'approved' status.

        Records who approved it (decided_by) and optionally a reason. Returns
        the updated plan or None if not found.
        """

        def prepare(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "approved",
                "decided_by": decided_by,
                "decided_at": utc_now_iso(),
                "reason": reason,
            }

        return self.update_plan(plan_id, prepare)

    @_serialized
    def reject_plan(
        self, plan_id: str, *, decided_by: str, reason: str | None = None
    ) -> dict[str, Any] | None:
        """Transition a plan to 'rejected' status.

        Records who rejected it and a reason. Returns the updated plan or None
        if not found.
        """

        def prepare(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "rejected",
                "decided_by": decided_by,
                "decided_at": utc_now_iso(),
                "reason": reason or "",
            }

        return self.update_plan(plan_id, prepare)

    @_serialized
    def supersede_plan(self, plan_id: str, *, reason: str | None = None) -> dict[str, Any] | None:
        """Transition a plan to 'superseded' status.

        Typically called when a newer version is approved. Records a reason
        if provided. Returns the updated plan or None if not found.
        """

        def prepare(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "superseded",
                "decided_at": utc_now_iso(),
                "reason": reason,
            }

        return self.update_plan(plan_id, prepare)
