"""Owner-scoped SQLite data access for the Executive Decision Center (migration 130).

The DAL for the ``decisions`` / ``decision_events`` / ``decision_rules`` tables
and the F1 ``edc_source_cursor`` triage watermark. It deliberately copies the
steward store idioms (the ``_checked`` field-allowlist, ``_json``/``_decoded``
JSON handling, ``@_serialized`` locking, ``BEGIN IMMEDIATE`` monotonic id
allocation) rather than importing and bending them — the steward tables are a
different shape and the coupling would be worse than the duplication.

STRICT PRIVACY (locked product decision, synthesis §8): **every read and write
is owner-scoped**. There is deliberately NO unscoped list/get on this store — a
route cannot forget the owner filter because there is no method that omits it.
``owner_employee_id`` is always a required keyword, never inferred from the row
being read.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore

# ---------------------------------------------------------------------------
# Field allow-lists. Unknown keys raise ValueError at the DAL boundary (the same
# guard the steward store relies on so a typo'd column fails loud, not silent).
# ---------------------------------------------------------------------------

#: Accepted keys for :meth:`DecisionStore.create_decision` / ``update_decision``.
#: ``number`` is allocated by the store, never an input; the JSON-backed fields
#: use their DECODED names (``recommended``, not ``recommended_json``).
_DECISION_FIELDS = frozenset(
    {
        "id",
        "owner_employee_id",
        "company_slug",
        "source",
        "source_ref",
        "source_account",
        "occurred_at",
        "title",
        "context",
        "counterparty",
        "classification",
        "consequence",
        "deadline_at",
        "likelihood",
        "confidence",
        "reason",
        "classifier",
        "rule_matches",
        "recommended",
        "available_actions",
        "status",
        "surfaced",
        "escalated_for_deadline",
        "resolution",
        "decided_by",
        "decided_at",
        "notes",
        "tags",
        "board_task_id",
        "board_task_ref",
        "wq_unit_id",
        "assignee_employee_id",
        "draft",
        "execution",
        "verification",
        "snooze_until",
        "slack_number",
        "dm_channel",
        "created_at",
        "updated_at",
    }
)

_RULE_FIELDS = frozenset(
    {
        "id",
        "owner_employee_id",
        "kind",
        "category",
        "matcher",
        "action",
        "state",
        "created_from",
        "approved_by",
        "approved_at",
        "hit_count",
        "last_hit_at",
        "created_at",
        "updated_at",
    }
)

_EVENT_FIELDS = frozenset(
    {
        "id",
        "decision_id",
        "actor",
        "event",
        "from_status",
        "to_status",
        "note",
        "created_at",
    }
)

#: Fields :meth:`DecisionStore.reclassify_decision` may rewrite (review F06). The
#: ``UNIQUE(source, source_ref, owner)`` constraint is the ADAPTER cursor, not a
#: verdict — a MAYBE (especially ``classifier='llm_unavailable'`` from an LLM
#: outage) must be re-evaluable later without a second row.
_RECLASSIFY_FIELDS = frozenset(
    {
        "classification",
        "recommended",
        "classifier",
        "confidence",
        "reason",
        "consequence",
        "deadline_at",
        "likelihood",
        "available_actions",
        "rule_matches",
        "surfaced",
    }
)

# JSON-backed columns and their empty defaults, by decoded name.
_DECISION_JSON: dict[str, Any] = {
    "rule_matches": [],
    "recommended": {},
    "available_actions": [],
    "tags": [],
    "draft": {},
    "execution": {},
    "verification": {},
}
_RULE_JSON: dict[str, Any] = {"matcher": {}, "action": {}}

#: Rule kinds that carry UNATTENDED automation authority. Structurally, these are
#: created ONLY by an explicit per-rule owner promotion (:meth:`promote_rule`) —
#: never by ``learn.py`` (which is grep-proven not to emit them) nor by NL parse.
#: F11: promotion is per-rule (state proposed→active + approved_by + kind), and any
#: live-execution gate lives PER RULE in ``action.live``, never a single global flip.
AUTOMATION_RULE_KINDS = frozenset({"auto_delegate", "auto_send"})

#: Sources whose decisions are SYSTEM-generated and must never feed the pattern
#: learner (RESOLUTIONS.md F3, rule inception). ``rule_proposal`` is the estate's
#: own proposal channel; ``session`` is the suggestions-only session source, whose
#: counterparty is a ``session:<id>`` token — clustering over it would teach the
#: learner rules keyed on a machine-local identifier that means nothing tomorrow.
#: Defense in depth: the learner's domain extraction already drops non-email
#: counterparties, so this is the second, explicit line, not the only one.
INTERNAL_DECISION_SOURCES: tuple[str, ...] = ("rule_proposal", "session")

#: The behavior kinds the deterministic classify pass + the learner may create.
#: The learner is restricted to this set at its own boundary; NL parse to this set
#: plus ``surface``/``classify_hint``. NONE of these is an automation kind.
LEARNABLE_RULE_KINDS = frozenset({"suppress", "delegate", "snooze_default"})

#: Fields :meth:`DecisionStore.update_rule` may rewrite (edit / disable / the
#: learner's dedupe-refresh). ``owner_employee_id``/``id`` are immutable identity;
#: ``kind`` is intentionally rewritable here only for a NL-proposal edit — the
#: learner never passes ``kind`` (grep-proven), so this is not an automation
#: back-door.
_RULE_UPDATE_FIELDS = frozenset(
    {"kind", "category", "matcher", "action", "state", "hit_count", "last_hit_at"}
)

# Recovery states are RESOLVABLE (review F1): a transiently-failed send must not
# freeze forever, and an ambiguous crash must still be dismissable by a human.
# ``failed_retryable`` re-enters the send path (edit/approve); ``reconcile_required``
# is human-only (dismiss/note) and NEVER auto-resends.
_RESOLVABLE_STATUSES = (
    "open",
    "snoozed",
    "draft_pending",
    "awaiting_approval",
    "failed_retryable",
    "reconcile_required",
)
_RESOLUTIONS = frozenset(
    {"execute", "delegate", "defer", "approve", "deny", "edit", "reply", "snooze", "dismiss"}
)
_PERSISTED_RESOLUTIONS = _RESOLUTIONS - {"edit"}


#: The operator — the only owner the matrix lets add a card to the SHARED queue
#: (defer:queue). Kept as a literal here so ``store`` (Tier-low, owner-scoped)
#: never imports the team package just to name one id; the value is the same
#: ``OPERATOR_EMPLOYEE_ID`` re-exported by ``team.contracts``.
_OPERATOR_EMPLOYEE_ID = "emp_owner"


def machine_spec(recommended: Any) -> dict[str, Any] | None:
    """The repo-shaped machine work embedded in a recommended action, or ``None``.

    ``defer:machine`` is offered ONLY when the recommended action carries a
    complete unit envelope (``repo_url``/40-hex ``base_sha``/``owned_paths``/
    ``acceptance_cmd`` — the workqueue's own required fields). A human-shaped
    recommendation (reply, phone the provider) has no such block, so the machine
    lane never appears for it. Shared with ``edc.actions`` (the defer executor)
    so the affordance and the executor agree on what "repo-shaped" means.
    """
    if not isinstance(recommended, dict):
        return None
    spec = recommended.get("machine")
    if not isinstance(spec, dict):
        return None
    if not all(
        spec.get(field) for field in ("repo_url", "base_sha", "owned_paths", "acceptance_cmd")
    ):
        return None
    # F3: the workqueue keys off a 40-hex commit; reject a malformed base_sha at
    # the EDC layer so a bad spec never advertises defer:machine nor reaches the
    # queue's own schema check. A non-hex/short/long value fails closed (no lane).
    if not re.match(r"^[0-9a-f]{40}$", str(spec.get("base_sha"))):
        return None
    return spec


def available_actions_for(decision: dict[str, Any], *, status: str | None = None) -> list[str]:
    """Return the owner-action matrix; never trust a client-supplied list.

    P3 makes Execute/Delegate/Defer first-class peers of Reply on an open item
    (synthesis §5, invariant 5), gated by the permission matrix (§6.2/6.3):
    Delegate is always offered (the owner hands the item to another person);
    Defer to the SHARED QUEUE is the operator-only (adding IS approval); Defer to a
    MACHINE appears only when the recommended action is repo-shaped work.
    """
    current = status or str(decision.get("status") or "")
    if current in {"draft_pending", "awaiting_approval"}:
        return ["approve", "deny", "edit", "snooze", "dismiss", "note"]
    if current == "failed_retryable":
        # Review F1: a transient send failure is never frozen. Edit the draft and
        # re-approve, or approve to re-drive the same draft, or drop it.
        return ["approve", "edit", "dismiss", "note"]
    if current == "reconcile_required":
        # Review F1: an ambiguous crash — a human must check the provider first.
        # Dismiss/note ONLY: no automated re-send is ever offered on this state.
        return ["dismiss", "note"]
    if current in {"open", "snoozed"}:
        # A rule_proposal Decision (synthesis §0.5) is decided by approve/deny/edit
        # ONLY — approving flips the linked rule proposed→active, denying declines
        # it. It never carries a reply/delegate/defer affordance of its own.
        if decision.get("source") == "rule_proposal":
            return ["approve", "deny", "edit", "note"]
        # A session Decision (edc/adapters/sessions.py) is a SUGGESTION pointing
        # at the Sessions panel, which is the one surface with authority over a
        # session. It carries no executor of its own: reply would draft mail to a
        # session id, and delegate/defer would hand an interactive terminal to a
        # board card or the machine queue. Snooze/dismiss/note only — this is the
        # structural half of "no executor may act on a session".
        if decision.get("source") == "session":
            return ["snooze", "dismiss", "note"]
        if decision.get("classification") == "maybe":
            return ["edit", "dismiss", "note"]
        actions = ["reply", "delegate", "deny", "snooze", "dismiss", "note", "edit"]
        owner = str(decision.get("owner_employee_id") or "")
        recommended = decision.get("recommended")
        # NOTE: "execute" is deliberately NOT advertised. A read model must never
        # offer an unbuilt write path (P3 review F2): the decide endpoint has no
        # generic execute handler and no classifier populates ``recommended.execute``
        # today, so surfacing it would be a dead affordance that 400s on click. The
        # affordance returns when a concrete execute executor + its recommended
        # shape are specified and wired end-to-end.
        machine = machine_spec(recommended) is not None
        # Defer is reachable when EITHER the shared-queue path is open (the operator) or
        # the machine path is (repo-shaped work) — the resolution token stays
        # "defer" so the store's one-shot CAS accepts it; the executor picks the
        # lane from ``defer_mode``.
        if owner == _OPERATOR_EMPLOYEE_ID or machine:
            actions.append("defer")
        if machine:
            # A UI hint (not a resolution token): the machine lane is available.
            actions.append("defer:machine")
        return actions
    return []


class DecisionConflictError(RuntimeError):
    """A one-shot decision transition lost its compare-and-swap race."""

    def __init__(self, current: dict[str, Any] | None) -> None:
        super().__init__("decision is no longer in a resolvable state")
        self.current = current


class DecisionOwnerError(PermissionError):
    """The authenticated actor does not own the decision."""


# The full ordered column list of the ``decisions`` table (number included). The
# INSERT names these explicitly so an additive future migration cannot silently
# change what this writer persists.
_DECISION_COLUMNS = (
    "id",
    "number",
    "owner_employee_id",
    "company_slug",
    "source",
    "source_ref",
    "source_account",
    "occurred_at",
    "title",
    "context",
    "counterparty",
    "classification",
    "consequence",
    "deadline_at",
    "likelihood",
    "confidence",
    "reason",
    "classifier",
    "rule_matches_json",
    "recommended_json",
    "available_actions_json",
    "status",
    "surfaced",
    "escalated_for_deadline",
    "resolution",
    "decided_by",
    "decided_at",
    "notes",
    "tags_json",
    "board_task_id",
    "board_task_ref",
    "wq_unit_id",
    "assignee_employee_id",
    "draft_json",
    "execution_json",
    "verification_json",
    "snooze_until",
    "slack_number",
    "dm_channel",
    "created_at",
    "updated_at",
)


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Run a method under the composed store's process-wide writer lock.

    Mirrors :func:`omniagentos.steward.store._serialized`: the lock is the same
    reentrant ``RLock`` the ``SqliteStore`` uses for ``_begin``/``_write``, so a
    method may open a ``BEGIN IMMEDIATE`` transaction inside it.
    """

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("DecisionStore", args[0])
        with store._store._lock:
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


def _decision(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, _DECISION_JSON)


def _rule(row: dict[str, Any]) -> dict[str, Any]:
    return _decoded(row, _RULE_JSON)


class DecisionStore:
    """Owner-scoped DAL for EDC decisions, events, rules, and triage cursors."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The calling thread's connection, resolved live (never cached)."""
        return self._store._connection

    # -- decisions ----------------------------------------------------------

    def create_decision(self, decision: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a Decision, allocating its monotonic ``number``; dedupe is a no-op.

        Idempotency is the ``UNIQUE(source, source_ref, owner_employee_id)``
        constraint — this IS the adapter cursor (D3). A re-scan of the same
        source record returns the EXISTING row with ``created=False`` and
        consumes no ``number`` (the existing-row check runs first, inside the
        same ``BEGIN IMMEDIATE`` that would allocate it). Appends the ``create``
        audit event in the same transaction. The whole unit runs through
        ``SqliteStore._execute_write_transaction`` so it inherits the shared
        ``omniagentos.db.busy`` bounded busy/locked retry seam.
        """
        values = dict(_checked(dict(decision), _DECISION_FIELDS))
        for required in ("owner_employee_id", "source", "source_ref", "title", "classification"):
            if not values.get(required):
                raise ValueError(f"decision requires {required!r}")
        if "recommended" not in values:
            # Invariant 1 lives in classify (which demotes to MAYBE rather than
            # surfacing without one); the column is NOT NULL, so default to an
            # empty object here and let the caller populate it.
            values["recommended"] = {}

        now = utc_now_iso()
        decision_id = str(values.get("id") or new_id("dcn"))

        def _body(connection: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            existing = connection.execute(
                "SELECT * FROM decisions WHERE source = ? AND source_ref = ? "
                "AND owner_employee_id = ?",
                (values["source"], values["source_ref"], values["owner_employee_id"]),
            ).fetchone()
            if existing is not None:
                return _decision(dict(existing)), False

            number_row = connection.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM decisions"
            ).fetchone()
            number = int(number_row["n"]) if number_row is not None else 1

            params = self._decision_insert_params(decision_id, number, values, now)
            placeholders = ", ".join("?" for _ in _DECISION_COLUMNS)
            connection.execute(
                f"INSERT INTO decisions ({', '.join(_DECISION_COLUMNS)}) VALUES ({placeholders})",
                params,
            )
            self._insert_event(
                decision_id,
                actor=str(values.get("decided_by") or "system"),
                event="create",
                to_status=str(values.get("status") or "open"),
                note="",
                created_at=now,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row)), True

        return self._store._execute_write_txn(_body, op="edc.create_decision")

    def _decision_insert_params(
        self, decision_id: str, number: int, values: dict[str, Any], now: str
    ) -> tuple[Any, ...]:
        return (
            decision_id,
            number,
            values["owner_employee_id"],
            values.get("company_slug", ""),
            values["source"],
            values["source_ref"],
            values.get("source_account", ""),
            values.get("occurred_at"),
            values["title"],
            values.get("context", ""),
            values.get("counterparty", ""),
            values["classification"],
            values.get("consequence", ""),
            values.get("deadline_at"),
            values.get("likelihood"),
            float(values.get("confidence", 0.0)),
            values.get("reason", ""),
            values.get("classifier", "deterministic"),
            _json(values.get("rule_matches", [])),
            _json(values.get("recommended", {})),
            _json(values.get("available_actions", [])),
            values.get("status", "open"),
            int(values.get("surfaced", 0)),
            int(values.get("escalated_for_deadline", 0)),
            values.get("resolution"),
            values.get("decided_by"),
            values.get("decided_at"),
            values.get("notes", ""),
            _json(values.get("tags", [])),
            values.get("board_task_id"),
            values.get("board_task_ref", ""),
            values.get("wq_unit_id"),
            values.get("assignee_employee_id"),
            _json(values.get("draft", {})),
            _json(values.get("execution", {})),
            _json(values.get("verification", {})),
            values.get("snooze_until"),
            values.get("slack_number"),
            values.get("dm_channel", ""),
            values.get("created_at", now),
            values.get("updated_at", now),
        )

    @_serialized
    def get_decision(self, decision_id: str, *, owner_employee_id: str) -> dict[str, Any] | None:
        """One Decision, only if it belongs to ``owner_employee_id`` (else ``None``).

        A foreign id reads as absent — the API turns that into 404, never 403,
        so an id-probe cannot distinguish "not yours" from "does not exist".
        """
        row = self._connection.execute(
            "SELECT * FROM decisions WHERE id = ? AND owner_employee_id = ?",
            (decision_id, owner_employee_id),
        ).fetchone()
        return None if row is None else _decision(dict(row))

    @_serialized
    def list_decisions(
        self,
        *,
        owner_employee_id: str,
        status: str | None = None,
        classification: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The owner's decisions, newest first. ``owner_employee_id`` is required."""
        clauses = ["owner_employee_id = ?"]
        params: list[Any] = [owner_employee_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if classification is not None:
            clauses.append("classification = ?")
            params.append(classification)
        params.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, number DESC LIMIT ?",
            params,
        ).fetchall()
        return [_decision(dict(row)) for row in rows]

    @_serialized
    def list_source_decisions(
        self,
        *,
        owner_employee_id: str,
        source: str,
        statuses: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """EVERY decision of one source in the given statuses — deliberately unpaged.

        ``list_decisions`` is a UI read and carries a 200-row page; a SOURCE sweep
        that must decide the fate of each of its own rows cannot use it, because
        an unrelated burst (200 newer email rows) would silently push this
        source's stale items off the page and the sweep would read their absence
        as "nothing to do". Scoped to (owner, source, status) so the result set is
        bounded by that source's own live footprint, and served by
        ``idx_decisions_owner_status``. Oldest first: a sweep should reach the
        longest-stale row first even if it is interrupted.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT * FROM decisions WHERE owner_employee_id = ? AND source = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at ASC, number ASC",
            (owner_employee_id, source, *statuses),
        ).fetchall()
        return [_decision(dict(row)) for row in rows]

    @_serialized
    def update_decision(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Owner-scoped field update. Returns the row, or ``None`` if not the owner's.

        A minimal, allow-listed setter for the substrate; the audited one-shot
        ``resolve()`` transition (CAS + event) arrives in a later phase. ``id``,
        ``number``, and the immutable source identity are not updatable here.
        """
        immutable = {"id", "number", "source", "source_ref", "owner_employee_id"}
        values = dict(_checked(dict(fields), _DECISION_FIELDS))
        if immutable & values.keys():
            raise ValueError(f"cannot update immutable fields: {sorted(immutable & values.keys())}")
        if not values:
            return self.get_decision(decision_id, owner_employee_id=owner_employee_id)

        assignments: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key in _DECISION_JSON:
                assignments.append(f"{key}_json = ?")
                params.append(_json(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(utc_now_iso())
        params.extend([decision_id, owner_employee_id])
        changed = self._store._write_count(
            f"UPDATE decisions SET {', '.join(assignments)} WHERE id = ? AND owner_employee_id = ?",
            params,
        )
        if changed == 0:
            return None
        return self.get_decision(decision_id, owner_employee_id=owner_employee_id)

    def resolve(
        self,
        decision_id: str,
        *,
        actor: str,
        resolution: str,
        note: str | None = None,
        tags: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one owner-only, one-shot lifecycle transition and audit it.

        The row CAS and ``decision_events`` append share one SQLite transaction.
        Effectful resolutions derive ``in_progress`` server-side so authority
        is consumed before external I/O without pretending the effect completed.
        """
        if not actor:
            raise DecisionOwnerError("an authenticated actor is required")
        if resolution not in _RESOLUTIONS:
            raise ValueError(f"unsupported decision resolution: {resolution}")
        parameters = dict(params or {})
        allowed_parameters = {
            "draft",
            "execution",
            "recommended",
            "snooze_until",
            "slack_number",
            "dm_channel",
            "expected_draft_sha256",
        }
        unknown_parameters = parameters.keys() - allowed_parameters
        if unknown_parameters:
            raise ValueError(
                "unsupported resolution params: " + ", ".join(sorted(unknown_parameters))
            )

        def _body(connection: sqlite3.Connection) -> dict[str, Any]:
            raw = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if raw is None:
                raise KeyError(decision_id)
            current = _decision(dict(raw))
            if current["owner_employee_id"] != actor:
                raise DecisionOwnerError("only the decision owner may resolve it")
            if current["status"] not in _RESOLVABLE_STATUSES:
                raise DecisionConflictError(current)
            if resolution not in available_actions_for(current):
                raise ValueError(
                    f"resolution {resolution!r} is unavailable while status is "
                    f"{current['status']!r}"
                )
            expected_sha = parameters.get("expected_draft_sha256")
            if expected_sha is not None:
                current_sha = str((current.get("draft") or {}).get("sha256") or "")
                if not current_sha or str(expected_sha) != current_sha:
                    raise DecisionConflictError(current)

            to_status = _resolution_status(resolution, current=current, parameters=parameters)
            event = _resolution_event(resolution)

            fields: dict[str, Any] = {
                "status": to_status,
                "decided_by": actor,
                "decided_at": utc_now_iso(),
                "available_actions": available_actions_for(current, status=to_status),
            }
            if resolution in _PERSISTED_RESOLUTIONS:
                fields["resolution"] = resolution
            if note:
                existing = str(current.get("notes") or "").strip()
                fields["notes"] = f"{existing}\n{note}".strip() if existing else note
            if tags is not None:
                fields["tags"] = list(dict.fromkeys([*(current.get("tags") or []), *tags]))
            for key in (
                "draft",
                "execution",
                "recommended",
                "snooze_until",
                "slack_number",
                "dm_channel",
            ):
                if key in parameters:
                    fields[key] = parameters[key]

            assignments: list[str] = []
            values: list[Any] = []
            for key, value in fields.items():
                if key in _DECISION_JSON:
                    assignments.append(f"{key}_json = ?")
                    values.append(_json(value))
                else:
                    assignments.append(f"{key} = ?")
                    values.append(value)
            assignments.append("updated_at = ?")
            values.append(utc_now_iso())
            placeholders = ",".join("?" for _ in _RESOLVABLE_STATUSES)
            values.extend([decision_id, actor, *_RESOLVABLE_STATUSES])
            changed = connection.execute(
                f"UPDATE decisions SET {', '.join(assignments)} "
                f"WHERE id = ? AND owner_employee_id = ? AND status IN ({placeholders})",
                values,
            ).rowcount
            if changed != 1:
                latest = connection.execute(
                    "SELECT * FROM decisions WHERE id = ?", (decision_id,)
                ).fetchone()
                raise DecisionConflictError(None if latest is None else _decision(dict(latest)))
            self._insert_event(
                decision_id,
                actor=actor,
                event=event,
                from_status=str(current["status"]),
                to_status=to_status,
                note=note or "",
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert updated is not None
            return _decision(dict(updated))

        return self._store._execute_write_txn(_body, op="edc.resolve")

    def await_approval(
        self,
        decision_id: str,
        *,
        actor: str,
        slack_number: int,
        dm_channel: str,
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        """CAS a draft into the shared Slack approval transport."""

        def _body(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if row is None:
                raise KeyError(decision_id)
            current = _decision(dict(row))
            if current["owner_employee_id"] != actor:
                raise DecisionOwnerError("only the decision owner may request approval")
            changed = connection.execute(
                "UPDATE decisions SET status = 'awaiting_approval', slack_number = ?, "
                "dm_channel = ?, execution_json = ?, available_actions_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = 'draft_pending'",
                (
                    slack_number,
                    dm_channel,
                    _json(execution),
                    _json(available_actions_for(current, status="awaiting_approval")),
                    utc_now_iso(),
                    decision_id,
                    actor,
                ),
            ).rowcount
            if changed != 1:
                latest = connection.execute(
                    "SELECT * FROM decisions WHERE id = ?", (decision_id,)
                ).fetchone()
                raise DecisionConflictError(None if latest is None else _decision(dict(latest)))
            self._insert_event(
                decision_id,
                actor=actor,
                event="surface",
                from_status="draft_pending",
                to_status="awaiting_approval",
                note="Slack approval requested",
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert updated is not None
            return _decision(dict(updated))

        return self._store._execute_write_txn(_body, op="edc.await_approval")

    def cancel_approval(
        self,
        decision_id: str,
        *,
        actor: str,
        note: str,
    ) -> dict[str, Any] | None:
        """Return an undelivered Slack approval to an editable draft."""

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            changed = connection.execute(
                "UPDATE decisions SET status = 'draft_pending', slack_number = NULL, "
                "dm_channel = '', available_actions_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = 'awaiting_approval'",
                (
                    _json(["approve", "deny", "edit", "snooze", "dismiss", "note"]),
                    utc_now_iso(),
                    decision_id,
                    actor,
                ),
            ).rowcount
            if changed != 1:
                return None
            self._insert_event(
                decision_id,
                actor=actor,
                event="edit",
                from_status="awaiting_approval",
                to_status="draft_pending",
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.cancel_approval")

    def transition_effect(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        from_status: str,
        to_status: str,
        event: str,
        execution: dict[str, Any],
        note: str = "",
    ) -> dict[str, Any]:
        """CAS an action recovery transition and append its event atomically."""

        def _body(connection: sqlite3.Connection) -> dict[str, Any]:
            changed = connection.execute(
                "UPDATE decisions SET status = ?, execution_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = ?",
                (
                    to_status,
                    _json(execution),
                    utc_now_iso(),
                    decision_id,
                    owner_employee_id,
                    from_status,
                ),
            ).rowcount
            if changed != 1:
                raw = connection.execute(
                    "SELECT * FROM decisions WHERE id = ? AND owner_employee_id = ?",
                    (decision_id, owner_employee_id),
                ).fetchone()
                raise DecisionConflictError(None if raw is None else _decision(dict(raw)))
            self._insert_event(
                decision_id,
                actor=owner_employee_id,
                event=event,
                from_status=from_status,
                to_status=to_status,
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.transition_effect")

    def complete_outcome(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        from_status: str,
        to_status: str,
        verification: dict[str, Any],
        event: str = "verify_outcome",
        note: str = "",
    ) -> dict[str, Any] | None:
        """CAS a dispatched decision to its completion state (P3 outcome sweep).

        System-actor transition used ONLY by the completion sweep: it moves a
        ``done_unverified`` decision to ``done_verified`` once the STRONG,
        outcome-verified signal is in (a delegated card's ``verified_at``, a
        deferred wq unit's terminal ``pass``, or a read-only probe) and records
        the probe/verification payload. Owner-scoped and CAS-guarded on
        ``from_status`` so two overlapping ticks cannot double-complete. Returns
        ``None`` if the row moved underneath the sweep (already completed, or the
        owner does not match) — the sweep treats that as "someone else handled
        it", never an error.
        """

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            changed = connection.execute(
                "UPDATE decisions SET status = ?, verification_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = ?",
                (
                    to_status,
                    _json(verification),
                    utc_now_iso(),
                    decision_id,
                    owner_employee_id,
                    from_status,
                ),
            ).rowcount
            if changed != 1:
                return None
            self._insert_event(
                decision_id,
                actor="system",
                event=event,
                from_status=from_status,
                to_status=to_status,
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.complete_outcome")

    def reopen(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        from_status: str,
        event: str,
        note: str = "",
        to_status: str = "open",
    ) -> dict[str, Any] | None:
        """System CAS used by approval expiry and snooze resurfacing sweeps.

        ``to_status`` defaults to ``open``; approval expiry passes
        ``draft_pending`` when the decision still holds a reply draft so the
        dashboard send panel keeps rendering (review F3).
        """

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current_row = connection.execute(
                "SELECT * FROM decisions WHERE id = ? AND owner_employee_id = ?",
                (decision_id, owner_employee_id),
            ).fetchone()
            if current_row is None:
                return None
            current = _decision(dict(current_row))
            changed = connection.execute(
                "UPDATE decisions SET status = ?, snooze_until = NULL, "
                "slack_number = NULL, available_actions_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = ?",
                (
                    to_status,
                    _json(available_actions_for(current, status=to_status)),
                    utc_now_iso(),
                    decision_id,
                    owner_employee_id,
                    from_status,
                ),
            ).rowcount
            if changed != 1:
                return None
            self._insert_event(
                decision_id,
                actor="system",
                event=event,
                from_status=from_status,
                to_status=to_status,
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.reopen")

    def expire_decision(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        from_status: str,
        note: str = "",
        actor: str = "system",
    ) -> dict[str, Any] | None:
        """System CAS retiring a decision whose SOURCE condition no longer holds.

        The mirror image of :meth:`reopen`, and one transaction for the same two
        reasons: the status flip and its ``expire`` receipt must be atomic (a
        crash between them would leave a retired row with no audit trail), and
        the ``status = from_status`` guard makes an owner's concurrent
        resolution win the race — a sweep that read the row as open a moment
        before the operator dismissed it must NOT overwrite his dismissal. Returns
        ``None`` when the CAS finds the row already moved.

        ``resolution``/``decided_by`` are deliberately left untouched: expiry is
        the system observing that the question evaporated, never a decision the
        owner made, so ``resolved_for_learning`` (which requires a persisted
        resolution) can never read it as taught behaviour.
        """

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            changed = connection.execute(
                "UPDATE decisions SET status = 'expired', available_actions_json = '[]', "
                "snooze_until = NULL, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status = ?",
                (utc_now_iso(), decision_id, owner_employee_id, from_status),
            ).rowcount
            if changed != 1:
                return None
            self._insert_event(
                decision_id,
                actor=actor,
                event="expire",
                from_status=from_status,
                to_status="expired",
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.expire_decision")

    def mark_deadline_escalated(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
    ) -> dict[str, Any] | None:
        """One-shot system escalation for an open/snoozed near-deadline row."""

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ? AND owner_employee_id = ?",
                (decision_id, owner_employee_id),
            ).fetchone()
            if row is None:
                return None
            current = _decision(dict(row))
            if current["status"] not in {"open", "snoozed"}:
                return None
            changed = connection.execute(
                "UPDATE decisions SET classification = 'urgent', "
                "escalated_for_deadline = 1, available_actions_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND escalated_for_deadline = 0 "
                "AND status IN ('open','snoozed')",
                (
                    _json(available_actions_for({**current, "classification": "urgent"})),
                    utc_now_iso(),
                    decision_id,
                    owner_employee_id,
                ),
            ).rowcount
            if changed != 1:
                return None
            self._insert_event(
                decision_id,
                actor="system",
                event="escalate",
                from_status=str(current["status"]),
                to_status=str(current["status"]),
                note=f"deadline within 24h: {current.get('deadline_at')}",
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert updated is not None
            return _decision(dict(updated))

        return self._store._execute_write_txn(_body, op="edc.deadline_escalate")

    def route_stale_in_progress(
        self,
        *,
        owner_employee_id: str,
        cutoff: str,
        note: str = "",
    ) -> list[dict[str, Any]]:
        """Route decisions stuck ``in_progress`` past ``cutoff`` to reconcile.

        A decision whose external effect neither succeeded nor persisted a
        recovery state (a crash mid-dispatch) would otherwise be lost in
        ``in_progress``. It is routed to ``reconcile_required`` — the ambiguous
        state a human resolves; it is NEVER auto-resent. ``cutoff`` is an ISO
        timestamp; rows with ``updated_at < cutoff`` are swept.
        """

        def _body(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT id FROM decisions WHERE owner_employee_id = ? "
                "AND status = 'in_progress' AND updated_at < ?",
                (owner_employee_id, cutoff),
            ).fetchall()
            routed: list[dict[str, Any]] = []
            for row in rows:
                decision_id = str(row["id"])
                changed = connection.execute(
                    "UPDATE decisions SET status = 'reconcile_required', "
                    "available_actions_json = ?, updated_at = ? "
                    "WHERE id = ? AND owner_employee_id = ? AND status = 'in_progress'",
                    (
                        _json(available_actions_for({}, status="reconcile_required")),
                        utc_now_iso(),
                        decision_id,
                        owner_employee_id,
                    ),
                ).rowcount
                if changed != 1:
                    continue
                self._insert_event(
                    decision_id,
                    actor="system",
                    event="escalate",
                    from_status="in_progress",
                    to_status="reconcile_required",
                    note=note or "stale in_progress routed to reconcile_required (no re-send)",
                    connection=connection,
                )
                updated = connection.execute(
                    "SELECT * FROM decisions WHERE id = ?", (decision_id,)
                ).fetchone()
                assert updated is not None
                routed.append(_decision(dict(updated)))
            return routed

        return self._store._execute_write_txn(_body, op="edc.route_stale_in_progress")

    def reclassify_decision(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        fields: dict[str, Any],
        actor: str = "system",
        note: str = "",
    ) -> dict[str, Any] | None:
        """Re-evaluate an OPEN/held decision's classification (review F06).

        A MAYBE row (or an ``llm_unavailable`` fail-closed one) is not frozen by
        the source-uniqueness constraint — it is re-classified in place, with a
        ``classify`` audit event recording the transition. ``fields`` must carry
        ``classification`` and may carry any other classification-derived field.

        Refused (returns ``None``) once the decision has reached a terminal or
        actively-resolving state — reclassifying a sent reply or a completed
        delegation would rewrite history, not re-triage an unhandled item. Runs
        through the shared ``omniagentos.db.busy`` busy-retry seam.
        """
        values = dict(_checked(dict(fields), _RECLASSIFY_FIELDS))
        if not values.get("classification"):
            raise ValueError("reclassify requires 'classification'")

        _RECLASSIFIABLE = ("open", "snoozed", "suppressed")

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = connection.execute(
                "SELECT status, classification FROM decisions "
                "WHERE id = ? AND owner_employee_id = ?",
                (decision_id, owner_employee_id),
            ).fetchone()
            if current is None or current["status"] not in _RECLASSIFIABLE:
                return None

            assignments: list[str] = []
            params: list[Any] = []
            for key, value in values.items():
                if key in _DECISION_JSON:
                    assignments.append(f"{key}_json = ?")
                    params.append(_json(value))
                else:
                    assignments.append(f"{key} = ?")
                    params.append(value)
            assignments.append("updated_at = ?")
            params.append(utc_now_iso())
            params.extend([decision_id, owner_employee_id])
            connection.execute(
                f"UPDATE decisions SET {', '.join(assignments)} "
                "WHERE id = ? AND owner_employee_id = ?",
                params,
            )
            self._insert_event(
                decision_id,
                actor=actor,
                event="classify",
                note=note or f"{current['classification']} -> {values['classification']}",
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return _decision(dict(row))

        return self._store._execute_write_txn(_body, op="edc.reclassify_decision")

    # -- events -------------------------------------------------------------

    def _insert_event(
        self,
        decision_id: str,
        *,
        actor: str,
        event: str,
        from_status: str | None = None,
        to_status: str | None = None,
        note: str = "",
        created_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """Append one audit event on the caller's connection (no own transaction).

        Callers run inside a ``SqliteStore._execute_write_transaction`` unit and
        pass that unit's ``connection`` so the row and its event commit atomically
        under the shared busy-retry seam. ``connection`` defaults to this thread's
        connection for the rare same-thread caller outside a seam transaction.
        """
        conn = connection if connection is not None else self._connection
        event_id = new_id("dce")
        conn.execute(
            "INSERT INTO decision_events "
            "(id, decision_id, actor, event, from_status, to_status, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                decision_id,
                actor,
                event,
                from_status,
                to_status,
                note,
                created_at or utc_now_iso(),
            ),
        )
        return event_id

    def append_event(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        actor: str,
        event: str,
        from_status: str | None = None,
        to_status: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Append an audit event to a decision the owner owns; else raise.

        Owner-scoped like every other method: appending to another owner's
        decision is refused, not silently written to a foreign audit trail. Runs
        through the shared ``omniagentos.db.busy`` busy-retry seam.
        """

        def _body(connection: sqlite3.Connection) -> dict[str, Any]:
            owns = connection.execute(
                "SELECT 1 FROM decisions WHERE id = ? AND owner_employee_id = ?",
                (decision_id, owner_employee_id),
            ).fetchone()
            if owns is None:
                raise KeyError(
                    f"decision {decision_id!r} not found for owner {owner_employee_id!r}"
                )
            event_id = self._insert_event(
                decision_id,
                actor=actor,
                event=event,
                from_status=from_status,
                to_status=to_status,
                note=note,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM decision_events WHERE id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

        return self._store._execute_write_txn(_body, op="edc.append_event")

    @_serialized
    def list_events(self, decision_id: str, *, owner_employee_id: str) -> list[dict[str, Any]]:
        """A decision's audit trail, oldest first — only if the owner owns it.

        The owner check is a JOIN to ``decisions`` so a foreign id yields an
        empty list, never another owner's events.
        """
        rows = self._connection.execute(
            "SELECT e.* FROM decision_events e "
            "JOIN decisions d ON d.id = e.decision_id "
            "WHERE e.decision_id = ? AND d.owner_employee_id = ? "
            # rowid (insertion order) is the tiebreak: created_at is only
            # second-resolution, so two events appended in the same second — a
            # create + its immediate classify, say — must still order by when
            # they were written, not by their random uuid id.
            "ORDER BY e.created_at ASC, e.rowid ASC",
            (decision_id, owner_employee_id),
        ).fetchall()
        return _rows(rows)

    # -- rules --------------------------------------------------------------

    @_serialized
    def create_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Insert a per-owner decision rule (defaults to ``state='proposed'``)."""
        values = dict(_checked(dict(rule), _RULE_FIELDS))
        for required in ("owner_employee_id", "kind", "matcher"):
            if not values.get(required):
                raise ValueError(f"rule requires {required!r}")
        # Defense-in-depth (review minor / F11): the generic inserter never mints an
        # automation kind. ``auto_delegate``/``auto_send`` are created ONLY by the
        # audited :meth:`promote_rule` transition (which sets ``kind`` directly, not
        # via this path), so a caller that reaches ``create_rule`` with an automation
        # kind is a bug — fail closed rather than let a raw insert forge authority.
        if values["kind"] in AUTOMATION_RULE_KINDS:
            raise ValueError(
                f"create_rule may not mint the automation kind {values['kind']!r}; "
                "automation authority is only minted by promote_rule (spec §15.12)"
            )
        now = utc_now_iso()
        rule_id = str(values.get("id") or new_id("dcr"))
        self._store._write(
            "INSERT INTO decision_rules "
            "(id, owner_employee_id, kind, category, matcher_json, action_json, state, "
            "created_from, approved_by, approved_at, hit_count, last_hit_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule_id,
                values["owner_employee_id"],
                values["kind"],
                values.get("category", ""),
                _json(values.get("matcher", {})),
                _json(values.get("action", {})),
                values.get("state", "proposed"),
                values.get("created_from", ""),
                values.get("approved_by"),
                values.get("approved_at"),
                int(values.get("hit_count", 0)),
                values.get("last_hit_at"),
                values.get("created_at", now),
                values.get("updated_at", now),
            ),
        )
        result = self.get_rule(rule_id, owner_employee_id=values["owner_employee_id"])
        assert result is not None
        return result

    @_serialized
    def get_rule(self, rule_id: str, *, owner_employee_id: str) -> dict[str, Any] | None:
        """One rule, only if it belongs to ``owner_employee_id``."""
        row = self._connection.execute(
            "SELECT * FROM decision_rules WHERE id = ? AND owner_employee_id = ?",
            (rule_id, owner_employee_id),
        ).fetchone()
        return None if row is None else _rule(dict(row))

    @_serialized
    def list_rules(
        self,
        *,
        owner_employee_id: str,
        state: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """The owner's rules. ``owner_employee_id`` is required."""
        clauses = ["owner_employee_id = ?"]
        params: list[Any] = [owner_employee_id]
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self._connection.execute(
            f"SELECT * FROM decision_rules WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC, id ASC",
            params,
        ).fetchall()
        return [_rule(dict(row)) for row in rows]

    @_serialized
    def update_rule(
        self, rule_id: str, *, owner_employee_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Owner-scoped, allow-listed rule setter (edit / disable / learner refresh).

        Returns the updated rule, or ``None`` if the id is not the owner's.
        Identity columns (``id``/``owner_employee_id``) are not writable. Callers
        that need the audited proposed→active/declined transitions use
        :meth:`activate_rule` / :meth:`decline_rule` / :meth:`promote_rule`, which
        stamp ``approved_by``/``approved_at`` and CAS on the source state.
        """
        values = dict(_checked(dict(fields), _RULE_UPDATE_FIELDS))
        if not values:
            return self.get_rule(rule_id, owner_employee_id=owner_employee_id)
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key in _RULE_JSON:
                assignments.append(f"{key}_json = ?")
                params.append(_json(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(utc_now_iso())
        params.extend([rule_id, owner_employee_id])
        changed = self._store._write_count(
            f"UPDATE decision_rules SET {', '.join(assignments)} "
            "WHERE id = ? AND owner_employee_id = ?",
            params,
        )
        if changed == 0:
            return None
        return self.get_rule(rule_id, owner_employee_id=owner_employee_id)

    def _transition_rule(
        self,
        rule_id: str,
        *,
        owner_employee_id: str,
        from_states: tuple[str, ...],
        to_state: str,
        approved_by: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS a rule from one of ``from_states`` to ``to_state`` (owner-scoped)."""
        fields = dict(extra or {})
        assignments: list[str] = ["state = ?"]
        params: list[Any] = [to_state]
        if approved_by is not None:
            assignments += ["approved_by = ?", "approved_at = ?"]
            params += [approved_by, utc_now_iso()]
        for key, value in fields.items():
            if key in _RULE_JSON:
                assignments.append(f"{key}_json = ?")
                params.append(_json(value))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(utc_now_iso())
        placeholders = ",".join("?" for _ in from_states)
        params.extend([rule_id, owner_employee_id, *from_states])

        def _body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            changed = connection.execute(
                f"UPDATE decision_rules SET {', '.join(assignments)} "
                f"WHERE id = ? AND owner_employee_id = ? AND state IN ({placeholders})",
                params,
            ).rowcount
            if changed != 1:
                return None
            row = connection.execute(
                "SELECT * FROM decision_rules WHERE id = ?", (rule_id,)
            ).fetchone()
            return None if row is None else _rule(dict(row))

        return self._store._execute_write_txn(_body, op="edc.transition_rule")

    def activate_rule(
        self, rule_id: str, *, owner_employee_id: str, approved_by: str
    ) -> dict[str, Any] | None:
        """Approve a PROPOSED rule → ``active`` (keeps its behavior ``kind``).

        The per-rule promotion the ``rule_proposal`` Decision approval drives
        (synthesis §0.5). A no-op (``None``) if the rule already left ``proposed``.
        NEVER changes the kind, so a ``delegate`` proposal activates as a pre-fill
        rule, not an automation kind — those are :meth:`promote_rule` only.
        """
        return self._transition_rule(
            rule_id,
            owner_employee_id=owner_employee_id,
            from_states=("proposed",),
            to_state="active",
            approved_by=approved_by,
        )

    def decline_rule(
        self, rule_id: str, *, owner_employee_id: str, declined_by: str
    ) -> dict[str, Any] | None:
        """Decline a PROPOSED rule → ``declined`` (30-day re-proposal suppression)."""
        return self._transition_rule(
            rule_id,
            owner_employee_id=owner_employee_id,
            from_states=("proposed",),
            to_state="declined",
            approved_by=declined_by,
        )

    def promote_rule(
        self,
        rule_id: str,
        *,
        owner_employee_id: str,
        approved_by: str,
        kind: str,
        live: bool = False,
    ) -> dict[str, Any] | None:
        """Explicit PER-RULE promotion to an automation kind (F11 / spec §15.12).

        The ONLY path that ever writes an ``auto_delegate``/``auto_send`` kind. It
        is a deliberate, per-rule owner action: state → ``active`` with
        ``approved_by`` stamped, ``kind`` set to the automation kind, and the
        live-execution gate written PER RULE in ``action.live`` (never a single
        global switch that would arm every rule at once). ``live=False`` leaves
        the rule in the pre-fill/one-tap state even while active.
        """
        if kind not in AUTOMATION_RULE_KINDS:
            raise ValueError(f"promote_rule only sets an automation kind, not {kind!r}")
        current = self.get_rule(rule_id, owner_employee_id=owner_employee_id)
        if current is None:
            return None
        action = dict(current.get("action") or {})
        action["live"] = bool(live)
        return self._transition_rule(
            rule_id,
            owner_employee_id=owner_employee_id,
            from_states=("proposed", "active"),
            to_state="active",
            approved_by=approved_by,
            extra={"kind": kind, "action": action},
        )

    def decide_rule_proposal(
        self,
        decision_id: str,
        *,
        owner_employee_id: str,
        actor: str,
        approved: bool,
    ) -> dict[str, Any]:
        """One-shot terminal CAS for a ``rule_proposal`` Decision (approve/deny).

        A rule proposal has no external effect, so approval resolves it directly
        to ``done_verified`` and decline to ``denied`` — no ``in_progress``
        executor hop. Owner-scoped and CAS-guarded on the decidable states so a
        dashboard/Slack race has exactly one winner (:class:`DecisionConflictError`
        otherwise). The rule ``proposed→active``/``declined`` flip is a separate
        owner-scoped call the wrapper makes AFTER this winner is settled.
        """
        to_status = "done_verified" if approved else "denied"
        event = "approve" if approved else "deny"

        def _body(connection: sqlite3.Connection) -> dict[str, Any]:
            raw = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if raw is None:
                raise KeyError(decision_id)
            current = _decision(dict(raw))
            if current["owner_employee_id"] != actor or actor != owner_employee_id:
                raise DecisionOwnerError("only the decision owner may decide its rule proposal")
            if current.get("source") != "rule_proposal":
                raise ValueError("decide_rule_proposal is only for rule_proposal decisions")
            changed = connection.execute(
                "UPDATE decisions SET status = ?, resolution = ?, decided_by = ?, "
                "decided_at = ?, available_actions_json = ?, updated_at = ? "
                "WHERE id = ? AND owner_employee_id = ? AND status IN ('open','snoozed')",
                (
                    to_status,
                    event,
                    actor,
                    utc_now_iso(),
                    _json([]),
                    utc_now_iso(),
                    decision_id,
                    owner_employee_id,
                ),
            ).rowcount
            if changed != 1:
                latest = connection.execute(
                    "SELECT * FROM decisions WHERE id = ?", (decision_id,)
                ).fetchone()
                raise DecisionConflictError(None if latest is None else _decision(dict(latest)))
            self._insert_event(
                decision_id,
                actor=actor,
                event=event,
                from_status=str(current["status"]),
                to_status=to_status,
                note="rule proposal " + ("approved" if approved else "declined"),
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert updated is not None
            return _decision(dict(updated))

        return self._store._execute_write_txn(_body, op="edc.decide_rule_proposal")

    @_serialized
    def resolved_for_learning(
        self,
        *,
        owner_employee_id: str,
        since_iso: str,
        exclude_sources: tuple[str, ...] = INTERNAL_DECISION_SOURCES,
    ) -> list[dict[str, Any]]:
        """Owner's DECIDED decisions in the window, for the nightly learner.

        The single owner-scoped read the learner clusters over. RESOLUTIONS.md F3
        (rule inception): internal/system-origin decisions are excluded HERE, in
        the DAL, via ``source NOT IN (...)`` — a ``rule_proposal`` (or any future
        system-generated) decision can never feed the pattern learner. Only rows
        with a persisted owner ``resolution`` and a ``decided_at`` inside the
        window are returned.
        """
        placeholders = ",".join("?" for _ in exclude_sources) or "''"
        rows = self._connection.execute(
            f"SELECT * FROM decisions WHERE owner_employee_id = ? "
            f"AND resolution IS NOT NULL AND resolution != '' "
            f"AND decided_at IS NOT NULL AND decided_at >= ? "
            f"AND source NOT IN ({placeholders}) "
            "ORDER BY decided_at ASC",
            (owner_employee_id, since_iso, *exclude_sources),
        ).fetchall()
        return [_decision(dict(row)) for row in rows]

    # -- triage watermark (F1) ---------------------------------------------

    @_serialized
    def get_source_cursor(self, source: str, owner_employee_id: str) -> dict[str, Any] | None:
        """The durable triage watermark for one ``(source, owner)``, or ``None``.

        The email adapter selects only ``comms_messages`` beyond
        ``last_message_id`` so triage is O(new messages), not O(history) (F1).
        """
        row = self._connection.execute(
            "SELECT * FROM edc_source_cursor WHERE source = ? AND owner_employee_id = ?",
            (source, owner_employee_id),
        ).fetchone()
        return _row(row)

    @_serialized
    def advance_source_cursor(
        self,
        source: str,
        owner_employee_id: str,
        *,
        last_message_id: str,
        last_triaged_at: str | None = None,
    ) -> dict[str, Any]:
        """Upsert the ``(source, owner)`` watermark to ``last_message_id``.

        Monotonic by construction on the caller's side (advanced only after a
        message is durably decisioned); the UPSERT itself is a plain replace, so
        a caller that passes a lower id would move it backward — callers must
        pass the highest id they have triaged.
        """
        stamp = last_triaged_at or utc_now_iso()
        self._store._write(
            "INSERT INTO edc_source_cursor "
            "(source, owner_employee_id, last_message_id, last_triaged_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, owner_employee_id) DO UPDATE SET "
            "last_message_id = excluded.last_message_id, "
            "last_triaged_at = excluded.last_triaged_at",
            (source, owner_employee_id, last_message_id, stamp),
        )
        result = self.get_source_cursor(source, owner_employee_id)
        assert result is not None
        return result


def _resolution_status(
    resolution: str,
    *,
    current: dict[str, Any],
    parameters: dict[str, Any],
) -> str:
    if resolution == "edit":
        return "draft_pending" if "draft" in parameters else str(current["status"])
    return {
        "dismiss": "dismissed",
        "deny": "denied",
        "snooze": "snoozed",
        "reply": "draft_pending",
        "approve": "in_progress",
        "execute": "in_progress",
        "delegate": "in_progress",
        "defer": "in_progress",
    }.get(resolution, "in_progress")


def _resolution_event(resolution: str) -> str:
    if resolution == "reply":
        return "draft"
    return resolution


__all__ = [
    "AUTOMATION_RULE_KINDS",
    "INTERNAL_DECISION_SOURCES",
    "LEARNABLE_RULE_KINDS",
    "DecisionConflictError",
    "DecisionOwnerError",
    "DecisionStore",
    "available_actions_for",
    "machine_spec",
]
