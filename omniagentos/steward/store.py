"""Thread-safe SQLite data access for the Horizon 4 Steward."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore

_GOAL_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "discipline_id",
        "north_star",
        "target",
        "keywords",
        "priority",
        "status",
        "parent_goal_id",
        "routine_id",
        "origin",
        "graduated_at",
        "blocked_reason",
        "created_at",
        "updated_at",
    }
)
_GOAL_READING_FIELDS = frozenset({"goal_id", "cycle", "value", "met", "captured_at"})
#: Column assignments for the dict-driven upsert UPDATE clause. Kept at module
#: scope NEXT TO ``_GOAL_FIELDS`` deliberately: the parity test in
#: tests/goals/test_goal_loops.py asserts these keys equal
#: ``_GOAL_FIELDS - {id, name, created_at, updated_at}`` — a column added to
#: one list but not the other would otherwise become silently non-updatable
#: and read as "preserved".
_GOAL_UPDATABLE = {
    "description": "description = excluded.description",
    "discipline_id": "discipline_id = excluded.discipline_id",
    "north_star": "north_star_json = excluded.north_star_json",
    "target": "target_json = excluded.target_json",
    "keywords": "keywords_json = excluded.keywords_json",
    "priority": "priority = excluded.priority",
    "status": "status = excluded.status",
    "parent_goal_id": "parent_goal_id = excluded.parent_goal_id",
    "routine_id": "routine_id = excluded.routine_id",
    "origin": "origin = excluded.origin",
    "graduated_at": "graduated_at = excluded.graduated_at",
    "blocked_reason": "blocked_reason = excluded.blocked_reason",
}
_SNAPSHOT_FIELDS = frozenset(
    {"goal_id", "source", "metric", "value", "unit", "window", "captured_at", "meta"}
)
_COMMS_FIELDS = frozenset(
    {
        "source",
        "external_id",
        "thread_id",
        "sender",
        "recipients",
        "sent_at",
        "subject",
        "body_text",
        "raw",
        "attachments",
        "kb_status",
        "episode_id",
        "created_at",
        # EDC (migration 130): the owner axis on the email substrate. Nullable —
        # a message from an unmapped source stays owner NULL and EDC skips it
        # loudly rather than guessing. Stamped at ingest from the edc.accounts
        # map; never a per-request parameter.
        "owner_employee_id",
    }
)
_SOURCE_FIELDS = frozenset({"status", "config", "last_poll_at", "last_error"})
_ALERT_FIELDS = frozenset(
    {
        "rule",
        "severity",
        "title",
        "body",
        "evidence",
        "cooldown_key",
        "state",
        "created_at",
        "acked_at",
        "cooldown_minutes",
        "magnitude",
    }
)
_SUGGESTION_FIELDS = frozenset(
    {
        "id",
        "title",
        "rationale",
        "evidence",
        "proposed_plan",
        "risk_class",
        "source",
        "goal_id",
        "alert_id",
        "outcome",
        "state",
        "created_at",
        "decided_at",
        "decided_by",
        "task_id",
        "run_id",
    }
)
_BRIEFING_FIELDS = frozenset(
    {
        "id",
        "briefing_date",
        "vault_path",
        "summary",
        "deliveries",
        "voice_artifact_id",
        "acked_at",
        "created_at",
    }
)


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        steward = cast("StewardStore", args[0])
        with steward._store._lock:
            return method(*args, **kwargs)

    return wrapped


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


class GoalExistsError(ValueError):
    """A create-only goal write hit an existing name (UNIQUE refusal)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a goal named {name!r} already exists")
        self.name = name


def _checked(values: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")
    return values


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _decoded(row: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for name, default in fields.items():
        out[name] = _parse_json(out.pop(f"{name}_json", None), default)
    return out


def _goal(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"north_star": {}, "target": {}, "keywords": []})


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"meta": {}})


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"recipients": [], "raw": {}, "attachments": []})


def _source(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"config": {}})


def _alert(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"evidence": {}})


def _suggestion(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"evidence": [], "proposed_plan": None, "outcome": {}})


def _briefing(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, {"summary": {}, "deliveries": []})


def sustained_consecutive(
    readings: list[dict[str, Any]], periods: int, *, current_cycle: int
) -> bool:
    """Return whether the newest ``periods`` readings sustain the setpoint.

    ``readings`` must be ordered oldest-to-newest, as returned by
    :meth:`StewardStore.goal_reading_series`. An absent value always breaks the
    current streak, even if malformed input claims it was met.

    A streak is only "sustained" if the last ``periods`` readings cover
    ``periods`` DISTINCT, CONSECUTIVE cycles ending AT ``current_cycle`` — the
    controller's own present cycle. Row COUNT alone is not enough (a gapped
    series, e.g. cycles 1/7/99, or duplicate rows for one retried cycle must
    not read as a real streak), and neither is a historical streak: a metric
    source that went dark stops producing readings, so without the
    ``current_cycle`` anchor an old streak would read as "sustained" forever.
    Dark source == absence == not met, by construction.
    """
    # Anchor validation FIRST (r4): a malformed anchor must raise identically
    # regardless of series length — raising only on long series while a short
    # series swallowed it to False made the contract state-dependent. The
    # anchor MUST equal the cycle of the reading the controller just wrote
    # THIS tick (not its next cycle): an off-by-one caller would render a
    # perfect streak permanently un-graduated, indistinguishable from a dark
    # source.
    if isinstance(current_cycle, bool) or not isinstance(current_cycle, int):
        raise ValueError(f"current_cycle must be an int: {current_cycle!r}")
    if periods <= 0 or len(readings) < periods:
        return False
    window = readings[-periods:]
    if not all(row.get("value") is not None and bool(row.get("met")) for row in window):
        return False
    raw_cycles = [row.get("cycle") for row in window]
    if any(not isinstance(cycle, int) or isinstance(cycle, bool) for cycle in raw_cycles):
        return False
    cycles = cast("list[int]", raw_cycles)
    if cycles[-1] != current_cycle:
        return False
    if len(set(cycles)) != periods:
        return False
    return all(cycles[i + 1] - cycles[i] == 1 for i in range(len(cycles) - 1))


# Severity hierarchy for cooldown escalation.
_SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_ESCALATION_FACTOR = 1.5
# How many evidence entries a repeatedly-firing suggestion keeps (most recent
# first out of the front). Enough to show a pattern in the UI; small enough that
# a rule firing every cycle for two weeks cannot grow the row without bound.
_SUGGESTION_EVIDENCE_MAX = 25


def suggestion_occurrences(suggestion: dict[str, Any]) -> int:
    """How many times this suggestion has fired -- explicitly, not by counting.

    Reads the ``_case`` counter :meth:`StewardStore.create_or_bump_suggestion`
    maintains. ``len(evidence)`` is only the answer for rows written before that
    counter existed (and only until their next bump), because evidence is capped
    at :data:`_SUGGESTION_EVIDENCE_MAX` and the count is not.
    """
    case_meta = (suggestion.get("outcome") or {}).get("_case") or {}
    count = case_meta.get("occurrence_count")
    if isinstance(count, int) and count >= 1:
        return count
    return max(1, len(suggestion.get("evidence") or []))


class StewardStore:
    """Steward DAL composed over an already configured :class:`SqliteStore`."""

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

    @_serialized
    def upsert_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        values = dict(_checked(dict(goal), _GOAL_FIELDS))
        now = utc_now_iso()
        goal_id = str(values.get("id") or f"gl_{uuid.uuid4().hex[:12]}")
        parameters = (
            goal_id,
            values["name"],
            values.get("description", ""),
            values.get("discipline_id"),
            _json(values.get("north_star", {})),
            _json(values.get("target", {})),
            _json(values.get("keywords", [])),
            values.get("priority", 100),
            values.get("status", "active"),
            values.get("parent_goal_id"),
            values.get("routine_id"),
            values.get("origin"),
            values.get("graduated_at"),
            values.get("blocked_reason"),
            values.get("created_at", now),
            values.get("updated_at", now),
        )
        # The UPDATE clause is built from the keys the CALLER actually passed:
        # an omitted key leaves the stored column untouched, an explicit None
        # clears it. This is what makes goals/seed.py's idempotent re-seed
        # unable to erase graduation/lineage it never heard of, while a
        # graduation-aware caller can still clear graduated_at by passing None.
        #
        # Graduation consistency (r4, symmetric and LOUD): the pair
        # (status, graduated_at) must never form a contradictory row, and a
        # refused write must never look like a success (r3's silent CASE did).
        #   - status-without-graduation on a graduated goal: 'active' is the
        #     seed's idempotent shape and is ignored FOR STATUS ONLY via the
        #     CASE (the seed stays re-runnable); any OTHER status raises —
        #     an operator write must never be silently swallowed.
        #   - clearing graduated_at while stored status is 'graduated'
        #     without also supplying status: raises (would strand
        #     status='graduated' with no receipt).
        #   - status='graduated' without a receipt (stored or supplied):
        #     raises.
        existing_row = _row(
            self._connection.execute(
                "SELECT status, graduated_at FROM goals WHERE name = ?",
                (values["name"],),
            ).fetchone()
        )
        if existing_row is not None:
            stored_status = existing_row.get("status")
            stored_graduated = existing_row.get("graduated_at")
            status_supplied = "status" in values
            graduation_supplied = "graduated_at" in values
            if (
                status_supplied
                and not graduation_supplied
                and stored_graduated is not None
                and values.get("status") != "active"
            ):
                raise ValueError(
                    "cannot change status of a graduated goal without managing "
                    f"graduated_at (goal {values['name']!r}, "
                    f"requested status {values.get('status')!r})"
                )
            if (
                graduation_supplied
                and values.get("graduated_at") is None
                and not status_supplied
                and stored_status == "graduated"
            ):
                raise ValueError(
                    "clearing graduated_at while status is 'graduated' would "
                    f"strand a contradictory row (goal {values['name']!r}); "
                    "supply status in the same call"
                )
            if (
                status_supplied
                and values.get("status") == "graduated"
                and not (stored_graduated is not None or values.get("graduated_at") is not None)
            ):
                raise ValueError(
                    "status='graduated' requires a graduation receipt "
                    f"(goal {values['name']!r}: none stored, none supplied)"
                )
        assignments = []
        for key, clause in _GOAL_UPDATABLE.items():
            if key not in values:
                continue
            if key == "status" and "graduated_at" not in values:
                # The seed's idempotent 'active' shape: preserved status on a
                # graduated goal (any other status raised above, loudly).
                assignments.append(
                    "status = CASE WHEN goals.graduated_at IS NOT NULL "
                    "THEN goals.status ELSE excluded.status END"
                )
                continue
            assignments.append(clause)
        assignments.append("updated_at = excluded.updated_at")
        self._store._write(
            "INSERT INTO goals (id, name, description, discipline_id, north_star_json, "
            "target_json, keywords_json, priority, status, parent_goal_id, routine_id, origin, "
            "graduated_at, blocked_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET " + ", ".join(assignments),
            parameters,
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM goals WHERE name = ?", (values["name"],)
            ).fetchone()
        )
        assert row is not None
        return _goal(row)

    @_serialized
    def list_goals(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM goals ORDER BY priority ASC, created_at ASC, id ASC"
                ).fetchall()
            )
        else:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM goals WHERE status = ? "
                    "ORDER BY priority ASC, created_at ASC, id ASC",
                    (status,),
                ).fetchall()
            )
        return [_goal(row) for row in rows]

    @_serialized
    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        )
        return None if row is None else _goal(row)

    @_serialized
    def get_goal_by_name(self, name: str) -> dict[str, Any] | None:
        """Return the goal row for ``name``, or ``None``.

        ``upsert_goal`` is keyed on ``name`` (``ON CONFLICT(name)``); a
        create-only caller must check this first -- the write itself cannot
        distinguish create from upsert once it has run.
        """
        row = _row(
            self._connection.execute("SELECT * FROM goals WHERE name = ?", (name,)).fetchone()
        )
        return None if row is None else _goal(row)

    @_serialized
    def insert_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        """Create-ONLY write: plain INSERT, atomic under ``_serialized``.

        A duplicate name raises ``GoalExistsError`` from the UNIQUE constraint
        itself — never a check-then-write race (r3: twelve concurrent creates
        of one name must yield one row and eleven refusals, and a duplicate
        create must never take upsert_goal's update path, which would clear
        parent_goal_id / lower bars past the ancestry guard).
        """
        values = dict(_checked(dict(goal), _GOAL_FIELDS))
        now = utc_now_iso()
        goal_id = str(values.get("id") or f"gl_{uuid.uuid4().hex[:12]}")
        try:
            self._store._write(
                "INSERT INTO goals (id, name, description, discipline_id, north_star_json, "
                "target_json, keywords_json, priority, status, parent_goal_id, routine_id, "
                "origin, graduated_at, blocked_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    values["name"],
                    values.get("description", ""),
                    values.get("discipline_id"),
                    _json(values.get("north_star", {})),
                    _json(values.get("target", {})),
                    _json(values.get("keywords", [])),
                    values.get("priority", 100),
                    values.get("status", "active"),
                    values.get("parent_goal_id"),
                    values.get("routine_id"),
                    values.get("origin"),
                    values.get("graduated_at"),
                    values.get("blocked_reason"),
                    values.get("created_at", now),
                    values.get("updated_at", now),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise GoalExistsError(values["name"]) from exc
        row = _row(
            self._connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        )
        assert row is not None
        return _goal(row)

    _GOAL_TREE_DEPTH_CAP = 5

    @_serialized
    def goal_tree(self, root_goal_id: str) -> dict[str, Any]:
        """Return a goal and descendants, capped at :data:`_GOAL_TREE_DEPTH_CAP`
        depth. A node whose children were withheld (depth cap, or a cycle back
        to an ancestor) is marked ``truncated``/``truncated_reason`` so it is
        never byte-identical to a genuine leaf.
        """

        def build(goal_id: str, depth: int, ancestors: frozenset[str]) -> dict[str, Any]:
            row = _row(
                self._connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            )
            if row is None:
                raise KeyError(f"goal not found: {goal_id}")
            node: dict[str, Any] = {"goal": _goal(row), "children": []}
            if depth >= self._GOAL_TREE_DEPTH_CAP:
                child_rows = _rows(
                    self._connection.execute(
                        "SELECT 1 FROM goals WHERE parent_goal_id = ? LIMIT 1",
                        (goal_id,),
                    ).fetchall()
                )
                if child_rows:
                    node["truncated"] = True
                    node["truncated_reason"] = "depth_cap"
                return node
            child_rows = _rows(
                self._connection.execute(
                    "SELECT * FROM goals WHERE parent_goal_id = ? "
                    "ORDER BY priority ASC, created_at ASC, id ASC",
                    (goal_id,),
                ).fetchall()
            )
            next_ancestors = ancestors | {goal_id}
            children = []
            cycle_withheld = False
            for child in child_rows:
                if child["id"] in next_ancestors:
                    cycle_withheld = True
                    continue
                children.append(build(str(child["id"]), depth + 1, next_ancestors))
            node["children"] = children
            if cycle_withheld:
                node["truncated"] = True
                node["truncated_reason"] = "cycle"
            return node

        return build(root_goal_id, 0, frozenset())

    @_serialized
    def append_goal_reading(self, reading: dict[str, Any]) -> int:
        """Write one (goal_id, cycle) reading, upserting on a retried cycle.

        Validates what the DDL claims this seam enforces: ``value`` must be
        ``None`` or finite (a NaN must never land as absent-but-met); ``met``
        must be strictly 0/1/bool (a "false" string must never coerce True);
        ``goal_id`` must reference an existing goal (135's FK backstops this).
        Upserts on a repeated cycle rather than inserting a duplicate row,
        since a retried write is a re-report, not a new sample.
        """
        values = dict(_checked(dict(reading), _GOAL_READING_FIELDS))
        value = values.get("value")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"goal reading value must be a real number or None: {value!r}")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"goal reading value must be finite: {value!r}")
        met_raw = values.get("met", 0)
        if (
            not isinstance(met_raw, (bool, int))
            or isinstance(met_raw, int)
            and met_raw not in (0, 1)
        ):
            raise ValueError(f"goal reading met must be 0, 1, or a bool: {met_raw!r}")
        cycle_raw = values.get("cycle")
        if (
            isinstance(cycle_raw, bool)
            or not isinstance(cycle_raw, int)
            or cycle_raw < 0
            or cycle_raw >= 2**63
        ):
            # SQLite would happily store 'abc' as TEXT and the sustain predicate
            # would then TypeError mid-graduation — refuse at the seam instead.
            # The upper bound stops the OverflowError SQLite's driver would
            # raise for ints past 2**63-1 from relocating the failure deeper.
            raise ValueError(f"goal reading cycle must be a non-negative int64: {cycle_raw!r}")
        goal_id = values["goal_id"]
        if self.get_goal(goal_id) is None:
            raise KeyError(f"goal not found: {goal_id}")
        self._store._write(
            "INSERT INTO goal_readings (goal_id, cycle, value, met, captured_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(goal_id, cycle) DO UPDATE SET "
            "value = excluded.value, met = excluded.met, captured_at = excluded.captured_at",
            (
                goal_id,
                values["cycle"],
                value,
                0 if value is None else int(bool(met_raw)),
                values.get("captured_at", utc_now_iso()),
            ),
        )
        row = self._connection.execute(
            "SELECT id FROM goal_readings WHERE goal_id = ? AND cycle = ?",
            (goal_id, values["cycle"]),
        ).fetchone()
        assert row is not None
        return int(row[0])

    @_serialized
    def goal_reading_series(self, goal_id: str, *, last_n: int = 20) -> list[dict[str, Any]]:
        if last_n <= 0:
            return []
        rows = _rows(
            self._connection.execute(
                "SELECT id, goal_id, cycle, value, met, captured_at FROM "
                "(SELECT id, goal_id, cycle, value, met, captured_at FROM goal_readings "
                "WHERE goal_id = ? ORDER BY cycle DESC, id DESC LIMIT ?) "
                "ORDER BY cycle ASC, id ASC",
                (goal_id, last_n),
            ).fetchall()
        )
        return rows

    @_serialized
    def insert_metric_snapshot(self, snap: dict[str, Any]) -> int:
        """Insert or update a metric snapshot, deduplicating per calendar day.

        H1 + PERF-002: Implements UPSERT semantics so that only one snapshot per
        (goal_id, source, metric, calendar_day) exists. Returns the row id.
        """
        values = dict(_checked(dict(snap), _SNAPSHOT_FIELDS))
        captured_at = values.get("captured_at", utc_now_iso())
        goal_id = values.get("goal_id")
        source = values.get("source")
        metric = values.get("metric")
        value = values.get("value")
        unit = values.get("unit", "")
        window = values.get("window", "day")
        meta = _json(values.get("meta", {}))

        self._store._begin()
        try:
            # For SQLite's partial-index ON CONFLICT to work, we must use the exact
            # expression from the index: substr(captured_at, 1, 10). Simpler: delete
            # existing same-day row, then insert.
            day_prefix = captured_at[:10]  # YYYY-MM-DD
            self._connection.execute(
                "DELETE FROM metric_snapshots WHERE goal_id = ? AND source = ? AND metric = ? "
                "AND substr(captured_at, 1, 10) = ?",
                (goal_id, source, metric, day_prefix),
            )
            cursor = self._connection.execute(
                "INSERT INTO metric_snapshots "
                "(goal_id, source, metric, value, unit, window, captured_at, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, source, metric, value, unit, window, captured_at, meta),
            )
            assert cursor.lastrowid is not None
            snapshot_id = int(cursor.lastrowid)
            self._store._commit()
            return snapshot_id
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def latest_snapshot(
        self, source: str, metric: str, goal_id: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM metric_snapshots WHERE source = ? AND metric = ?"
        params: list[Any] = [source, metric]
        if goal_id is not None:
            sql += " AND goal_id = ?"
            params.append(goal_id)
        sql += " ORDER BY captured_at DESC, id DESC LIMIT 1"
        row = _row(self._connection.execute(sql, params).fetchone())
        return None if row is None else _snapshot(row)

    @_serialized
    def snapshot_series(
        self,
        metric: str,
        *,
        goal_id: str | None = None,
        source: str | None = None,
        days: int = 14,
    ) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses = ["metric = ?", "captured_at >= ?"]
        params: list[Any] = [metric, cutoff]
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        rows = _rows(
            self._connection.execute(
                "SELECT * FROM metric_snapshots WHERE "
                + " AND ".join(clauses)
                + " ORDER BY captured_at ASC, id ASC",
                params,
            ).fetchall()
        )
        return [_snapshot(row) for row in rows]

    @_serialized
    def link_fact_to_goal(self, goal_id: str, fact_id: int, linked_by: str) -> None:
        self._store._write(
            "INSERT OR IGNORE INTO goal_facts (goal_id, fact_id, linked_by, linked_at) "
            "VALUES (?, ?, ?, ?)",
            (goal_id, fact_id, linked_by, utc_now_iso()),
        )

    @_serialized
    def goal_fact_links(self, goal_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM goal_facts WHERE goal_id = ? ORDER BY linked_at ASC, fact_id ASC",
                (goal_id,),
            ).fetchall()
        )

    @_serialized
    def insert_comms_message(self, msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        values = dict(_checked(dict(msg), _COMMS_FIELDS))
        created = (
            self._store._write_count(
                "INSERT OR IGNORE INTO comms_messages "
                "(source, external_id, thread_id, sender, recipients_json, sent_at, subject, "
                "body_text, raw_json, attachments_json, kb_status, episode_id, created_at, "
                "owner_employee_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values.get("source"),
                    values.get("external_id"),
                    values.get("thread_id", ""),
                    values.get("sender", ""),
                    _json(values.get("recipients", [])),
                    values.get("sent_at"),
                    values.get("subject", ""),
                    values.get("body_text"),
                    _json(values.get("raw", {})),
                    _json(values.get("attachments", [])),
                    values.get("kb_status", "none"),
                    values.get("episode_id"),
                    values.get("created_at", utc_now_iso()),
                    values.get("owner_employee_id"),
                ),
            )
            > 0
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM comms_messages WHERE source = ? AND external_id = ?",
                (values.get("source"), values.get("external_id")),
            ).fetchone()
        )
        assert row is not None
        return _message(row), created

    @_serialized
    def list_comms_messages(
        self,
        *,
        source: str | None = None,
        q: str | None = None,
        thread_id: str | None = None,
        kb_status: str | None = None,
        owner_employee_id: str | None = None,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if owner_employee_id is not None:
            # EDC owner scoping: exact match on the stamped owner. Callers pass
            # the principal-derived owner; there is no "all owners" convenience
            # that could leak one person's mail into another's view.
            clauses.append("owner_employee_id = ?")
            params.append(owner_employee_id)
        if q is not None:
            clauses.append(
                "(COALESCE(subject, '') LIKE ? OR COALESCE(body_text, '') LIKE ? "
                "OR COALESCE(sender, '') LIKE ?)"
            )
            pattern = f"%{q}%"
            params.extend([pattern, pattern, pattern])
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if kb_status is not None:
            clauses.append("kb_status = ?")
            params.append(kb_status)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = _rows(
            self._connection.execute(
                f"SELECT * FROM comms_messages{where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        )
        return [_message(row) for row in rows]

    @_serialized
    def list_owner_comms_after(
        self,
        *,
        owner_employee_id: str,
        after_id: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Owner-scoped messages with ``id > after_id``, oldest first (EDC F1).

        The email adapter's O(new) triage path: it advances a per-(source, owner)
        watermark to the highest triaged ``comms_messages.id`` and reads only
        beyond it. ``id`` is the monotonic AUTOINCREMENT primary key, so the scan
        starts at ``after_id + 1`` and is bounded by the number of NEW messages,
        never the growing history. Ascending order lets the caller advance the
        watermark monotonically as it consumes each row.
        """
        rows = _rows(
            self._connection.execute(
                "SELECT * FROM comms_messages "
                "WHERE owner_employee_id = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (owner_employee_id, after_id, limit),
            ).fetchall()
        )
        return [_message(row) for row in rows]

    @_serialized
    def get_comms_message(self, message_id: int) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                "SELECT * FROM comms_messages WHERE id = ?", (message_id,)
            ).fetchone()
        )
        return None if row is None else _message(row)

    @_serialized
    def set_message_kb(
        self, message_id: int, *, kb_status: str, episode_id: int | None = None
    ) -> None:
        self._store._write(
            "UPDATE comms_messages SET kb_status = ?, episode_id = ? WHERE id = ?",
            (kb_status, episode_id, message_id),
        )

    @_serialized
    def upsert_comms_source(self, name: str, kind: str, **fields: Any) -> dict[str, Any]:
        values = dict(_checked(dict(fields), _SOURCE_FIELDS))
        columns = ["name", "kind"]
        params: list[Any] = [name, kind]
        for key, value in values.items():
            column = "config_json" if key == "config" else key
            columns.append(column)
            params.append(_json(value) if key == "config" else value)
        assignments = [
            "kind = excluded.kind",
            *[f"{column} = excluded.{column}" for column in columns[2:]],
        ]
        placeholders = ", ".join("?" for _ in columns)
        self._store._write(
            f"INSERT INTO comms_sources ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET {', '.join(assignments)}",
            params,
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM comms_sources WHERE name = ?", (name,)
            ).fetchone()
        )
        assert row is not None
        return _source(row)

    @_serialized
    def list_comms_sources(self) -> list[dict[str, Any]]:
        return [
            _source(row)
            for row in _rows(
                self._connection.execute("SELECT * FROM comms_sources ORDER BY name ASC").fetchall()
            )
        ]

    @_serialized
    def create_alert(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        """Create an alert, or UPDATE the open case with this (rule, cooldown_key) key.

        CASE IDENTITY (no migration): a "case" is identified purely from two
        columns every alert already has -- ``rule`` + ``cooldown_key`` -- so no
        new column is needed to look one up. Its bookkeeping (occurrence
        count, first/last seen) lives under ``evidence_json["_case"]``, a
        nested namespace inside the JSON blob column that already exists,
        picked so it can never collide with a rule's own evidence keys (e.g.
        "magnitude", "value").

        Behaviour:
        - No OPEN row exists for this key (never fired, or the prior case was
          acked/resolved): insert a brand-new open row.
        - An OPEN row exists and this occurrence does NOT escalate it (H12:
          severity did not increase and magnitude, if any, did not jump by
          >= 1.5x): bump that SAME row's occurrence count + last_seen and
          return ``None`` (suppressed) -- the visible fields (severity/title/
          body/evidence) stay exactly as they were, so a quiet repeat never
          looks like new information.
        - An OPEN row exists and this occurrence DOES escalate it: update the
          SAME row's visible fields plus the case bookkeeping and return the
          updated row (the caller treats this as "created" -- a worsening
          incident should re-notify). Before case identity this INSERTED a
          second row for an incident that never actually recovered -- exactly
          the "82 open alerts collapse to ~3 cooldown keys" council finding.
        """
        values = dict(_checked(dict(alert), _ALERT_FIELDS))
        cooldown_minutes = int(values.pop("cooldown_minutes", 0))
        magnitude = values.pop("magnitude", None)
        created_at = str(values.get("created_at") or utc_now_iso())
        cooldown_key = values.get("cooldown_key")
        rule = values.get("rule")
        new_severity = values.get("severity", "low")

        evidence = values.get("evidence", {})
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
        if magnitude is not None:
            evidence["magnitude"] = magnitude

        self._store._begin()
        try:
            open_case: dict[str, Any] | None = None
            if cooldown_key is not None and cooldown_minutes > 0:
                row = self._connection.execute(
                    "SELECT * FROM alerts WHERE rule = ? AND cooldown_key = ? "
                    "AND state = 'open' ORDER BY created_at DESC, id DESC LIMIT 1",
                    (rule, cooldown_key),
                ).fetchone()
                open_case = _alert(dict(row)) if row is not None else None

            if open_case is not None:
                open_evidence = open_case.get("evidence") or {}
                case_meta = dict(open_evidence.get("_case") or {})
                first_seen = (
                    case_meta.get("first_seen") or open_case.get("created_at") or created_at
                )
                case_meta.update(
                    {
                        "occurrence_count": int(case_meta.get("occurrence_count") or 1) + 1,
                        "first_seen": first_seen,
                        "last_seen": created_at,
                    }
                )

                recent_severity = open_case.get("severity", "low")
                recent_magnitude = open_evidence.get("magnitude")
                new_severity_val = _SEVERITY_ORDER.get(new_severity, 0)
                recent_severity_val = _SEVERITY_ORDER.get(recent_severity, 0)
                is_escalation = new_severity_val > recent_severity_val
                if not is_escalation and magnitude is not None and recent_magnitude is not None:
                    # Both must be positive: a zero-vs-zero comparison (e.g. a $0
                    # revenue day, magnitude 0.0) would otherwise satisfy
                    # `0 >= 0 * 1.5` and defeat the cooldown, re-alerting every
                    # cycle (re-review RR-PROD-001). A same-severity repeat with no
                    # real magnitude jump stays suppressed.
                    is_escalation = (
                        magnitude > 0
                        and recent_magnitude > 0
                        and magnitude >= recent_magnitude * _ESCALATION_FACTOR
                    )

                if not is_escalation:
                    # Quiet update: bump the case's bookkeeping only. The row's
                    # visible content is untouched -- this is the "82 opens / 3
                    # keys" fix: repeats collapse into the SAME row instead of
                    # ever appending a new one while the case stays open.
                    quiet_evidence = dict(open_evidence)
                    quiet_evidence["_case"] = case_meta
                    self._connection.execute(
                        "UPDATE alerts SET evidence_json = ? WHERE id = ?",
                        (_json(quiet_evidence), open_case["id"]),
                    )
                    self._store._commit()
                    return None

                evidence["_case"] = case_meta
                self._connection.execute(
                    "UPDATE alerts SET severity = ?, title = ?, body = ?, evidence_json = ? "
                    "WHERE id = ?",
                    (
                        new_severity,
                        values.get("title"),
                        values.get("body"),
                        _json(evidence),
                        open_case["id"],
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM alerts WHERE id = ?", (open_case["id"],)
                ).fetchone()
                self._store._commit()
                assert row is not None
                return _alert(dict(row))

            # No open case for this key: a brand-new incident.
            evidence["_case"] = {
                "occurrence_count": 1,
                "first_seen": created_at,
                "last_seen": created_at,
            }
            cursor = self._connection.execute(
                "INSERT INTO alerts (rule, severity, title, body, evidence_json, cooldown_key, "
                "state, created_at, acked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule,
                    new_severity,
                    values.get("title"),
                    values.get("body"),
                    _json(evidence),
                    cooldown_key,
                    values.get("state", "open"),
                    created_at,
                    values.get("acked_at"),
                ),
            )
            assert cursor.lastrowid is not None
            alert_id = int(cursor.lastrowid)
            row = _row(
                self._connection.execute(
                    "SELECT * FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
            )
            self._store._commit()
            assert row is not None
            return _alert(row)
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        )
        return None if row is None else _alert(row)

    @_serialized
    def resolve_alert(
        self, alert_id: int, *, reason: str, resolved_at: str | None = None
    ) -> dict[str, Any] | None:
        """Close an OPEN case: the underlying rule recovered, or a backlog sweep aged it out.

        No new column: ``state`` already carries a free-form string, and the
        "when + why" of the resolution lives under the SAME no-migration
        ``evidence_json["_case"]`` namespace :meth:`create_alert` writes.
        Returns ``None`` (no-op) if the alert is missing or already non-open.
        """
        row = _row(
            self._connection.execute(
                "SELECT * FROM alerts WHERE id = ? AND state = 'open'", (alert_id,)
            ).fetchone()
        )
        if row is None:
            return None
        current = _alert(row)
        evidence = dict(current.get("evidence") or {})
        case_meta = dict(evidence.get("_case") or {})
        case_meta["resolved_at"] = resolved_at or utc_now_iso()
        case_meta["resolved_reason"] = reason
        evidence["_case"] = case_meta
        changed = self._store._write_count(
            "UPDATE alerts SET state = 'resolved', evidence_json = ? WHERE id = ? AND state = 'open'",
            (_json(evidence), alert_id),
        )
        if changed == 0:
            return None
        return self.get_alert(alert_id)

    @_serialized
    def list_alerts(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if state is None:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            )
        else:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM alerts WHERE state = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            )
        return [_alert(row) for row in rows]

    @_serialized
    def open_alerts(self) -> list[dict[str, Any]]:
        """Every OPEN alert, unpaginated.

        The alert-side twin of :meth:`open_suggestion_by_title`: a dedicated,
        UNLIMITED read for the monitor's three backlog sweeps (auto-resolve,
        disabled-rule, stale-backlog aging), all of which used to scan
        ``list_alerts("open")`` -- whose default ``limit=100`` hid every open
        case past the first page from all three, so a recovered condition,
        a retired rule's zombie case, or a 14-day-stale case could go
        un-swept for as long as it sat beyond page one. That is exactly the
        situation those sweeps exist to fix, so pagination could make them
        silently stop working precisely when the backlog was worst (the same
        failure mode ``open_suggestion_by_title`` closed for suggestion
        dedupe -- see 9e01d306).
        """
        rows = _rows(
            self._connection.execute(
                "SELECT * FROM alerts WHERE state = 'open' ORDER BY created_at DESC, id DESC"
            ).fetchall()
        )
        return [_alert(row) for row in rows]

    @_serialized
    def ack_alert(self, alert_id: int, by: str) -> dict[str, Any] | None:
        del by
        changed = self._store._write_count(
            "UPDATE alerts SET state = 'acked', acked_at = ? WHERE id = ? AND state = 'open'",
            (utc_now_iso(), alert_id),
        )
        if changed == 0:
            return None
        # The same read-back :meth:`resolve_alert` uses; not a second inline
        # copy of the by-id SELECT. ``_serialized`` takes a reentrant lock.
        return self.get_alert(alert_id)

    @_serialized
    def open_alert_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM alerts WHERE state = 'open'"
        ).fetchone()
        return 0 if row is None else int(row["count"])

    @_serialized
    def create_suggestion(self, s: dict[str, Any]) -> dict[str, Any]:
        values = dict(_checked(dict(s), _SUGGESTION_FIELDS))
        suggestion_id = str(values.get("id") or f"sug_{uuid.uuid4().hex[:12]}")
        self._store._write(
            "INSERT INTO suggestions (id, title, rationale, evidence_json, proposed_plan_json, "
            "risk_class, source, goal_id, alert_id, outcome_json, state, created_at, decided_at, "
            "decided_by, task_id, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                suggestion_id,
                values.get("title"),
                values.get("rationale"),
                _json(values.get("evidence", [])),
                _json(values["proposed_plan"]) if values.get("proposed_plan") is not None else None,
                values.get("risk_class"),
                values.get("source"),
                values.get("goal_id"),
                values.get("alert_id"),
                _json(values.get("outcome", {})),
                values.get("state", "open"),
                values.get("created_at", utc_now_iso()),
                values.get("decided_at"),
                values.get("decided_by"),
                values.get("task_id"),
                values.get("run_id"),
            ),
        )
        result = self.get_suggestion(suggestion_id)
        assert result is not None
        return result

    @_serialized
    def open_suggestion_by_title(self, title: str) -> dict[str, Any] | None:
        """The OPEN suggestion with exactly this title, or None.

        A dedicated, UNLIMITED lookup. Dedupe used to scan
        ``list_suggestions(state="open")``, whose default ``limit=100`` made
        every open suggestion past the first page invisible to it: the 101st
        open suggestion's next firing inserted a duplicate row instead of
        bumping, which is the exact pile-up (71 rows, ~2 distinct titles) the
        dedupe exists to prevent -- and it reappears precisely when the backlog
        is worst. Matching is exact string equality, so case/whitespace variants
        are different suggestions by design.
        """
        row = _row(
            self._connection.execute(
                "SELECT * FROM suggestions WHERE state = 'open' AND title = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (title,),
            ).fetchone()
        )
        return None if row is None else _suggestion(row)

    @_serialized
    def create_or_bump_suggestion(self, s: dict[str, Any]) -> dict[str, Any]:
        """Create a suggestion, or bump an existing OPEN one with the same title.

        Council finding: 71 suggestions collapsed to ~2 repeated titles because
        every firing appended a fresh row. Matching is scoped to state='open'
        ONLY, so a suggestion a human already decided (approved/rejected/
        dismissed/expired/...) never resurrects -- the next occurrence starts a
        brand-new open suggestion, same as if this were the first time it fired.

        TWO BOUNDED THINGS, deliberately separated:

        * ``evidence`` keeps only the most recent
          :data:`_SUGGESTION_EVIDENCE_MAX` entries. Every firing used to append
          forever, so a rule firing each cycle grew one JSON blob until a human
          decided the suggestion or the 14-day expiry swept it -- unbounded
          growth on a row that is READ on every suggestions page load.
        * the occurrence COUNT is explicit, in the ``_case`` namespace of the
          ``outcome`` JSON already on the row (the same no-migration idiom
          :meth:`create_alert` uses for alerts). It used to be implied by
          ``len(evidence)``, which the cap above would have silently turned
          into a lie -- "fired 25 times" forever, no matter how long it ran.
        """
        title = s.get("title")
        existing = self.open_suggestion_by_title(str(title)) if title is not None else None
        now = utc_now_iso()
        if existing is None:
            fresh = dict(s)
            outcome = dict(fresh.get("outcome") or {})
            outcome["_case"] = {
                "occurrence_count": 1,
                "first_seen": now,
                "last_seen": now,
            }
            fresh["outcome"] = outcome
            return self.create_suggestion(fresh)

        merged_evidence = list(existing.get("evidence") or [])
        new_evidence = s.get("evidence")
        if isinstance(new_evidence, list):
            merged_evidence.extend(new_evidence)
        elif new_evidence is not None:
            merged_evidence.append(new_evidence)
        kept_evidence = merged_evidence[-_SUGGESTION_EVIDENCE_MAX:]

        outcome = dict(existing.get("outcome") or {})
        case_meta = dict(outcome.get("_case") or {})
        # Rows written before the counter existed carry their count in the
        # evidence list, which is exactly what it meant then. Read it once,
        # then it is explicit forever after.
        previous_count = case_meta.get("occurrence_count")
        if not isinstance(previous_count, int) or previous_count < 1:
            previous_count = max(1, len(existing.get("evidence") or []))
        case_meta.update(
            {
                "occurrence_count": previous_count + 1,
                "first_seen": case_meta.get("first_seen") or existing.get("created_at") or now,
                "last_seen": now,
                # Stated, not inferred: the row no longer holds one evidence
                # entry per occurrence, and a reader must not assume it does.
                "evidence_kept": len(kept_evidence),
            }
        )
        outcome["_case"] = case_meta

        alert_id = s.get("alert_id", existing.get("alert_id"))
        rationale = s.get("rationale", existing.get("rationale"))
        self._store._write(
            "UPDATE suggestions SET evidence_json = ?, outcome_json = ?, alert_id = ?, "
            "rationale = ? WHERE id = ?",
            (_json(kept_evidence), _json(outcome), alert_id, rationale, existing["id"]),
        )
        result = self.get_suggestion(str(existing["id"]))
        assert result is not None
        return result

    @_serialized
    def list_suggestions(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if state is None:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM suggestions ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            )
        else:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM suggestions WHERE state = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            )
        return [_suggestion(row) for row in rows]

    @_serialized
    def get_suggestion(self, sid: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        )
        return None if row is None else _suggestion(row)

    @_serialized
    def claim_suggestion(self, sid: str) -> dict[str, Any] | None:
        """Atomically transition suggestion from 'open' to 'approving'.

        H9: Returns the row if transition succeeded, else None.
        Used by RE (approve) to claim a suggestion before finalizing.
        """
        changed = self._store._write_count(
            "UPDATE suggestions SET state = 'approving' WHERE id = ? AND state = 'open'",
            (sid,),
        )
        if changed == 0:
            return None
        return self.get_suggestion(sid)

    @_serialized
    def release_suggestion(self, sid: str) -> dict[str, Any] | None:
        """Atomically transition suggestion from 'approving' back to 'open'.

        H9: Returns the row if transition succeeded, else None.
        Used by RE to roll back a claim.
        """
        changed = self._store._write_count(
            "UPDATE suggestions SET state = 'open' WHERE id = ? AND state = 'approving'",
            (sid,),
        )
        if changed == 0:
            return None
        return self.get_suggestion(sid)

    @_serialized
    def decide_suggestion(
        self,
        sid: str,
        *,
        state: str,
        decided_by: str,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Decide a suggestion, transitioning from 'open' or 'approving' to final state.

        H9: Widened to accept state='approving' as valid source, not just 'open'.
        """
        changed = self._store._write_count(
            "UPDATE suggestions SET state = ?, decided_by = ?, decided_at = ?, task_id = ?, "
            "run_id = ? WHERE id = ? AND state IN ('open', 'approving')",
            (state, decided_by, utc_now_iso(), task_id, run_id, sid),
        )
        if changed == 0:
            return None
        return self.get_suggestion(sid)

    @_serialized
    def record_suggestion_outcome(self, sid: str, outcome: dict[str, Any]) -> dict[str, Any] | None:
        """Replace the caller-owned outcome, preserving the store's ``_case``.

        The outcome blob belongs to whoever records it (dismiss reason, result
        of a run), but ``_case`` inside it is the store's dedupe bookkeeping --
        wholesale replacement would drop the occurrence count of a suggestion
        that is still open. Everything else is replaced exactly as before.
        """
        merged = dict(outcome)
        if "_case" not in merged:
            current = self.get_suggestion(sid)
            case_meta = (current or {}).get("outcome", {}).get("_case") if current else None
            if case_meta:
                merged["_case"] = case_meta
        changed = self._store._write_count(
            "UPDATE suggestions SET outcome_json = ? WHERE id = ?", (_json(merged), sid)
        )
        if changed == 0:
            return None
        return self.get_suggestion(sid)

    @_serialized
    def insert_briefing(self, b: dict[str, Any]) -> dict[str, Any]:
        values = dict(_checked(dict(b), _BRIEFING_FIELDS))
        briefing_date = str(values["briefing_date"])
        briefing_id = str(values.get("id") or f"brf_{briefing_date}")
        self._store._write(
            "INSERT INTO briefings (id, briefing_date, vault_path, summary_json, deliveries_json, "
            "voice_artifact_id, acked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                briefing_id,
                briefing_date,
                values.get("vault_path"),
                _json(values.get("summary", {})),
                _json(values.get("deliveries", [])),
                values.get("voice_artifact_id"),
                values.get("acked_at"),
                values.get("created_at", utc_now_iso()),
            ),
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM briefings WHERE id = ?", (briefing_id,)
            ).fetchone()
        )
        assert row is not None
        return _briefing(row)

    @_serialized
    def upsert_briefing(self, b: dict[str, Any]) -> dict[str, Any]:
        """Upsert a briefing: INSERT ... ON CONFLICT(briefing_date) DO UPDATE.

        H2 + DATA-003 + INT-003: Ensures same-date re-run doesn't raise or leave
        stale rows. Order: compute → upsert row → write note.
        """
        values = dict(_checked(dict(b), _BRIEFING_FIELDS))
        briefing_date = str(values["briefing_date"])
        briefing_id = str(values.get("id") or f"brf_{briefing_date}")
        now = utc_now_iso()

        self._store._write(
            "INSERT INTO briefings (id, briefing_date, vault_path, summary_json, deliveries_json, "
            "voice_artifact_id, acked_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(briefing_date) DO UPDATE SET "
            "vault_path = excluded.vault_path, summary_json = excluded.summary_json, "
            "deliveries_json = excluded.deliveries_json, voice_artifact_id = excluded.voice_artifact_id, "
            "acked_at = excluded.acked_at",
            (
                briefing_id,
                briefing_date,
                values.get("vault_path"),
                _json(values.get("summary", {})),
                _json(values.get("deliveries", [])),
                values.get("voice_artifact_id"),
                values.get("acked_at"),
                values.get("created_at", now),
            ),
        )
        row = _row(
            self._connection.execute(
                "SELECT * FROM briefings WHERE briefing_date = ?", (briefing_date,)
            ).fetchone()
        )
        assert row is not None
        return _briefing(row)

    @_serialized
    def latest_briefing(self) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                "SELECT * FROM briefings ORDER BY briefing_date DESC, created_at DESC LIMIT 1"
            ).fetchone()
        )
        return None if row is None else _briefing(row)

    @_serialized
    def list_briefings(self, limit: int = 30) -> list[dict[str, Any]]:
        return [
            _briefing(row)
            for row in _rows(
                self._connection.execute(
                    "SELECT * FROM briefings ORDER BY briefing_date DESC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )
        ]

    @_serialized
    def ack_briefing(self, briefing_id: str) -> dict[str, Any] | None:
        changed = self._store._write_count(
            "UPDATE briefings SET acked_at = ? WHERE id = ?", (utc_now_iso(), briefing_id)
        )
        if changed == 0:
            return None
        row = _row(
            self._connection.execute(
                "SELECT * FROM briefings WHERE id = ?", (briefing_id,)
            ).fetchone()
        )
        return None if row is None else _briefing(row)


__all__ = ["StewardStore", "suggestion_occurrences"]
