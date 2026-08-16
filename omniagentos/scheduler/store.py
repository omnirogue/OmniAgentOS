"""SQLite data access for routines ("loops with teeth").

Composed over an already-migrated :class:`omniagentos.db.store.SqliteStore`,
mirroring :class:`omniagentos.steward.store.StewardStore`'s pattern: this
class owns the routines/routine_runs tables exclusively and reuses the base
store's connection, lock, and transaction helpers rather than opening a
second connection.

Every write that can change a routine's declared shape (create/update) goes
through :func:`omniagentos.scheduler.routines.validate_routine` FIRST — this
is the one seam a caller cannot bypass to persist a routine missing its gate
or hard stop-condition, whether the caller is the HTTP API, a test, or a
future importer/runner.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines import (
    NEUTRAL_STOP_REASONS,
    OUTCOME_CLASSES,
    OUTCOME_NEUTRAL,
    SELF_REPORT_COLUMN,
    UNGATEABLE_STOP_REASONS,
    RoutineValidationError,
    _is_non_vacuous_gate_command,
    classify_run_outcome,
    loop_gate_errors,
    should_auto_pause_on_settled,
    validate_routine,
)
from omniagentos.scheduler.routines import acceptance_rate as _acceptance_rate

_ROUTINE_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "trigger_type",
        "trigger_config",
        "task_template",
        "gate_type",
        "gate_config",
        "hard_cap_type",
        "hard_cap_value",
        "notification_target",
        "status",
        "auto_pause_reason",
        "last_fired",
        "created_at",
        "updated_at",
        # Migration F (LOOPS-1) — advisory metadata; nullable.
        "scope",
        "purpose",
        # Migration 116 (M1) — the project this routine's work belongs to.
        # Nullable and never defaulted; NULL means unscoped.
        "project_id",
    }
)


def _normalized_project_id(value: Any) -> str | None:
    """Blank project scope is NULL (M1).

    ``project_id = ''`` names no project, so persisting it would create a third
    state between "unscoped" and "scoped to P" that no reader can act on — and
    the C11 broker already collapses blank to NULL when it checks a grant's
    binding, so the write side collapses it too and the two agree.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Schema CHECKs still require enum-shaped engine columns even for drafts
# (Migration F only adds scope/purpose). Disabled drafts that omit engine
# fields get these placeholders so the row can persist; validate_routine's
# draft exemption skips shape checks, and the D5 enable guard re-validates
# as-active before flipping status.
_DRAFT_ENGINE_DEFAULTS: dict[str, Any] = {
    "trigger_type": "event",
    "trigger_config": {},
    "task_template": {},
    "gate_type": "exit_code",
    "gate_config": {},
    "hard_cap_type": "max_iterations",
    "hard_cap_value": 1.0,
    "notification_target": {},
}
_ROUTINE_RUN_FIELDS = frozenset(
    {
        "routine_id",
        "run_id",
        "iteration",
        "gate_passed",
        "accepted",
        "cost_usd",
        "stop_reason",
        "notes",
        "started_at",
        "finished_at",
        "created_at",
        # Migration 103 — the three-valued run outcome. Optional: when a caller
        # omits it, :func:`classify_run_outcome` derives it from the row.
        "outcome_class",
        # Migration 104 — what the executor CLAIMED about its own tick, verbatim.
        # A claim, never a verdict: gate_passed stays reserved for what an
        # executed gate found. Its presence marks a run this process executed
        # itself (no `runs` row), which is how settle_pending finds it.
        SELF_REPORT_COLUMN,
    }
)


class RoutineRunAlreadySettled(RuntimeError):
    """Raised when attempting to settle a routine run that has already been settled."""


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        routines = cast("RoutinesStore", args[0])
        with routines._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _refuse_active_mock_harness(
    status: Any,
    task_template: Any,
    gate_config: Any = None,
    *,
    allow_mock_with_real_gate: bool = False,
) -> None:
    """D5 store belt: refuse active rows without a real harness.

    Unset harness is always refused for active rows. ``mock`` is refused on
    status flips (enable path) but may be allowed on create when the seeder
    ships a non-vacuous objective gate (code-reviewed ensure_* pattern).
    """
    if status != "active":
        return
    harness = ""
    if isinstance(task_template, dict):
        harness = str(task_template.get("harness") or "").strip()
    elif isinstance(task_template, str) and task_template:
        try:
            parsed = json.loads(task_template)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            harness = str(parsed.get("harness") or "").strip()
    if harness and harness != "mock":
        return
    if harness == "mock" and allow_mock_with_real_gate:
        cmd = ""
        if isinstance(gate_config, dict):
            cmd = str(gate_config.get("command") or "")
        if cmd and _is_non_vacuous_gate_command(cmd):
            return
    raise RoutineValidationError(
        [f"active routine cannot have harness={harness!r} (D5 store belt)"]
    )


def _refuse_ungated_loop_activation(row: Any) -> None:
    """No row may become ACTIVE as a loop it cannot be judged as.

    ``validate_routine`` is the one definition of the rule and this is not a
    second copy of it — :func:`loop_gate_errors` is imported and asked. What
    this adds is REACH: ``set_status``/``set_status_cas`` deliberately do not
    re-validate shape, so before this belt a gateless loop row could be created
    ``disabled`` (draft exemption) and then flipped active through the DAL,
    passing the rule exactly zero times. The HTTP enable route was already
    guarded; the DAL is what contradicted the "every write path" claim.

    Both JSON-string and dict column shapes are accepted because callers reach
    this with rows straight from :func:`_routine` (parsed) and, historically,
    with raw column values.
    """
    if not isinstance(row, dict):
        return
    errors = loop_gate_errors(
        _parse_json(row.get("task_template"), {}),
        row.get("gate_type"),
        _parse_json(row.get("gate_config"), {}),
    )
    if errors:
        raise RoutineValidationError(errors)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _checked(values: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")
    return values


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _routine(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["trigger_config"] = _parse_json(out.pop("trigger_config_json", None), {})
    out["task_template"] = _parse_json(out.pop("task_template_json", None), {})
    out["gate_config"] = _parse_json(out.pop("gate_config_json", None), {})
    out["notification_target"] = _parse_json(out.pop("notification_target_json", None), {})
    # scope/purpose appear only after Migration F; older DBs omit the keys.
    if "scope" not in out:
        out["scope"] = None
    if "purpose" not in out:
        out["purpose"] = None
    # project_id appears only after migration 116; older DBs omit the key, and a
    # missing key must read as unscoped rather than as an absent field.
    if "project_id" not in out:
        out["project_id"] = None
    return out


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _fill_draft_engine_defaults(values: dict[str, Any]) -> dict[str, Any]:
    """Fill schema-required engine columns for a disabled draft that omitted them."""
    out = dict(values)
    for key, default in _DRAFT_ENGINE_DEFAULTS.items():
        current = out.get(key)
        if current is None:
            out[key] = default
        elif key in {"trigger_config", "task_template", "gate_config", "notification_target"}:
            if not isinstance(current, dict):
                out[key] = default
            # empty dict is allowed for drafts (schema DEFAULT '{}')
        elif key == "hard_cap_value" and current is None:
            out[key] = default
    return out


def _count_settled_runs(
    connection: sqlite3.Connection,
    routine_id: str,
    limit: int = 100,
) -> tuple[int, int]:
    """Count settled and accepted runs for a routine (up to limit most recent).

    Returns (settled_runs, accepted_runs) where:
    - settled_runs: count of runs the gate actually ruled on
    - accepted_runs: count of settled runs where accepted = 1

    Only counts runs where finished_at IS NOT NULL (fully settled).
    Runs with no configured gate (gate_passed=NULL) are excluded from the denominator.
    So are runs whose ``stop_reason`` is in
    :data:`omniagentos.scheduler.routines.UNGATEABLE_STOP_REASONS` or
    :data:`omniagentos.scheduler.routines.NEUTRAL_STOP_REASONS` even when they
    carry a non-NULL gate_passed — the gate rendered no verdict on those, so they
    are a third bucket, neither accepted nor rejected.

    The stop_reason sets are a deliberate BACKSTOP, not the primary mechanism:
    the write path already stores a neutral run as gate_passed=NULL/accepted=NULL,
    which the first check drops. They exist so that a row written with a
    favourable boolean but a neutral reason — by a legacy caller, or by a
    regression that re-admits ``parked``/``idle`` as accepted — still cannot
    inflate the floor's numerator.
    """
    rows = connection.execute(
        "SELECT gate_passed, accepted, stop_reason FROM routine_runs "
        "WHERE routine_id = ? AND finished_at IS NOT NULL "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (routine_id, limit),
    ).fetchall()

    settled = 0
    accepted = 0
    for row in rows:
        gate_passed = row["gate_passed"]
        if gate_passed is None:
            continue
        reason = row["stop_reason"] or ""
        if reason in UNGATEABLE_STOP_REASONS or reason in NEUTRAL_STOP_REASONS:
            continue
        settled += 1
        if row["accepted"]:
            accepted += 1

    return settled, accepted


def _resolved_outcome_class(
    values: dict[str, Any],
    *,
    gate_passed: Any,
    accepted: Any,
    stop_reason: Any,
    finished_at: Any,
) -> str | None:
    """The outcome class to persist for one run — declared, or derived.

    A caller that KNOWS the class (the built-in/loop path, which is the only
    place that can tell "parked awaiting a human" apart from "the gate said no")
    declares it. Everyone else gets it derived from the row, so a legacy caller
    cannot leave the column NULL on a settled run and quietly re-open the
    two-valued collapse this taxonomy exists to close.
    """
    declared = values.get("outcome_class")
    if declared is not None:
        text = str(declared)
        if text not in OUTCOME_CLASSES:
            raise ValueError(f"unknown outcome_class: {text!r}")
        return text
    return classify_run_outcome(
        gate_passed=gate_passed,
        accepted=accepted,
        stop_reason=stop_reason,
        finished_at=finished_at,
    )


def _routine_run(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("gate_passed", "accepted"):
        if out.get(key) is not None:
            out[key] = bool(out[key])
    return out


class RoutinesStore:
    """Routines DAL composed over an already configured :class:`SqliteStore`."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    # --- routines ---

    @_serialized
    def create_routine(self, routine: dict[str, Any]) -> dict[str, Any]:
        """Insert a new routine. Raises RoutineValidationError if it's missing
        its trigger, task template, objective gate, hard stop-condition, or
        notification target (unless ``status='disabled'`` — draft exemption).

        ``project_id`` (M1, migration 116) scopes the routine. It is optional
        and NOT defaulted: absent or blank stores NULL, meaning "this routine
        is not scoped to a project" — the honest reading of every routine that
        predates the column, and the same NULL semantics migration 115 gave the
        grant side. Nothing here derives a project from the name or the task
        template, because a derived project is an invented one.
        """
        values = dict(_checked(dict(routine), _ROUTINE_FIELDS))
        # Default status matches the HTTP create default so validation sees the
        # same effective status the row will be stored with.
        if "status" not in values or values.get("status") is None:
            values["status"] = "active"
        validate_routine(values)
        # M2 store belt: refuse active+mock/unset at create, not only on HTTP.
        _refuse_active_mock_harness(
            values.get("status"),
            values.get("task_template"),
            values.get("gate_config"),
            allow_mock_with_real_gate=True,
        )
        if values["status"] == "disabled":
            values = _fill_draft_engine_defaults(values)
        now = utc_now_iso()
        routine_id = str(values.get("id") or new_id("rtn"))
        columns = _table_columns(self._connection, "routines")
        has_meta = "scope" in columns and "purpose" in columns
        # Column-sniffed exactly like scope/purpose: a database that has not
        # reached migration 116 must still be able to create a routine, and it
        # simply cannot record a project for it.
        has_project = "project_id" in columns

        col_names = [
            "id",
            "name",
            "description",
            "trigger_type",
            "trigger_config_json",
            "task_template_json",
            "gate_type",
            "gate_config_json",
            "hard_cap_type",
            "hard_cap_value",
            "notification_target_json",
            "status",
            "auto_pause_reason",
            "created_at",
            "updated_at",
        ]
        parameters: list[Any] = [
            routine_id,
            values["name"],
            values.get("description", ""),
            values["trigger_type"],
            _json(values.get("trigger_config") or {}),
            _json(values.get("task_template") or {}),
            values["gate_type"],
            _json(values.get("gate_config") or {}),
            values["hard_cap_type"],
            float(values["hard_cap_value"]),
            _json(values.get("notification_target") or {}),
            values.get("status", "active"),
            values.get("auto_pause_reason", ""),
            values.get("created_at", now),
            values.get("updated_at", now),
        ]
        if has_meta:
            col_names.extend(["scope", "purpose"])
            scope = values.get("scope")
            purpose = values.get("purpose")
            parameters.append(None if scope in (None, "") else scope)
            parameters.append(None if purpose in (None, "") else purpose)
        if has_project:
            col_names.append("project_id")
            parameters.append(_normalized_project_id(values.get("project_id")))

        placeholders = ", ".join("?" for _ in col_names)
        self._store._write(
            f"INSERT INTO routines ({', '.join(col_names)}) VALUES ({placeholders})",
            parameters,
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM routines WHERE id = ?", (routine_id,)
            ).fetchone()
        )
        assert row is not None
        return _routine(row)

    @_serialized
    def list_routines(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM routines ORDER BY created_at ASC, id ASC"
                ).fetchall()
            )
        else:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM routines WHERE status = ? ORDER BY created_at ASC, id ASC",
                    (status,),
                ).fetchall()
            )
        return [_routine(row) for row in rows]

    @_serialized
    def get_routine(self, routine_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                "SELECT * FROM routines WHERE id = ?", (routine_id,)
            ).fetchone()
        )
        return None if row is None else _routine(row)

    @_serialized
    def update_routine(self, routine_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Partially update a routine. The MERGED result is re-validated against
        its MERGED status — so activate-via-update cannot leak the draft
        exemption (LOOPS1-E2 / store.py validate-merged invariant)."""
        current = self.get_routine(routine_id)
        if current is None:
            return None
        updates = dict(_checked(dict(fields), _ROUTINE_FIELDS - {"id", "created_at"}))
        merged = {**current, **updates}
        # Validate the merged row against its merged status (no exemption leak).
        validate_routine(merged)
        # D5 store-side: never persist active + mock/unset harness (closes
        # concurrent update_routine races that bypass the route CAS path).
        if merged.get("status") == "active":
            template = merged.get("task_template") or {}
            harness = ""
            if isinstance(template, dict):
                harness = str(template.get("harness") or "").strip()
            if not harness or harness == "mock":
                raise RoutineValidationError(
                    [f"active routine cannot have harness={harness!r} (D5)"]
                )
        if merged.get("status") == "disabled":
            # Keep schema CHECKs happy if an update cleared engine fields on a draft.
            filled = _fill_draft_engine_defaults(merged)
            for key, value in filled.items():
                if key in _DRAFT_ENGINE_DEFAULTS and merged.get(key) in (None,):
                    updates.setdefault(key, value)
                    merged[key] = value
        now = utc_now_iso()
        set_clauses = []
        parameters: list[Any] = []
        columns = _table_columns(self._connection, "routines")
        column_map: dict[str, tuple[str, Any]] = {
            "name": ("name", lambda v: v),
            "description": ("description", lambda v: v),
            "trigger_type": ("trigger_type", lambda v: v),
            "trigger_config": ("trigger_config_json", _json),
            "task_template": ("task_template_json", _json),
            "gate_type": ("gate_type", lambda v: v),
            "gate_config": ("gate_config_json", _json),
            "hard_cap_type": ("hard_cap_type", lambda v: v),
            "hard_cap_value": ("hard_cap_value", float),
            "notification_target": ("notification_target_json", _json),
            "status": ("status", lambda v: v),
            "auto_pause_reason": ("auto_pause_reason", lambda v: v),
            "last_fired": ("last_fired", lambda v: v),
        }
        if "scope" in columns:
            column_map["scope"] = (
                "scope",
                lambda v: None if v in (None, "") else v,
            )
        if "purpose" in columns:
            column_map["purpose"] = (
                "purpose",
                lambda v: None if v in (None, "") else v,
            )
        for key, value in updates.items():
            if key not in column_map:
                # Meta fields on a pre-Migration-F DB are ignored (no column).
                continue
            column, encode = column_map[key]
            set_clauses.append(f"{column} = ?")
            parameters.append(encode(value))
        if not set_clauses:
            return self.get_routine(routine_id)
        set_clauses.append("updated_at = ?")
        parameters.append(now)
        set_clauses.append("revision = revision + 1")
        parameters.append(routine_id)
        self._store._write(
            f"UPDATE routines SET {', '.join(set_clauses)} WHERE id = ?",
            parameters,
        )
        return self.get_routine(routine_id)

    @_serialized
    def set_status(
        self, routine_id: str, status: str, *, reason: str = ""
    ) -> dict[str, Any] | None:
        """Enable/disable/auto-pause a routine WITHOUT re-validating its shape
        (a routine that was valid stays valid; only its lifecycle changes).

        One exception, and it is not a shape re-validation: a row whose work is
        a LOOP tick may not be made ACTIVE unless it declares a gate that can
        judge that tick (:func:`_refuse_ungated_loop_activation`). "Was valid
        stays valid" assumes the row was validated as what it is now, and a
        gateless loop row seeded ``disabled`` never was — that combination is
        exactly how the rule was escaped before this belt existed.
        """
        if status == "active":
            _refuse_ungated_loop_activation(self.get_routine(routine_id))
        changed = self._store._write_count(
            "UPDATE routines SET status = ?, auto_pause_reason = ?, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?",
            (status, reason, utc_now_iso(), routine_id),
        )
        if changed == 0:
            return None
        return self.get_routine(routine_id)

    @_serialized
    def set_status_cas(
        self,
        routine_id: str,
        status: str,
        *,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Compare-and-swap status using the monotonic integer ``revision``.

        Returns the updated row on success, or ``None`` when the row is missing
        or ``revision`` no longer matches (concurrent mutation during D5
        proof). When flipping to active, re-reads the row and refuses mock/unset
        harness even if ``revision`` still matches (M1 belt).
        """
        current = self.get_routine(routine_id)
        if current is None:
            return None
        if current.get("revision") != expected_revision:
            return None
        if status == "active":
            _refuse_active_mock_harness(status, current.get("task_template"))
            _refuse_ungated_loop_activation(current)
        now = utc_now_iso()
        changed = self._store._write_count(
            "UPDATE routines SET status = ?, auto_pause_reason = ?, updated_at = ?, "
            "revision = revision + 1 WHERE id = ? AND revision = ?",
            (status, reason, now, routine_id, expected_revision),
        )
        if changed == 0:
            return None
        return self.get_routine(routine_id)

    @_serialized
    def update_routine_cas(
        self,
        routine_id: str,
        fields: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any] | None:
        """Partial update with integer-revision compare-and-swap (D5 PATCH).

        Same validation as :meth:`update_routine`, but the UPDATE only commits when
        ``revision`` still equals ``expected_revision``. Returns ``None`` on
        missing row or concurrency mismatch so callers refuse rather than commit
        a post-proof harness→mock race.
        """
        current = self.get_routine(routine_id)
        if current is None:
            return None
        if current.get("revision") != expected_revision:
            return None
        updates = dict(_checked(dict(fields), _ROUTINE_FIELDS - {"id", "created_at"}))
        merged = {**current, **updates}
        validate_routine(merged)
        if merged.get("status") == "active":
            template = merged.get("task_template") or {}
            harness = ""
            if isinstance(template, dict):
                harness = str(template.get("harness") or "").strip()
            if not harness or harness == "mock":
                raise RoutineValidationError(
                    [f"active routine cannot have harness={harness!r} (D5)"]
                )
        if merged.get("status") == "disabled":
            filled = _fill_draft_engine_defaults(merged)
            for key, value in filled.items():
                if key in _DRAFT_ENGINE_DEFAULTS and merged.get(key) in (None,):
                    updates.setdefault(key, value)
                    merged[key] = value
        now = utc_now_iso()
        set_clauses: list[str] = []
        parameters: list[Any] = []
        columns = _table_columns(self._connection, "routines")
        column_map: dict[str, tuple[str, Any]] = {
            "name": ("name", lambda v: v),
            "description": ("description", lambda v: v),
            "trigger_type": ("trigger_type", lambda v: v),
            "trigger_config": ("trigger_config_json", _json),
            "task_template": ("task_template_json", _json),
            "gate_type": ("gate_type", lambda v: v),
            "gate_config": ("gate_config_json", _json),
            "hard_cap_type": ("hard_cap_type", lambda v: v),
            "hard_cap_value": ("hard_cap_value", float),
            "notification_target": ("notification_target_json", _json),
            "status": ("status", lambda v: v),
            "auto_pause_reason": ("auto_pause_reason", lambda v: v),
            "last_fired": ("last_fired", lambda v: v),
        }
        if "scope" in columns:
            column_map["scope"] = (
                "scope",
                lambda v: None if v in (None, "") else v,
            )
        if "purpose" in columns:
            column_map["purpose"] = (
                "purpose",
                lambda v: None if v in (None, "") else v,
            )
        for key, value in updates.items():
            if key not in column_map:
                continue
            column, encode = column_map[key]
            set_clauses.append(f"{column} = ?")
            parameters.append(encode(value))
        if not set_clauses:
            return self.get_routine(routine_id)
        set_clauses.append("updated_at = ?")
        parameters.append(now)
        set_clauses.append("revision = revision + 1")
        parameters.extend([routine_id, expected_revision])
        changed = self._store._write_count(
            f"UPDATE routines SET {', '.join(set_clauses)} WHERE id = ? AND revision = ?",
            parameters,
        )
        if changed == 0:
            return None
        return self.get_routine(routine_id)

    def record_fired(self, routine_id: str, fired_at: str | None = None) -> dict[str, Any] | None:
        """Stamp ``last_fired`` after a tick actually fires a routine.

        Deliberately unvalidated (mirrors :meth:`set_status`): firing never
        changes a routine's declared shape, so there is nothing to
        re-validate — only its DUE-check bookkeeping moves forward."""
        changed = self._store._write_count(
            "UPDATE routines SET last_fired = ?, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?",
            (fired_at or utc_now_iso(), utc_now_iso(), routine_id),
        )
        if changed == 0:
            return None
        return self.get_routine(routine_id)

    @_serialized
    def delete_routine(self, routine_id: str) -> bool:
        return self._store._write_count("DELETE FROM routines WHERE id = ?", (routine_id,)) > 0

    # --- routine runs ---

    def supports_self_report(self) -> bool:
        """True when this schema can carry a built-in's self-report (migration 104).

        Probed, never assumed. A deploy where the code is ahead of ``make
        migrate`` must not record runs whose claim is silently dropped on INSERT
        and which therefore sit pending forever — invisible non-settlement is
        the disease this lane exists to cure, not a tolerable transitional
        state. Callers that get ``False`` settle the tick inline instead (see
        ``routines_tick._fire``), with the same composition an absent gate
        produces: no verdict, no acceptance.
        """
        return SELF_REPORT_COLUMN in _table_columns(self._connection, "routine_runs")

    @_serialized
    def record_run(self, routine_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Record one routine firing/iteration and roll its cost/acceptance up
        onto the parent routine, auto-pausing it if the acceptance rate has
        fallen below the floor (see :func:`should_auto_pause`)."""
        values = dict(_checked(dict(run), _ROUTINE_RUN_FIELDS - {"routine_id"}))
        now = utc_now_iso()
        # ISSUE-8 (Sol review, seam 1): a caller — chiefly the public
        # POST /routines/{id}/runs route — can genuinely not know a run's
        # cost yet. `values.get("cost_usd", 0) or 0` used to fold that
        # (an omitted OR an explicit None) into a known, exact zero,
        # collapsing "unknown" into "free" at the very first write — the same
        # fatal class this migration exists to remove, just one seam
        # upstream of the one it originally closed. None now propagates as
        # unknown all the way to both the child row (routine_runs.cost_usd,
        # nullable since migration 120) and the parent rollup below.
        _cost_usd_raw = values.get("cost_usd")
        cost_usd: float | None = None if _cost_usd_raw is None else float(_cost_usd_raw)
        accepted = values.get("accepted")
        gate_passed = values.get("gate_passed")
        outcome_class = _resolved_outcome_class(
            values,
            gate_passed=gate_passed,
            accepted=accepted,
            stop_reason=values.get("stop_reason", ""),
            finished_at=values.get("finished_at"),
        )

        self._store._begin()
        try:
            routine_row = self._connection.execute(
                "SELECT * FROM routines WHERE id = ?", (routine_id,)
            ).fetchone()
            if routine_row is None:
                self._store._rollback()
                raise ValueError(f"routine not found: {routine_id}")

            run_columns = _table_columns(self._connection, "routine_runs")
            has_outcome = "outcome_class" in run_columns
            has_self_report = SELF_REPORT_COLUMN in run_columns
            insert_columns = [
                "routine_id",
                "run_id",
                "iteration",
                "gate_passed",
                "accepted",
                "cost_usd",
                "stop_reason",
                "notes",
                "started_at",
                "finished_at",
                "created_at",
            ]
            insert_values: list[Any] = [
                routine_id,
                values.get("run_id"),
                int(values.get("iteration", 1)),
                None if gate_passed is None else int(bool(gate_passed)),
                None if accepted is None else int(bool(accepted)),
                cost_usd,
                values.get("stop_reason", ""),
                values.get("notes", ""),
                values.get("started_at"),
                values.get("finished_at"),
                values.get("created_at", now),
            ]
            if has_outcome:
                insert_columns.append("outcome_class")
                insert_values.append(outcome_class)
            if has_self_report:
                declared = values.get(SELF_REPORT_COLUMN)
                insert_columns.append(SELF_REPORT_COLUMN)
                # Empty string collapses to NULL: the column's PRESENCE is what
                # marks a row as self-reported, so "" must not create a row that
                # claims to be a built-in tick and names no status.
                insert_values.append(str(declared) if declared else None)
            self._connection.execute(
                f"INSERT INTO routine_runs ({', '.join(insert_columns)}) "
                f"VALUES ({', '.join('?' * len(insert_columns))})",
                insert_values,
            )
            run_row_id = self._connection.execute("SELECT last_insert_rowid() AS id").fetchone()[
                "id"
            ]

            routine_columns = _table_columns(self._connection, "routines")
            has_neutral = "neutral_runs" in routine_columns
            total_runs = int(routine_row["total_runs"]) + 1
            accepted_runs = int(routine_row["accepted_runs"]) + (1 if accepted else 0)
            neutral_runs = (int(routine_row["neutral_runs"] or 0) if has_neutral else 0) + (
                1 if outcome_class == OUTCOME_NEUTRAL else 0
            )
            # ISSUE-8: the rollup is NULL (unknown) when EITHER this run's own
            # cost is unknown (cost_usd is None — the public API route no
            # longer defaults an omitted cost to 0.0) OR the rollup was
            # already poisoned by an earlier unknown run (settle_run's
            # cost_unknown, or an earlier record_run of this same shape).
            # Once unknown, adding a known figure on top cannot un-lose the
            # precision an earlier unknown run already cost the total.
            existing_total_cost = routine_row["total_cost_usd"]
            total_cost = (
                None
                if cost_usd is None or existing_total_cost is None
                else float(existing_total_cost) + cost_usd
            )
            # NULL, never 0.0, when nothing judgeable has happened yet — see
            # routines.acceptance_rate for why 0.0 there is a lie with teeth.
            acceptance_rate = _acceptance_rate(total_runs, accepted_runs, neutral_runs)
            cost_per_accepted_change = (
                total_cost / accepted_runs if accepted_runs > 0 and total_cost is not None else None
            )

            new_status = routine_row["status"]
            auto_pause_reason = routine_row["auto_pause_reason"] or ""
            # Check auto-pause based on SETTLED runs only (exclude pending gate results)
            if routine_row["status"] == "active":
                settled_runs, settled_accepted = _count_settled_runs(
                    self._connection, routine_id, limit=100
                )
                if should_auto_pause_on_settled(settled_runs, settled_accepted):
                    new_status = "auto_paused"
                    settled_acceptance_rate = settled_accepted / settled_runs
                    auto_pause_reason = (
                        f"acceptance rate {settled_acceptance_rate:.0%} over the last "
                        f"{settled_runs} settled runs fell below the 50% floor"
                    )

            neutral_assignment = "neutral_runs = ?, " if has_neutral else ""
            neutral_parameter: list[Any] = [neutral_runs] if has_neutral else []
            self._connection.execute(
                "UPDATE routines SET total_runs = ?, accepted_runs = ?, "
                f"{neutral_assignment}acceptance_rate = ?, "
                "total_cost_usd = ?, cost_per_accepted_change = ?, status = ?, "
                "auto_pause_reason = ?, updated_at = ?, revision = revision + 1 "
                "WHERE id = ?",
                (
                    total_runs,
                    accepted_runs,
                    *neutral_parameter,
                    acceptance_rate,
                    total_cost,
                    cost_per_accepted_change,
                    new_status,
                    auto_pause_reason,
                    now,
                    routine_id,
                ),
            )
            created = self._connection.execute(
                "SELECT * FROM routine_runs WHERE id = ?", (run_row_id,)
            ).fetchone()
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        assert created is not None
        return _routine_run(_row(created) or {})

    @_serialized
    def settle_run(
        self,
        routine_run_id: int,
        *,
        gate_passed: bool | None,
        accepted: bool | None,
        finished_at: str,
        notes: str | None = None,
        cost_usd: float | None = None,
        cost_unknown: bool = False,
        stop_reason: str | None = None,
    ) -> dict[str, Any]:
        """Settle one previously recorded firing and update parent rollups.

        ``record_run`` counts the firing immediately. Settlement therefore
        only adds an accepted run when this row transitions to true, and only
        adds the cost delta between the terminal run and the provisional cost.
        Repeating the same settlement cannot double-count either value.

        When gate_passed is None (no configured gate), the run is excluded from
        the acceptance floor denominator.

        The settlement layer's NULL/NULL semantics are read here, never
        rewritten: a settlement that carries no verdict classifies NEUTRAL and
        increments ``routines.neutral_runs``, which is what removes it from the
        persisted acceptance denominator as well as the floor's.

        ISSUE-8 — ``cost_unknown``: the caller's explicit assertion that this
        run's true cost was never reported (e.g. a dispatched run whose
        ``runs.cost_usd`` came back NULL from every provider), as distinct
        from ``cost_usd=None`` meaning "this call has nothing new to say about
        cost, leave the provisional figure alone" (the self-reported/built-in
        path, whose provisional cost is already a genuine, known zero). When
        set, the routine's ``total_cost_usd`` rollup is poisoned to NULL —
        "unknown", never a manufactured number — and stays NULL on every
        later settlement of that routine, because averaging a known figure
        into an unknown total is still unknown (migration 119).
        """
        now = utc_now_iso()
        self._store._begin()
        try:
            current = self._connection.execute(
                "SELECT * FROM routine_runs WHERE id = ?", (routine_run_id,)
            ).fetchone()
            if current is None:
                self._store._rollback()
                raise ValueError(f"routine run not found: {routine_run_id}")

            routine = self._connection.execute(
                "SELECT * FROM routines WHERE id = ?", (current["routine_id"],)
            ).fetchone()
            if routine is None:
                self._store._rollback()
                raise ValueError(f"routine not found: {current['routine_id']}")

            old_accepted = bool(current["accepted"]) if current["accepted"] is not None else False
            accepted_transition = accepted and not old_accepted
            # ISSUE-8 (Sol review, seam 2): the CHILD row's own cost_usd
            # (nullable since migration 120) must land on the same NULL when
            # this run's cost is unknown — never the $0.00 the parent's
            # NULL used to leave behind in the audit trail. `old_cost_raw`
            # can itself already be None (e.g. record_run wrote this run via
            # the public POST route with no cost_usd at all); a no-op
            # settlement call (cost_usd=None, cost_unknown=False) inherits
            # whatever that already says, unknown included.
            old_cost_raw = current["cost_usd"]
            if cost_unknown:
                new_cost: float | None = None
            elif cost_usd is not None:
                new_cost = float(cost_usd)
            else:
                new_cost = None if old_cost_raw is None else float(old_cost_raw)
            old_cost = 0.0 if old_cost_raw is None else float(old_cost_raw)
            cost_delta = 0.0 if new_cost is None else (new_cost - old_cost)

            # The UPDATE below is guarded by `finished_at IS NULL`, so the row
            # being settled was PENDING and had no outcome class yet. There is
            # therefore no old class to subtract — only a transition into one.
            effective_stop_reason = current["stop_reason"] if stop_reason is None else stop_reason
            outcome_class = classify_run_outcome(
                gate_passed=gate_passed,
                accepted=accepted,
                stop_reason=effective_stop_reason,
                finished_at=finished_at,
            )
            neutral_transition = outcome_class == OUTCOME_NEUTRAL

            assignments = [
                "gate_passed = ?",
                "accepted = ?",
                "finished_at = ?",
            ]
            parameters: list[Any] = [
                None if gate_passed is None else int(bool(gate_passed)),
                None if accepted is None else int(bool(accepted)),
                finished_at,
            ]
            if "outcome_class" in _table_columns(self._connection, "routine_runs"):
                assignments.append("outcome_class = ?")
                parameters.append(outcome_class)
            if notes is not None:
                assignments.append("notes = ?")
                parameters.append(notes)
            if cost_usd is not None or cost_unknown:
                # `new_cost` is None here exactly when cost_unknown asserted
                # it — writes NULL to the child row, matching the parent.
                assignments.append("cost_usd = ?")
                parameters.append(new_cost)
            if stop_reason is not None:
                assignments.append("stop_reason = ?")
                parameters.append(stop_reason)
            parameters.append(routine_run_id)
            cursor = self._connection.execute(
                f"UPDATE routine_runs SET {', '.join(assignments)} WHERE id = ? AND finished_at IS NULL",
                parameters,
            )
            if cursor.rowcount != 1:
                self._store._rollback()
                raise RoutineRunAlreadySettled(f"routine run {routine_run_id} is already settled")

            has_neutral = "neutral_runs" in _table_columns(self._connection, "routines")
            total_runs = int(routine["total_runs"])
            accepted_runs = int(routine["accepted_runs"]) + (1 if accepted_transition else 0)
            neutral_runs = (int(routine["neutral_runs"] or 0) if has_neutral else 0) + (
                1 if neutral_transition else 0
            )
            # ISSUE-8: `cost_unknown` poisons the rollup to NULL outright — an
            # explicit assertion beats any delta arithmetic. `new_cost is
            # None` also catches the no-op case that inherits an already-
            # unknown child row (e.g. record_run wrote it via the public API
            # with no cost_usd at all — the parent would already be NULL from
            # that write, but this stays correct even if it weren't).
            existing_total_cost = routine["total_cost_usd"]
            if cost_unknown or new_cost is None:
                total_cost = None
            elif existing_total_cost is not None:
                total_cost = float(existing_total_cost) + cost_delta
            else:
                # RECOVERY (coordinator/Sol review): existing_total_cost is
                # NULL (poisoned) but THIS settlement just resolved a known
                # cost for its own row. "Once unknown, stays unknown" was
                # correct for a PERMANENTLY unknown run (cost_unknown=True
                # can never be revisited — settle_run refuses to settle the
                # same row twice) but wrong for a run that was merely
                # PENDING with an unknown cost at record time and has now
                # been told its real one: staying poisoned forever there
                # turned "unknown reads as zero" into "known reads as
                # unknown", which is its own permanent, silent outage for a
                # budget_usd cap (see hard_cap_hit). Recover the rollup
                # exactly when this was the LAST remaining unknown child —
                # the child row UPDATE above already committed `new_cost`,
                # so a fresh scan of every row for this routine is ground
                # truth, not a guess: if none are NULL anymore, the true
                # total is knowable again and SUM is exactly that total; if
                # another child is still unknown (still pending, or
                # permanently poisoned by an earlier cost_unknown=True), the
                # routine's true total remains genuinely unresolvable and
                # must stay NULL.
                remaining_unknown = self._connection.execute(
                    "SELECT COUNT(*) AS n FROM routine_runs WHERE routine_id = ? AND cost_usd IS NULL",
                    (current["routine_id"],),
                ).fetchone()["n"]
                if remaining_unknown == 0:
                    recovered_sum = self._connection.execute(
                        "SELECT SUM(cost_usd) AS total FROM routine_runs WHERE routine_id = ?",
                        (current["routine_id"],),
                    ).fetchone()["total"]
                    total_cost = float(recovered_sum) if recovered_sum is not None else 0.0
                else:
                    total_cost = None
            acceptance_rate = _acceptance_rate(total_runs, accepted_runs, neutral_runs)
            cost_per_accepted_change = (
                total_cost / accepted_runs if accepted_runs > 0 and total_cost is not None else None
            )

            new_status = routine["status"]
            auto_pause_reason = routine["auto_pause_reason"] or ""
            # Every settlement moves the floor, not just accepting ones: a run
            # that settles REJECTED is exactly what trips it. Re-checking only on
            # an accepted transition let a routine stop firing (should_fire reads
            # the same counts) while status stayed 'active' with no reason — the
            # dashboard showed it healthy while it silently never ran.
            if routine["status"] == "active":
                # Check auto-pause based on SETTLED runs only (exclude pending gate results)
                settled_runs, settled_accepted = _count_settled_runs(
                    self._connection, current["routine_id"], limit=100
                )
                if should_auto_pause_on_settled(settled_runs, settled_accepted):
                    new_status = "auto_paused"
                    settled_acceptance_rate = settled_accepted / settled_runs if settled_runs else 0
                    auto_pause_reason = (
                        f"acceptance rate {settled_acceptance_rate:.0%} over the last "
                        f"{settled_runs} settled runs fell below the 50% floor"
                    )

            # `neutral_transition` belongs in this condition for the same reason
            # a settled REJECTION does: it changes the acceptance DENOMINATOR,
            # and a rollup that silently disagrees with the runs table is how
            # the dashboard came to show a routine healthy while it never ran.
            if (
                accepted_transition
                or neutral_transition
                or cost_usd is not None
                or cost_unknown
                or new_status != routine["status"]
            ):
                neutral_assignment = "neutral_runs = ?, " if has_neutral else ""
                neutral_parameter: list[Any] = [neutral_runs] if has_neutral else []
                self._connection.execute(
                    "UPDATE routines SET accepted_runs = ?, "
                    f"{neutral_assignment}acceptance_rate = ?, "
                    "total_cost_usd = ?, cost_per_accepted_change = ?, status = ?, "
                    "auto_pause_reason = ?, updated_at = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (
                        accepted_runs,
                        *neutral_parameter,
                        acceptance_rate,
                        total_cost,
                        cost_per_accepted_change,
                        new_status,
                        auto_pause_reason,
                        now,
                        current["routine_id"],
                    ),
                )

            settled = self._connection.execute(
                "SELECT * FROM routine_runs WHERE id = ?", (routine_run_id,)
            ).fetchone()
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        assert settled is not None
        return _routine_run(_row(settled) or {})

    @_serialized
    def list_runs(self, routine_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = _rows(
            self._connection.execute(
                "SELECT * FROM routine_runs WHERE routine_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (routine_id, limit),
            ).fetchall()
        )
        return [_routine_run(row) for row in rows]

    @_serialized
    def list_recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Cross-routine recent runs aggregate (routines × routine_runs join).

        Returns the newest ``limit`` settled/recorded runs across every
        routine, newest first. Each row carries the parent routine's name so
        the dashboard can render a timeline without a second round-trip. The
        shape is the one pinned by the S5 contract (section B of
        docs/ui-redesign/FINAL-PLAN.md):

            {routine_id, routine_name, run_id, gate_passed, accepted,
             cost_usd, finished_at}
        """
        rows = _rows(
            self._connection.execute(
                "SELECT rr.routine_id AS routine_id, "
                "       r.name AS routine_name, "
                "       rr.run_id AS run_id, "
                "       rr.gate_passed AS gate_passed, "
                "       rr.accepted AS accepted, "
                "       rr.cost_usd AS cost_usd, "
                "       rr.finished_at AS finished_at "
                "FROM routine_runs rr "
                "JOIN routines r ON r.id = rr.routine_id "
                "ORDER BY rr.created_at DESC, rr.id DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            gate = row.get("gate_passed")
            accepted = row.get("accepted")
            out.append(
                {
                    "routine_id": row["routine_id"],
                    "routine_name": row["routine_name"],
                    "run_id": row["run_id"],
                    "gate_passed": None if gate is None else bool(gate),
                    "accepted": None if accepted is None else bool(accepted),
                    "cost_usd": row["cost_usd"],
                    "finished_at": row["finished_at"],
                }
            )
        return out
