"""SQLite data access for the Team Work OS (migration 123).

Composed over the H1 :class:`~omniagentos.db.store.SqliteStore` exactly like
:class:`~omniagentos.company_goals.store.CompanyGoalsStore` and
:class:`~omniagentos.collab.store.CollabStore`: this DAL owns ``task_evidence``,
``task_events`` and ``prod_snapshots``, and reads ``board_tasks`` /
``employees`` across the seam. It opens NO connection of its own — it takes the
base store's per-thread connection, its writer lock, and its
``BEGIN IMMEDIATE`` write transaction, so a team write and a board write can
never interleave into each other's transaction.

The two rules that make this more than a table:

* **Evidence is idempotent.** Collectors re-run. ``UNIQUE(kind, repo, ref)``
  makes a re-collected artifact the SAME row, so no sweep can inflate a number
  by running twice.
* **Verification is not self-service.** A card whose evidence a machine graded
  (a passing test run, a merged PR) may be verified by anyone, because the
  claim is the machine's. Everything else needs a second person.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from functools import wraps
from typing import Any, NamedTuple, cast

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTaskStatus
from omniagentos.collab.store import append_task_event
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore, _row, _rows
from omniagentos.team.contracts import (
    ATTRIBUTIONS,
    AUTOMATION_SLOTS_PER_DAY,
    COMMITMENT_ID_PREFIX,
    COMMITMENT_KINDS,
    COMMITMENT_SOURCES,
    COMMITMENT_STATUSES,
    EVIDENCE_ID_PREFIX,
    EVIDENCE_KINDS,
    MECHANICAL_EVIDENCE_KINDS,
    OPEN_COMMITMENT_STATUSES,
    OPERATOR_EMPLOYEE_ID,
    POOL_CARD_LIMIT,
    POOL_DEPTH_FLOOR,
    QUALITY_GATES,
    SLOTTED_COMMITMENT_KINDS,
    QueueCard,
    TeamQueueBuckets,
)

# Owned tables. Nothing else in the repo writes them.
EVIDENCE_TABLE = "task_evidence"
EVENTS_TABLE = "task_events"
SNAPSHOTS_TABLE = "prod_snapshots"
COMMITMENTS_TABLE = "team_commitments"

# Read across the seam (owned by collab/004 and company_goals/098).
BOARD_TABLE = "board_tasks"
EMPLOYEES_TABLE = "employees"

# Append-order for the two append-only tables. ``created_at`` is
# SECOND-resolution (``utc_now_iso``), so it cannot order two rows written in
# the same second — and an audit trail whose ties break on a random uuid is a
# trail that reports moves in an order that never happened (measured: a create
# and the assign that followed it came back reversed). ``rowid`` is SQLite's own
# monotonic insertion counter, so it breaks those ties by the only thing that is
# actually true about them: which INSERT ran first. It is a tiebreaker, not the
# sort key — ``created_at`` stays primary so a backdated import still reads in
# its own chronology.
_APPEND_ORDER = "ORDER BY created_at ASC, rowid ASC"

# Queue rank for a card's priority. Ranked in SQL rather than in Python because
# a queue read may be LIMITed (the pool is): sorting the page after it has been
# truncated would drop the urgent card the sort exists to surface. Any value
# outside the known ladder ranks last, never first. The ``b.`` alias binds it
# to the board row now that the queue reads carry the company join below.
_PRIORITY_RANK_SQL = (
    "CASE b.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)

# Read-only far side of the company join (company_goals/098 + orgdims/061):
# goal_id -> company_goals.org_company_id -> org_companies. LEFT joins, so a
# card with no goal (or a goal whose company row is gone) reads NULL company
# fields rather than dropping out of its queue.
GOALS_TABLE = "company_goals"
COMPANIES_TABLE = "org_companies"
_COMPANY_JOIN = (
    f"LEFT JOIN {GOALS_TABLE} cg ON cg.id = b.goal_id "
    f"LEFT JOIN {COMPANIES_TABLE} oc ON oc.id = cg.org_company_id"
)
_COMPANY_COLUMNS = "oc.slug AS company_slug, oc.name AS company_name"

# The queue buckets, as (name, statuses). Ordered so the API renders columns
# left-to-right without re-deriving an order the store already knows.
_QUEUE_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ready", (BoardTaskStatus.OPEN.value,)),
    ("active", (BoardTaskStatus.CLAIMED.value, BoardTaskStatus.IN_PROGRESS.value)),
    ("blocked", (BoardTaskStatus.BLOCKED.value,)),
    ("review", (BoardTaskStatus.AWAITING_APPROVAL.value,)),
)

_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "verified_points",
    "verified_outcomes",
    "avg_active_sessions",
    "peak_sessions",
    "merged_prs",
    "first_pass_rate",
    "production_x",
)


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize access through the composed base store's connection lock."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        dal = cast("TeamStore", args[0])
        with dal._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _evidence_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = out.pop("meta_json", "{}")
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        parsed = {}
    out["meta"] = parsed if isinstance(parsed, dict) else {}
    return out


def _keys(row: Any) -> Iterable[str]:
    """Column names of a sqlite3.Row or a mapping, without assuming either."""
    if isinstance(row, dict):
        return row.keys()
    return row.keys() if hasattr(row, "keys") else ()


def completion_state(row: Any) -> str | None:
    """The tri-state a DONE card is in — the ONE derivation of it (migration 132).

    ``verified`` (a verifier stamped it), ``failed_verification`` (a verifier
    REFUSED it and nobody has repaired that since), ``unverified`` (done, and
    nobody has looked yet). A card that is not ``done`` has no completion state
    at all: ``None``, never a favourable "unverified".

    Lives here, module-level, because the API, the dashboard badge, the 07:00
    report and ``commitments.resolve_day`` all ask this same question — and the
    moment two of them derive it separately, a card can read verified on one
    surface and failed on another. Accepts any mapping-ish row (a sqlite3.Row,
    a dict, a projected card) so no caller has to re-read the whole record.
    """
    keys = list(_keys(row))
    status = row["status"] if "status" in keys else None
    if str(status or "") != BoardTaskStatus.DONE.value:
        return None
    if "verified_at" in keys and row["verified_at"] is not None:
        return "verified"
    if "verification_failed_at" in keys and row["verification_failed_at"] is not None:
        return "failed_verification"
    return "unverified"


def _valid_slot(kind: str, slot: int) -> int:
    """Bound the slot to what its kind actually has. Mirrors migration 133's CHECK.

    Validated app-side as well as in the schema for the reason every vocabulary
    in this module is: an anonymous ``IntegrityError`` names neither the column
    nor the legal range, and the caller reading it cannot tell a bad slot from a
    duplicate one. The negative case is not hypothetical bookkeeping — a slot of
    ``0`` or ``-1`` reaches Python's list indexing in the resolver, where it
    would quietly select the LAST qualifying card instead of failing.
    """
    number = int(slot)
    ceiling = AUTOMATION_SLOTS_PER_DAY if kind == "automation" else 1
    if not 1 <= number <= ceiling:
        raise ValueError(
            f"slot must be between 1 and {ceiling} for a {kind} commitment; got {slot!r}"
        )
    return number


class VerificationResult(NamedTuple):
    """One verification decision, as the transaction that made it saw it.

    Three facts that MUST come from the same write transaction, because every
    one of them is wrong when re-derived afterwards:

    ``card``
        the board row as it stands after the decision;
    ``event_id``
        the ``verify``/``verify_failed`` event THIS call appended. A caller that
        instead re-reads the trail and takes the last matching row attributes
        its work to whichever event landed most recently — which, under any
        concurrency at all, may be somebody else's;
    ``first_success``
        whether this was the card's first successful verification, counted from
        the append-only event history rather than from ``verified_at``. The
        stamp is cleared by a failed verification and by a reopen, so a card on
        its third pass can present a NULL stamp and look brand new.
    """

    card: dict[str, Any]
    event_id: str
    first_success: bool


def _existing_commitment(
    connection: sqlite3.Connection,
    day: str,
    employee_id: str,
    kind: str,
    task_id: str | None,
    slot: int = 1,
) -> dict[str, Any] | None:
    """The row an ``INSERT OR IGNORE`` collided with, by whichever key applies.

    Two keys, mirroring migration 133's two partial unique indexes: a card
    commitment collides on ``(day, employee, task)``, a slotted one on
    ``(day, employee, kind, slot)``. Reading back by the WRONG key would return
    a sibling slot — so an automation generator would get slot 1 back three
    times and report three "existing" rows it never actually created.

    Which key applies is decided by the KIND, not by whether ``task_id`` happens
    to be set. Those two questions look equivalent and are not: a slotted row
    that (wrongly) carried a task_id would be looked up by a key its index does
    not cover, find nothing, and blow the caller's assertion into a 500. The
    kind is what the index is keyed on, so the kind is what this reads by; the
    store refuses the impossible combination outright (see
    :meth:`TeamStore.create_commitment`).
    """
    if kind not in SLOTTED_COMMITMENT_KINDS:
        return _row(
            connection.execute(
                f"SELECT * FROM {COMMITMENTS_TABLE} "
                "WHERE day = ? AND employee_id = ? AND task_id = ?",
                (day, employee_id, task_id),
            ).fetchone()
        )
    return _row(
        connection.execute(
            f"SELECT * FROM {COMMITMENTS_TABLE} "
            "WHERE day = ? AND employee_id = ? AND kind = ? AND slot = ?",
            (day, employee_id, kind, int(slot)),
        ).fetchone()
    )


def _mint_carry(
    connection: sqlite3.Connection,
    *,
    carried_from: str,
    at: str,
    day: str,
    employee_id: str,
    task_id: str,
    title: str,
    expected_outcome: str = "",
) -> tuple[str, str]:
    """Insert-or-LINK the next-day follow-up for a missed commitment (S2).

    Returns ``(commitment_id, outcome)`` with outcome in:

    ``created``
        No next-day row existed; one was inserted, already linked.
    ``linked``
        A next-day row existed with ``carried_from`` NULL — typically one the
        06:55 generator minted for the same still-active card — and this call
        gave it the miss it follows from.
    ``exists``
        The next-day row is already linked to a miss. Nothing to do, and its
        EXISTING link is kept: a chain of slips must read back in the order it
        happened, so a later pass never re-points an earlier link.

    The 06:55 generator and this mint both write ``(day, employee, task)`` rows
    and either can run first, so a unique violation here would abort a whole
    resolution pass over a race with an obviously correct outcome. This is the
    ONE primitive both carry paths use — the atomic miss path and the repair
    sweep — because a repair that merely asked "does a row exist?" would treat
    a generator-minted UNLINKED row as proof the carry was already handled and
    leave that miss permanently unchained (Sol review, item 2).
    """
    row_id = new_id(COMMITMENT_ID_PREFIX)
    cursor = connection.execute(
        f"INSERT OR IGNORE INTO {COMMITMENTS_TABLE} "
        "(id, day, employee_id, task_id, kind, slot, title, expected_outcome, status, source, "
        "carried_from, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'task', 1, ?, ?, 'carried', 'auto', ?, ?, ?)",
        (row_id, day, employee_id, task_id, title, expected_outcome, carried_from, at, at),
    )
    if cursor.rowcount > 0:
        return row_id, "created"
    existing = _existing_commitment(connection, day, employee_id, "task", task_id)
    assert existing is not None, "INSERT OR IGNORE only skips on a unique collision"
    linked = connection.execute(
        f"UPDATE {COMMITMENTS_TABLE} SET carried_from = ?, updated_at = ? "
        "WHERE id = ? AND carried_from IS NULL",
        (carried_from, at, existing["id"]),
    )
    return str(existing["id"]), ("linked" if linked.rowcount > 0 else "exists")


def _annotate_delivered_commitments(connection: sqlite3.Connection, task_id: str, at: str) -> None:
    """Note on every 'delivered' commitment for ``task_id`` that it later failed.

    A commitment resolved 'delivered' this morning can be refused this
    afternoon. The row is NOT rewritten — a resolution is history, and rewriting
    it would make yesterday's report unreproducible — but a report that still
    reads a plain "delivered" is a report that is quietly wrong, so the note
    carries the correction forward. Any day, not just today: a refusal can
    arrive long after the commitment it undermines.

    BEST EFFORT, never raises: this is an annotation on a side table, and the
    verification refusal it annotates must land regardless (the same discipline
    the learning hook follows one layer up).
    """
    try:
        connection.execute(
            f"UPDATE {COMMITMENTS_TABLE} SET "
            "resolution_note = TRIM(resolution_note || ?), updated_at = ? "
            "WHERE task_id = ? AND status = 'delivered' "
            "AND resolution_note NOT LIKE '%verification failed post-resolution%'",
            (f" verification failed post-resolution {at}", at, task_id),
        )
    except sqlite3.Error as exc:  # pragma: no cover -- schema drift, not a user path
        print(f"team-store: commitment annotation failed for {task_id}: {exc}", file=sys.stderr)


def _one_of(name: str, value: str, allowed: Iterable[str]) -> str:
    """Validate a closed vocabulary here, so the caller sees the field name.

    Migration 123 CHECKs the same sets, but a raw ``IntegrityError`` names an
    anonymous constraint; an operator reading it cannot tell WHICH column was
    wrong or what the legal values are.
    """
    options = tuple(allowed)
    if value not in options:
        raise ValueError(f"{name} must be one of {sorted(options)}; got {value!r}")
    return value


class TeamStore:
    """Team Work OS DAL composed over a configured :class:`SqliteStore`.

    Accepts either a database PATH (``TeamStore("/tmp/x.db")`` — the
    :class:`CollabStore` shape, which migrates the file) or an already-built
    :class:`SqliteStore` (the :class:`CompanyGoalsStore` shape). Prefer the
    latter whenever a collab store already exists in the process: sharing one
    store means sharing one writer lock, and two independent locks over one
    database file serialize only through SQLite's busy handler.
    """

    def __init__(self, db: str | SqliteStore) -> None:
        self._store = db if isinstance(db, SqliteStore) else SqliteStore(str(db))

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance — ``SqliteStore`` hands out one
        connection per thread, so a handle captured at construction time would
        silently interleave this DAL's statements into another thread's
        transaction (CollabStore and RoutinesStore carry the same warning).
        """
        return self._store._connection

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------

    @_serialized
    def record_evidence(
        self,
        *,
        kind: str,
        ref: str,
        task_id: str | None = None,
        repo: str = "",
        actor: str = "",
        title: str = "",
        attribution: str = "deterministic",
        confidence: float = 1.0,
        quality_gate: str = "pass",
        meta: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Record one artifact and say WHAT HAPPENED. IDEMPOTENT on ``(kind, repo, ref)``.

        Returns ``(row, outcome)`` where outcome is one of:

        ``created``
            The artifact was not known; a row was inserted.
        ``attached``
            The row existed UNATTRIBUTED (``task_id IS NULL``) and was not a
            human decision, so this call gave it the card it was missing. This
            is the fix for a 201-that-did-nothing: a POST naming a card, against
            a commit a sweep had already parked in the inbox, used to return the
            stale row untouched and report success.
        ``regraded``
            An existing DETERMINISTIC row's machine verdict changed (an open PR
            that merged, a test run that went red). The gate and meta are
            refreshed; the attribution and the card are not touched.
        ``exists``
            Same artifact, same card, nothing to do.
        ``conflict``
            The row is already attributed to a DIFFERENT card, or a human
            deliberately parked it (``manual`` with no card). Neither may be
            silently re-pointed — that is exactly the overwrite
            :meth:`is_manual` exists to prevent. Moving evidence is
            :meth:`reattribute_evidence`'s job, and it is explicit.

        ``task_id=None`` is a first-class outcome, not a failure: a collector
        that cannot name the card files the artifact UNATTRIBUTED (see
        :meth:`list_unattributed`) rather than guessing an owner and inflating
        someone's numbers.
        """
        _one_of("kind", kind, EVIDENCE_KINDS)
        _one_of("attribution", attribution, ATTRIBUTIONS)
        _one_of("quality_gate", quality_gate, QUALITY_GATES)
        if not str(ref).strip():
            raise ValueError("ref is required (it is half the idempotency key)")
        row_id = evidence_id or new_id(EVIDENCE_ID_PREFIX)
        payload = _json(meta or {})
        now = utc_now_iso()

        def _fetch(connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
            row = _row(
                connection.execute(
                    f"SELECT * FROM {EVIDENCE_TABLE} WHERE id = ?", (identifier,)
                ).fetchone()
            )
            assert row is not None
            return _evidence_dict(row)

        def body(connection: sqlite3.Connection) -> tuple[dict[str, Any], str]:
            existing = _row(
                connection.execute(
                    f"SELECT * FROM {EVIDENCE_TABLE} WHERE kind = ? AND repo = ? AND ref = ?",
                    (kind, repo, ref),
                ).fetchone()
            )
            if existing is None:
                connection.execute(
                    f"INSERT INTO {EVIDENCE_TABLE} "
                    "(id, task_id, kind, ref, repo, actor, title, attribution, confidence, "
                    "quality_gate, meta_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        task_id,
                        kind,
                        ref,
                        repo,
                        actor,
                        title,
                        attribution,
                        float(confidence),
                        quality_gate,
                        payload,
                        now,
                    ),
                )
                if task_id is not None:
                    append_task_event(
                        connection,
                        task_id=task_id,
                        actor=actor or "system",
                        event="evidence",
                        note=f"{kind}:{ref}",
                    )
                return _fetch(connection, row_id), "created"

            existing_id = str(existing["id"])
            manual = str(existing["attribution"]) == "manual"
            current_task = None if existing["task_id"] is None else str(existing["task_id"])
            outcome = "exists"

            # A re-sighting by a COLLECTOR carries a fresh machine verdict. The
            # open PR that merged, the test run that went red: the row is the
            # same artifact, but its grade is not, and a stale grade is what
            # lets a card that never landed keep counting.
            if (
                not manual
                and attribution == "deterministic"
                and str(existing["quality_gate"]) != quality_gate
            ):
                connection.execute(
                    f"UPDATE {EVIDENCE_TABLE} SET quality_gate = ?, meta_json = ? WHERE id = ?",
                    (quality_gate, payload, existing_id),
                )
                outcome = "regraded"

            if task_id is not None and current_task is None and not manual:
                connection.execute(
                    f"UPDATE {EVIDENCE_TABLE} SET task_id = ?, attribution = ?, actor = ? "
                    "WHERE id = ?",
                    (task_id, attribution, actor or str(existing["actor"] or ""), existing_id),
                )
                append_task_event(
                    connection,
                    task_id=task_id,
                    actor=actor or "system",
                    event="evidence",
                    note=f"attached {kind}:{ref}",
                )
                return _fetch(connection, existing_id), "attached"

            if task_id is not None and current_task != task_id:
                # Already spoken for (or deliberately parked by a human).
                return _fetch(connection, existing_id), "conflict"
            return _fetch(connection, existing_id), outcome

        return self._store._execute_write_txn(body, op="team.add_evidence")

    def add_evidence(
        self,
        *,
        kind: str,
        ref: str,
        task_id: str | None = None,
        repo: str = "",
        actor: str = "",
        title: str = "",
        attribution: str = "deterministic",
        confidence: float = 1.0,
        quality_gate: str = "pass",
        meta: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> str:
        """:meth:`record_evidence`, returning only the id of the row that holds it.

        The collector-facing spelling: a sweep re-runs, gets the SAME id back,
        and never has to ask "did I already record this?".
        """
        row, _outcome = self.record_evidence(
            kind=kind,
            ref=ref,
            task_id=task_id,
            repo=repo,
            actor=actor,
            title=title,
            attribution=attribution,
            confidence=confidence,
            quality_gate=quality_gate,
            meta=meta,
            evidence_id=evidence_id,
        )
        return str(row["id"])

    @_serialized
    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                f"SELECT * FROM {EVIDENCE_TABLE} WHERE id = ?", (evidence_id,)
            ).fetchone()
        )
        return None if row is None else _evidence_dict(row)

    @_serialized
    def list_evidence(self, task_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {EVIDENCE_TABLE} WHERE task_id = ? {_APPEND_ORDER}"
        parameters: list[Any] = [task_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return [_evidence_dict(row) for row in _rows(self._connection.execute(sql, parameters))]

    @_serialized
    def list_unattributed(self, limit: int = 50) -> list[dict[str, Any]]:
        """The operator's reattribution inbox: evidence no card claims, oldest first."""
        return [
            _evidence_dict(row)
            for row in _rows(
                self._connection.execute(
                    f"SELECT * FROM {EVIDENCE_TABLE} WHERE task_id IS NULL {_APPEND_ORDER} LIMIT ?",
                    (int(limit),),
                )
            )
        ]

    @_serialized
    def is_manual(self, evidence_id: str) -> bool:
        """Whether this row is a HUMAN attribution a sweep must leave alone.

        The sweep guard. Any future deterministic re-attribution pass asks this
        first: a person who corrected a row already knows something the
        collector does not, and a sweep that overwrites them teaches everyone
        that correcting the board is pointless.
        """
        row = self._connection.execute(
            f"SELECT attribution FROM {EVIDENCE_TABLE} WHERE id = ?", (evidence_id,)
        ).fetchone()
        return row is not None and str(row["attribution"]) == "manual"

    @_serialized
    def reattribute_evidence(
        self, evidence_id: str, task_id: str | None, actor: str
    ) -> dict[str, Any] | None:
        """Move one piece of evidence to another card (or to unattributed).

        Marks the row ``manual`` and records the PRIOR link under
        ``meta_json['correction']``, because a corrected attribution that
        destroys what it corrected cannot be audited or undone. Both the card
        losing the evidence and the card gaining it get a ``task_events`` row,
        so neither trail has an unexplained gap.

        Returns the updated row, or None when no such evidence exists.
        """

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {EVIDENCE_TABLE} WHERE id = ?", (evidence_id,)
                ).fetchone()
            )
            if current is None:
                return None
            if task_id is not None:
                target = connection.execute(
                    f"SELECT id FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
                if target is None:
                    raise ValueError(f"task not found: {task_id}")
            previous = None if current["task_id"] is None else str(current["task_id"])
            meta = _evidence_dict(current)["meta"]
            meta["correction"] = {
                "from_task_id": previous,
                "to_task_id": task_id,
                "actor": actor,
                "at": utc_now_iso(),
            }
            connection.execute(
                f"UPDATE {EVIDENCE_TABLE} SET task_id = ?, attribution = 'manual', "
                "meta_json = ? WHERE id = ?",
                (task_id, _json(meta), evidence_id),
            )
            note = f"{current['kind']}:{current['ref']}"
            if previous is not None and previous != task_id:
                append_task_event(
                    connection,
                    task_id=previous,
                    actor=actor,
                    event="evidence",
                    note=f"detached {note}",
                )
            if task_id is not None and task_id != previous:
                append_task_event(
                    connection,
                    task_id=task_id,
                    actor=actor,
                    event="evidence",
                    note=f"attached {note}",
                )
            updated = _row(
                connection.execute(
                    f"SELECT * FROM {EVIDENCE_TABLE} WHERE id = ?", (evidence_id,)
                ).fetchone()
            )
            assert updated is not None
            return _evidence_dict(updated)

        return self._store._execute_write_txn(body, op="team.reattribute_evidence")

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    @_serialized
    def record_verification(self, task_id: str, verifier: str) -> VerificationResult | None:
        """Stamp a DONE card as verified. The RICH spelling of :meth:`verify_task`.

        Returns a :class:`VerificationResult` — the card, the id of the
        ``verify`` event this call minted, and whether it was the card's FIRST
        successful verification — or None when no such card exists.

        All three come out of the SAME transaction, and that is the whole point.
        The caller that decides "is this the first success?" from the card's
        ``verified_at`` gets it wrong the moment a card has been failed or
        unverified in between (both clear the stamp, so the next pass looks
        like a first one), and two concurrent verifies would both read NULL and
        both call themselves first. Counting prior ``verify`` EVENTS inside the
        write transaction cannot be fooled by either: the trail is append-only,
        and the count is taken under the same lock as the append.

        Two admissible paths, and the distinction is the whole point:

        * **Mechanical.** The card carries evidence a machine graded — a
          ``test_run`` or a merged ``pr`` at ``quality_gate='pass'`` that a
          COLLECTOR filed (``attribution='deterministic'``). Anyone may stamp it,
          including the owner, because the claim being recorded is the test
          runner's, not the verifier's. A hand-filed ``test_run`` is NOT that
          claim: ``POST /api/team/tasks/{id}/evidence`` writes ``manual`` rows,
          so without this filter an owner could type "my tests passed" and then
          verify themselves on the strength of their own sentence — the exact
          self-verification the human path refuses.
        * **Human.** No mechanical evidence, so somebody's judgement is the only
          thing standing behind "done". That somebody must not be the person who
          did the work — except the operator, who has nobody to counter-sign
          with and whose cards would otherwise be unverifiable forever.

        Raises ``ValueError`` when the card is not ``done`` (verification is a
        statement about finished work) or when a self-verification is refused.
        """

        def body(connection: sqlite3.Connection) -> VerificationResult | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )
            if current is None:
                return None
            status = str(current["status"])
            if status != BoardTaskStatus.DONE.value:
                raise ValueError(f"task {task_id} is {status}; only a done task can be verified")
            placeholders = ", ".join("?" for _ in MECHANICAL_EVIDENCE_KINDS)
            mechanical = connection.execute(
                f"SELECT id FROM {EVIDENCE_TABLE} WHERE task_id = ? "
                f"AND kind IN ({placeholders}) AND quality_gate = 'pass' "
                "AND attribution = 'deterministic' LIMIT 1",
                (task_id, *sorted(MECHANICAL_EVIDENCE_KINDS)),
            ).fetchone()
            if mechanical is None:
                owner = current["owner_employee_id"]
                self_verifying = owner is not None and str(owner) == verifier
                if self_verifying and verifier != OPERATOR_EMPLOYEE_ID:
                    raise ValueError(
                        f"{verifier} cannot verify their own task {task_id} without "
                        "mechanical evidence (a passing test_run or a merged pr)"
                    )
            now = utc_now_iso()
            # A RE-verification keeps the ORIGINAL stamp. Scoring counts a card
            # in the window its verified_at falls in, so unverify → re-verify
            # would otherwise move the same finished card into this week and
            # count it a second time — a free point for withdrawing a
            # verification and re-applying it.
            first_verify = connection.execute(
                f"SELECT created_at FROM {EVENTS_TABLE} WHERE task_id = ? AND event = 'verify' "
                "ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (task_id,),
            ).fetchone()
            # The SAME read answers "is this the first success?" — one query,
            # one truth, taken before this call appends its own verify event.
            first_success = first_verify is None
            stamped_at = now if first_verify is None else str(first_verify["created_at"])
            # A good verify REPAIRS a previously failed one, in the same UPDATE:
            # the card is verified now, so leaving verification_failed_* set
            # would render the tri-state as both at once. The reason is not
            # lost — every 'verify_failed' event still carries it (append-only).
            connection.execute(
                f"UPDATE {BOARD_TABLE} SET verified_at = ?, verified_by = ?, "
                "verification_failed_at = NULL, verification_failed_by = NULL, "
                "verification_failed_reason = NULL, updated_at = ? WHERE id = ?",
                (stamped_at, verifier, now, task_id),
            )
            event_id = append_task_event(
                connection,
                task_id=task_id,
                actor=verifier,
                event="verify",
                from_status=status,
                to_status=status,
                note="mechanical" if mechanical is not None else "human",
            )
            card = _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )
            assert card is not None  # read back inside the same transaction
            return VerificationResult(
                card=card, event_id=str(event_id), first_success=first_success
            )

        return self._store._execute_write_txn(body, op="team.verify_task")

    def verify_task(self, task_id: str, verifier: str) -> dict[str, Any] | None:
        """:meth:`record_verification`, returning only the card.

        The historical spelling, kept because thirty-odd callers across the
        estate (the inference sweep, the queue importer, the /task engine's
        tests) want the row and nothing else — the same relationship
        :meth:`add_evidence` has to :meth:`record_evidence`. Callers that must
        attribute a learning capture to the exact event this minted use the
        rich spelling; nobody should re-derive either fact by re-reading the
        trail afterwards, because by then another writer may have appended.
        """
        result = self.record_verification(task_id, verifier)
        return None if result is None else result.card

    @_serialized
    def record_verification_failure(
        self, task_id: str, verifier: str, reason: str
    ) -> VerificationResult | None:
        """Record a REFUSED verification. The rich spelling of :meth:`fail_verification`.

        Returns a :class:`VerificationResult` (card + the ``verify_failed``
        event this call minted; ``first_success`` is always False — a refusal
        is not a success) or None when no such card exists.

        The third completion state (migration 132). Until now a verifier who
        looked at a done card and found it wanting had exactly one move —
        say nothing — and the card kept reading "done, unverified", which is
        also what a card nobody has looked at reads. Those are different
        answers and the board now distinguishes them.

        The rules mirror :meth:`verify_task` deliberately, because a refusal is
        a verification decision and must not be easier to make than a pass:

        * the card must be ``done`` (there is nothing to refuse otherwise);
        * a ``reason`` is REQUIRED — a refusal with no reason is unactionable,
          and the owner cannot repair what nobody described;
        * the owner may not refuse their own card (the operator excepted, for
          the same reason the human verify path excepts them);
        * a BASELINE card is immutable, exactly as in :meth:`unverify_task`:
          the baseline is everyone's ``production_x`` denominator, and a
          refusal that unstamped one would silently shrink it and inflate the
          ratio. ``ValueError('baseline_immutable')``.

        Both verification stamps are cleared (a card cannot be verified AND
        failed), and the ``verify_failed`` event carrying the reason is written
        in the SAME transaction — that event is the durable record, since a
        later good verify clears the columns.

        Unlike the pass path there is no mechanical-evidence shortcut: a
        machine's passing test does not make a human's refusal self-service,
        and the anti-self-verification rule is about the OWNER's judgement of
        their own work either way.
        """
        if not str(reason).strip():
            raise ValueError("reason is required to fail a verification")

        def body(connection: sqlite3.Connection) -> VerificationResult | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )
            if current is None:
                return None
            if str(current["source"] or "") == BASELINE_SOURCE:
                raise ValueError("baseline_immutable")
            status = str(current["status"])
            if status != BoardTaskStatus.DONE.value:
                raise ValueError(
                    f"task {task_id} is {status}; only a done task can fail verification"
                )
            owner = current["owner_employee_id"]
            if owner is not None and str(owner) == verifier and verifier != OPERATOR_EMPLOYEE_ID:
                raise ValueError(f"{verifier} cannot fail-verify their own task {task_id}")
            now = utc_now_iso()
            connection.execute(
                f"UPDATE {BOARD_TABLE} SET verification_failed_at = ?, "
                "verification_failed_by = ?, verification_failed_reason = ?, "
                "verified_at = NULL, verified_by = NULL, updated_at = ? WHERE id = ?",
                (now, verifier, str(reason).strip(), now, task_id),
            )
            event_id = append_task_event(
                connection,
                task_id=task_id,
                actor=verifier,
                event="verify_failed",
                from_status=status,
                to_status=status,
                note=str(reason).strip(),
            )
            _annotate_delivered_commitments(connection, task_id, now)
            card = _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )
            assert card is not None  # read back inside the same transaction
            return VerificationResult(card=card, event_id=str(event_id), first_success=False)

        return self._store._execute_write_txn(body, op="team.fail_verification")

    def fail_verification(self, task_id: str, verifier: str, reason: str) -> dict[str, Any] | None:
        """:meth:`record_verification_failure`, returning only the card."""
        result = self.record_verification_failure(task_id, verifier, reason)
        return None if result is None else result.card

    @_serialized
    def unverify_task(self, task_id: str, actor: str) -> dict[str, Any] | None:
        """Withdraw a verification. Returns the card, or None if absent.

        Deliberately unconditional on who verified it: a wrong verification that
        only its author may withdraw is a wrong verification that outlives them.
        The event names who withdrew it.

        One exception, and it is not about trust: a BASELINE card
        (``source='baseline-2026-08-03'``) is the denominator of everyone's
        ``production_x``. Withdrawing its verification silently divides this
        week's points by a smaller baseline and inflates the ratio, so a
        baseline is re-stated by a re-import, never by an API call — including
        the operator's (``ValueError('baseline_immutable')``).
        """

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )
            if current is None:
                return None
            if str(current["source"] or "") == BASELINE_SOURCE:
                raise ValueError("baseline_immutable")
            connection.execute(
                f"UPDATE {BOARD_TABLE} SET verified_at = NULL, verified_by = NULL, "
                "updated_at = ? WHERE id = ?",
                (utc_now_iso(), task_id),
            )
            append_task_event(
                connection,
                task_id=task_id,
                actor=actor,
                event="unverify",
                note="" if current["verified_by"] is None else str(current["verified_by"]),
            )
            return _row(
                connection.execute(
                    f"SELECT * FROM {BOARD_TABLE} WHERE id = ?", (task_id,)
                ).fetchone()
            )

        return self._store._execute_write_txn(body, op="team.unverify_task")

    # ------------------------------------------------------------------
    # events (read)
    # ------------------------------------------------------------------

    @_serialized
    def list_events(self, task_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """One card's audit trail, oldest first."""
        sql = f"SELECT * FROM {EVENTS_TABLE} WHERE task_id = ? {_APPEND_ORDER}"
        parameters: list[Any] = [task_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters))

    @_serialized
    def append_comment(self, task_id: str, *, actor: str, note: str) -> str:
        """Append ONE ``comment`` event. Returns the event id.

        The trail's general-purpose annotation write, used by the learning hook
        for its durable ``learning_capture`` markers. Deliberately its own tiny
        transaction: the hook runs AFTER a verification has committed, so its
        marker must not be able to reopen — let alone roll back — that write.
        """

        def body(connection: sqlite3.Connection) -> str:
            return append_task_event(
                connection, task_id=task_id, actor=actor, event="comment", note=note
            )

        return cast(str, self._store._execute_write_txn(body, op="team.append_comment"))

    # ------------------------------------------------------------------
    # queues
    # ------------------------------------------------------------------

    @_serialized
    def pool_cards(self, *, limit: int | None = None) -> list[QueueCard]:
        """Protocol-conformant, unowned work available for a person to claim.

        Ordered by PRIORITY first (see :data:`_PRIORITY_RANK_SQL`) so a ``!top``
        card is the one a person sees when the pool is truncated, then oldest
        first — the tie-break the queues have always used.

        Projects the company join (:data:`_COMPANY_JOIN`) so every pool card
        names its company (slug + display name) as server truth. LEFT joins:
        a goal whose company row is gone degrades to NULL company fields, and
        the card stays in the pool.
        """
        limit_sql = ""
        parameters: list[Any] = [BoardTaskStatus.OPEN.value, BASELINE_SOURCE]
        if limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(int(limit))
        rows = _rows(
            self._connection.execute(
                "SELECT b.id, b.title, b.ref, b.status, b.size, b.priority, "
                f"b.owner_employee_id, b.source, b.due_date, {_COMPANY_COLUMNS} "
                f"FROM {BOARD_TABLE} b {_COMPANY_JOIN} "
                "WHERE b.status = ? AND b.owner_employee_id IS NULL "
                "AND b.archived_at IS NULL AND b.parent_task_id IS NULL AND b.source <> ? "
                "AND b.goal_id IS NOT NULL AND TRIM(b.acceptance_criteria) <> '' "
                f"ORDER BY {_PRIORITY_RANK_SQL}, b.created_at ASC, b.id ASC{limit_sql}",
                parameters,
            )
        )
        return [QueueCard.from_row(row) for row in rows]

    @_serialized
    def pool_depth(self) -> int:
        """True pool depth, independent of the board response card limit."""
        row = self._connection.execute(
            f"SELECT COUNT(*) AS depth FROM {BOARD_TABLE} "
            "WHERE status = ? AND owner_employee_id IS NULL "
            "AND archived_at IS NULL AND parent_task_id IS NULL AND source <> ? "
            "AND goal_id IS NOT NULL AND TRIM(acceptance_criteria) <> ''",
            (BoardTaskStatus.OPEN.value, BASELINE_SOURCE),
        ).fetchone()
        return 0 if row is None else int(row["depth"])

    @_serialized
    def pool_payload(self) -> dict[str, Any]:
        """Bounded API envelope with signals derived from the true COUNT."""
        cards = self.pool_cards(limit=POOL_CARD_LIMIT)
        depth = self.pool_depth()
        return {
            "cards": [card.model_dump() for card in cards],
            "depth": depth,
            "low": depth < POOL_DEPTH_FLOOR,
            "truncated": depth > len(cards),
        }

    @_serialized
    def team_queues(
        self,
        *,
        employee_ids: list[str] | None = None,
        today: str | None = None,
    ) -> dict[str, TeamQueueBuckets]:
        """Every person's board, bucketed, keyed by employee id.

        Only OWNED, unarchived cards appear: an agent card with no
        ``owner_employee_id`` belongs to no person's queue, and putting it in
        one would make the Ready count a number nobody can act on.

        Every bucket is ordered by PRIORITY first (:data:`_PRIORITY_RANK_SQL`),
        then oldest first — so ``bucket.ready[0]`` is the card the person should
        pick up, and the pulse's urgent markers need no second sort.

        ``employee_ids`` scopes the answer; the default is the whole
        ``employees`` roster, which is COMPLETE rather than merely convenient —
        migration 123 gives ``owner_employee_id`` a foreign key, so an owner who
        is not on the roster cannot be persisted in the first place, and no card
        can hide behind a missing person. ``today`` (a ``YYYY-MM-DD`` UTC day)
        overrides the DoneToday window for tests.

        A person with an empty board gets an EMPTY bucket set, not an absent
        key: "nothing to do" and "not on the team" are different answers, and
        only one of them should make a queue disappear from the view.
        """
        day = today or utc_now_iso()[:10]
        if employee_ids is None:
            from omniagentos.team.scoring import ROSTER_SQL

            wanted = [str(row["id"]) for row in self._connection.execute(ROSTER_SQL)]
        else:
            wanted = list(dict.fromkeys(employee_ids))
        if not wanted:
            return {}

        buckets = {employee_id: TeamQueueBuckets(employee_id=employee_id) for employee_id in wanted}
        placeholders = ", ".join("?" for _ in wanted)
        rows = _rows(
            self._connection.execute(
                "SELECT b.id, b.title, b.ref, b.status, b.size, b.priority, "
                f"b.owner_employee_id, b.source, b.due_date, b.updated_at, {_COMPANY_COLUMNS} "
                f"FROM {BOARD_TABLE} b {_COMPANY_JOIN} "
                f"WHERE b.owner_employee_id IN ({placeholders}) AND b.archived_at IS NULL "
                f"ORDER BY {_PRIORITY_RANK_SQL}, b.created_at ASC, b.id ASC",
                tuple(wanted),
            )
        )
        by_status = {status: name for name, statuses in _QUEUE_BUCKETS for status in statuses}
        for row in rows:
            employee_id = str(row["owner_employee_id"])
            bucket = buckets.get(employee_id)
            if bucket is None:  # pragma: no cover -- the IN clause guarantees membership
                continue
            status = str(row["status"])
            card = QueueCard.from_row(row)
            name = by_status.get(status)
            if name is not None:
                getattr(bucket, name).append(card)
            elif status == BoardTaskStatus.DONE.value and str(row["updated_at"])[:10] == day:
                bucket.done_today.append(card)
        return buckets

    # ------------------------------------------------------------------
    # daily commitments (migration 132)
    # ------------------------------------------------------------------

    @_serialized
    def create_commitment(
        self,
        *,
        day: str,
        employee_id: str,
        kind: str,
        title: str,
        task_id: str | None = None,
        slot: int = 1,
        expected_outcome: str = "",
        source: str = "auto",
        status: str = "committed",
        carried_from: str | None = None,
        commitment_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Record one commitment. IDEMPOTENT on the unique keys (132/133).

        Returns ``(row, outcome)`` where outcome is ``created`` or ``exists``.
        The generator re-runs — a re-run at 07:00 after a 06:55 run must return
        the SAME row, not a second commitment, so the insert is
        ``INSERT OR IGNORE`` against the two partial unique indexes
        (``(day, employee, task)`` for a card, ``(day, employee, kind, slot)``
        for a slotted one) and the outcome names which happened. A caller that
        needs to know whether it created the row (the carry mint does) gets a
        straight answer instead of inferring it from an exception.

        ``slot`` numbers the daily slots: 1 for a task commitment (unslotted —
        its identity is its card) and for the single improvement, 1..3 for the
        automation slots (:data:`AUTOMATION_SLOTS_PER_DAY`).
        """
        _one_of("kind", kind, COMMITMENT_KINDS)
        _one_of("source", source, COMMITMENT_SOURCES)
        _one_of("status", status, COMMITMENT_STATUSES)
        _valid_slot(kind, slot)
        if kind in SLOTTED_COMMITMENT_KINDS and task_id is not None:
            # A slotted commitment names no card up front — that is what makes
            # it a standing expectation rather than a promise about one piece of
            # work. Refused HERE with a readable message because the alternative
            # is worse than an error: the row would be unique-indexed by slot but
            # looked up by card, so an idempotent re-run would fail to find it.
            raise ValueError(f"a {kind} commitment cannot name a task_id")
        if not str(title).strip():
            raise ValueError("title is required (it is what the person committed to)")
        row_id = commitment_id or new_id(COMMITMENT_ID_PREFIX)
        now = utc_now_iso()

        def body(connection: sqlite3.Connection) -> tuple[dict[str, Any], str]:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO {COMMITMENTS_TABLE} "
                "(id, day, employee_id, task_id, kind, slot, title, expected_outcome, status, "
                "source, carried_from, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    day,
                    employee_id,
                    task_id,
                    kind,
                    _valid_slot(kind, slot),
                    str(title).strip(),
                    expected_outcome,
                    status,
                    source,
                    carried_from,
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                stored = _row(
                    connection.execute(
                        f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (row_id,)
                    ).fetchone()
                )
                assert stored is not None
                return stored, "created"
            existing = _existing_commitment(connection, day, employee_id, kind, task_id, slot)
            assert existing is not None, "INSERT OR IGNORE only skips on a unique collision"
            return existing, "exists"

        return self._store._execute_write_txn(body, op="team.create_commitment")

    @_serialized
    def get_commitment(self, commitment_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (commitment_id,)
            ).fetchone()
        )

    @_serialized
    def list_commitments(
        self,
        *,
        day: str | None = None,
        employee_id: str | None = None,
        status: str | Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Commitments, newest day first, then by person and creation order.

        ``status`` takes one value or a SET of them, because the two questions
        callers ask are "which rows are in state X" and "which rows are still
        OPEN" — and open is two states, ``committed`` and ``carried`` (a carried
        row is work the person still owes, so the next morning must judge it).
        """
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (("day", day), ("employee_id", employee_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if status is not None:
            wanted = [status] if isinstance(status, str) else list(status)
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            parameters.extend(wanted)
        sql = f"SELECT * FROM {COMMITMENTS_TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # kind DESC reads 'task' > 'improvement' > 'automation': the day's cards
        # first, then the standing slots, which is the order every surface
        # renders. ``slot`` sorts before created_at because a generator writes
        # all three automation rows inside one second — without it the slots
        # would come back in an order that changes between runs.
        sql += " ORDER BY day DESC, employee_id ASC, kind DESC, slot ASC, created_at ASC, rowid ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters))

    @_serialized
    def resolve_commitment(
        self,
        commitment_id: str,
        *,
        status: str,
        resolution_note: str,
        resolved_by: str = "system",
        resolved_at: str | None = None,
        carry: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve ONE commitment — and, atomically, mint its carry.

        Only an OPEN row may be resolved
        (:data:`~omniagentos.team.contracts.OPEN_COMMITMENT_STATUSES`:
        ``committed`` or ``carried``). A resolution is history, and a miss that
        can be rewritten to delivered tomorrow is not accountability (S10);
        re-resolving is a strict no-op returning the row unchanged, which is
        what makes ``resolve_day`` re-runnable by both the 06:55 job and the
        07:00 report.

        ``carried`` is open, not terminal. It marks PROVENANCE — "this slipped
        from yesterday", which the morning DM renders — and the person still
        owes the work, so the next morning judges it exactly like any other
        commitment. Treating it as terminal left carried work permanently
        unjudged, which is the one thing this table exists to prevent.

        ``carry`` (a ``create_commitment`` kwargs dict) is minted IN THE SAME
        TRANSACTION as the miss it follows from. A crash between the two writes
        would otherwise leave a missed commitment whose work nobody picked back
        up — the one outcome the carry rule exists to prevent. If the next-day
        row already exists (the 06:55 generator got there first), it is LINKED
        (``carried_from`` set) rather than inserted, so the collision is a link,
        never an IntegrityError.
        """
        _one_of("status", status, COMMITMENT_STATUSES)
        now = resolved_at or utc_now_iso()

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (commitment_id,)
                ).fetchone()
            )
            if current is None:
                return None
            if str(current["status"]) not in OPEN_COMMITMENT_STATUSES:
                return current
            open_states = ", ".join("?" for _ in OPEN_COMMITMENT_STATUSES)
            connection.execute(
                f"UPDATE {COMMITMENTS_TABLE} SET status = ?, resolution_note = ?, "
                "resolved_at = ?, resolved_by = ?, updated_at = ? WHERE id = ? "
                f"AND status IN ({open_states})",
                (
                    status,
                    resolution_note,
                    now,
                    resolved_by,
                    now,
                    commitment_id,
                    *OPEN_COMMITMENT_STATUSES,
                ),
            )
            if carry is not None:
                _mint_carry(connection, carried_from=commitment_id, at=now, **carry)
            return _row(
                connection.execute(
                    f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (commitment_id,)
                ).fetchone()
            )

        return self._store._execute_write_txn(body, op="team.resolve_commitment")

    @_serialized
    def resolve_commitments(
        self,
        entries: Sequence[tuple[str, str, str]],
        *,
        resolved_by: str = "system",
        resolved_at: str | None = None,
    ) -> int:
        """Resolve SEVERAL commitments in ONE transaction. Returns rows written.

        ``entries`` is ``(commitment_id, status, resolution_note)``.

        The atomicity is the point, not an optimisation. One employee's three
        automation slots are resolved against ONE snapshot of their qualifying
        cards; resolving them row-by-row means a crash (or a lock timeout)
        between two of them leaves the day half-judged, and the retry computes a
        FRESH snapshot — one that may have gained a card whose done event landed
        earlier in the day. The already-frozen slot keeps its assignment while
        the retry re-derives the rest from different data, which is how the same
        card ends up filling two slots and how a mis-assignment becomes
        permanent. Either every slot is judged from that snapshot or none is.

        Each row keeps the same OPEN-only guard :meth:`resolve_commitment`
        applies, so an already-resolved row inside the batch is skipped rather
        than rewritten.
        """
        now = resolved_at or utc_now_iso()
        for _identifier, status, _note in entries:
            _one_of("status", status, COMMITMENT_STATUSES)

        def body(connection: sqlite3.Connection) -> int:
            open_states = ", ".join("?" for _ in OPEN_COMMITMENT_STATUSES)
            written = 0
            for identifier, status, note in entries:
                cursor = connection.execute(
                    f"UPDATE {COMMITMENTS_TABLE} SET status = ?, resolution_note = ?, "
                    "resolved_at = ?, resolved_by = ?, updated_at = ? WHERE id = ? "
                    f"AND status IN ({open_states})",
                    (
                        status,
                        note,
                        now,
                        resolved_by,
                        now,
                        identifier,
                        *OPEN_COMMITMENT_STATUSES,
                    ),
                )
                written += cursor.rowcount
            return written

        return cast(int, self._store._execute_write_txn(body, op="team.resolve_commitments"))

    @_serialized
    def append_resolution_note(
        self, commitment_id: str, note: str, *, actor: str
    ) -> dict[str, Any] | None:
        """Append to a RESOLVED row's note — the only edit history admits.

        The operator's channel for "this miss was a blocked dependency, not a
        slip". It appends; it never replaces, and it never touches ``status``.
        """
        text = str(note).strip()
        if not text:
            raise ValueError("note is required")
        now = utc_now_iso()

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (commitment_id,)
                ).fetchone()
            )
            if current is None:
                return None
            connection.execute(
                f"UPDATE {COMMITMENTS_TABLE} SET resolution_note = TRIM(resolution_note || ?), "
                "updated_at = ? WHERE id = ?",
                (f" [{actor}] {text}", now, commitment_id),
            )
            return _row(
                connection.execute(
                    f"SELECT * FROM {COMMITMENTS_TABLE} WHERE id = ?", (commitment_id,)
                ).fetchone()
            )

        return self._store._execute_write_txn(body, op="team.append_resolution_note")

    @_serialized
    def mint_carry(
        self,
        *,
        carried_from: str,
        day: str,
        employee_id: str,
        task_id: str,
        title: str,
        expected_outcome: str = "",
        at: str | None = None,
    ) -> tuple[str, str]:
        """The repair sweep's carry write: :func:`_mint_carry` in its own txn.

        Exactly the primitive the atomic miss path uses, so the two carry paths
        can never disagree about what "already carried" means. Returns
        ``(commitment_id, 'created'|'linked'|'exists')``.
        """
        payload = {
            "carried_from": carried_from,
            "day": day,
            "employee_id": employee_id,
            "task_id": task_id,
            "title": title,
            "expected_outcome": expected_outcome,
            "at": at or utc_now_iso(),
        }

        def body(connection: sqlite3.Connection) -> tuple[str, str]:
            return _mint_carry(connection, **payload)

        return cast(tuple[str, str], self._store._execute_write_txn(body, op="team.mint_carry"))

    # ------------------------------------------------------------------
    # productivity snapshots
    # ------------------------------------------------------------------

    @_serialized
    def upsert_snapshot(
        self,
        *,
        day: str,
        employee_id: str,
        verified_points: int = 0,
        verified_outcomes: int = 0,
        avg_active_sessions: float | None = None,
        peak_sessions: int | None = None,
        merged_prs: int | None = None,
        first_pass_rate: float | None = None,
        production_x: float | None = None,
        breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write the (day, employee) row, replacing any earlier value for that day.

        A recompute for a day REPLACES it rather than appending: two rows for
        one person-day is two answers to one question, and every roll-up
        downstream would have to guess which one is current.
        """
        values: dict[str, Any] = {
            "verified_points": int(verified_points),
            "verified_outcomes": int(verified_outcomes),
            "avg_active_sessions": avg_active_sessions,
            "peak_sessions": peak_sessions,
            "merged_prs": merged_prs,
            "first_pass_rate": first_pass_rate,
            "production_x": production_x,
        }
        assignments = ", ".join(f"{column} = excluded.{column}" for column in _SNAPSHOT_COLUMNS)
        self._store._write(
            f"INSERT INTO {SNAPSHOTS_TABLE} "
            "(day, employee_id, verified_points, verified_outcomes, avg_active_sessions, "
            "peak_sessions, merged_prs, first_pass_rate, production_x, breakdown_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (day, employee_id) DO UPDATE SET "
            f"{assignments}, breakdown_json = excluded.breakdown_json",
            (
                day,
                employee_id,
                *(values[column] for column in _SNAPSHOT_COLUMNS),
                _json(breakdown or {}),
            ),
        )
        stored = self.get_snapshot(day, employee_id)
        assert stored is not None
        return stored

    @_serialized
    def get_snapshot(self, day: str, employee_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {SNAPSHOTS_TABLE} WHERE day = ? AND employee_id = ?",
                (day, employee_id),
            ).fetchone()
        )

    @_serialized
    def list_snapshots(
        self, *, day: str | None = None, employee_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if day is not None:
            clauses.append("day = ?")
            parameters.append(day)
        if employee_id is not None:
            clauses.append("employee_id = ?")
            parameters.append(employee_id)
        sql = f"SELECT * FROM {SNAPSHOTS_TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY day DESC, employee_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters))
