"""SQLite persistence for the agent collaboration layer (board + messaging).

Composes the H1 :class:`SqliteStore` connection so migration 004 is applied with
the rest of the schema. Thread-safe via the H1 store lock; ``claim_task`` uses
BEGIN IMMEDIATE compare-and-swap so exactly one agent wins a concurrent race.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, cast

from omniagentos.collab.contracts import (
    AUTOMATION_MATURITY,
    AUTOMATION_PROPOSAL_SOURCE,
    BASELINE_SOURCE,
    OPERATOR_EMPLOYEE_ID,
    TASK_ADHOC_SOURCE,
    TASK_PRIORITIES,
    Agent,
    BoardTask,
    BoardTaskStatus,
    Channel,
    ChannelKind,
    Message,
    can_claim,
)
from omniagentos.contracts import new_id, utc_now_iso

_BOARD_UPDATE_COLUMNS = frozenset(
    {
        "title",
        "description",
        "required_expertise",
        "discipline",
        "priority",
        "status",
        "result_ref",
        # Intake loop (migration 016): the nullable link from a board card to the
        # control-plane run that executes it. Settable via PATCH so dispatch can
        # bind a freshly-enqueued run and the board reconciler can (re)affirm it.
        "run_id",
        # Archive support (migration 035): nullable timestamp when task was archived.
        "archived_at",
        # Multidimensional org (migration 061): JSON envelope for company/product/
        # workstream/domain/channel/risk classification.
        "org_json",
        # Project scope (migration 087): dispatch persists it at creation and
        # chat PATCH mirrors it, so /api/board?project_id= filters server-side.
        "project_id",
        # --- Team Work OS (migration 123) ----------------------------------
        # ``verified_at``/``verified_by`` are deliberately ABSENT: verification
        # is the one claim on this table that a card's own owner must not be
        # able to make for themselves, so it is settable only through
        # ``TeamStore.verify_task`` (which enforces mechanical-evidence or
        # second-person rules) — exactly the protection ``claimed_by`` has
        # against ``claim_task``. A PATCH naming them raises "unknown columns".
        "parent_task_id",
        "goal_id",
        "owner_employee_id",
        "ref",
        "size",
        "acceptance_criteria",
        "blocked_reason",
        "due_date",
        "source",
        # --- developer accountability (migration 132) ----------------------
        # The automation axis IS patchable: "how much did the system do, and
        # what could it do itself next time" is a judgement the person closing
        # the card makes, and nothing downstream scores it.
        "automation_maturity",
        "automation_note",
        # ``verification_failed_at``/``_by``/``_reason`` are ABSENT for exactly
        # the reason ``verified_at``/``verified_by`` are: a card's own owner
        # must not be able to stamp — or unstamp — a verification verdict.
        # Only ``TeamStore.fail_verification`` sets them; a successful verify or
        # a reopen (below) clears them.
    }
)

# Board statuses a SUBTASK may sit in without holding its parent open. Anything
# else (open/claimed/in_progress/awaiting_approval/blocked/pending) means the
# parent's work is demonstrably unfinished.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {BoardTaskStatus.DONE.value, BoardTaskStatus.CANCELLED.value}
)

# Evidence kinds/quality are the TeamStore's business; the done-gate here only
# needs to know whether ANY evidence row is attached to the card.
_EVIDENCE_TABLE = "task_evidence"
_EVENTS_TABLE = "task_events"

# Writing ANY of these re-evaluates the blocked/done rules against the row as it
# will exist. They are the two predicates' whole vocabulary — status, who owns
# it, what it promised, why it is stuck, where it sits in the hierarchy, and
# what it is worth — so no single-field patch can arrive at a forbidden state by
# writing the halves of a predicate in two requests.
_TEAM_RULE_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "owner_employee_id",
        "acceptance_criteria",
        "blocked_reason",
        "parent_task_id",
        "size",
    }
)


# --- board LIST projection (payload discipline) -----------------------------
#
# List reads NEVER ``SELECT *``. ``board_tasks.swarm_json`` (migration 045) is an
# unbounded per-card envelope — one live board carried 47.8 MB of it across ~1k
# cards (``pre_attempt_dirty_paths`` alone), 96% of a 51 MB ``GET
# /api/collab/board`` response — while the only pieces any list consumer reads
# are the kanban phase overlay and the integration flag. So the list path
# projects every column EXCEPT ``swarm_json`` and flattens those two out of it
# with ``json_extract``. Single-card reads (``get_board_task``) still return the
# whole row: one envelope is affordable, a thousand are not.
#
# Adding a column to ``board_tasks``? Add it here too (or to
# ``_BOARD_LIST_OMITTED`` with a reason) — ``tests/collab/test_board_projection``
# fails on drift so a new column can never silently disappear from the board.
_BOARD_LIST_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "description",
    "required_expertise_json",
    "discipline",
    "priority",
    "status",
    "claimed_by",
    "claim_version",
    "result_ref",
    "created_at",
    "updated_at",
    "run_id",
    "archived_at",
    "category_id",
    "lane",
    "park_state",
    "longhaul_json",
    "swarm_run_id",
    "task_mode",
    "org_json",
    "origin",
    "preflight_json",
    "project_id",
    # Team Work OS (migration 123). The team board reads every one of these in
    # its list view — hierarchy, owner, size, verification stamp and the blocked
    # reason are what the queue columns and the "what is stuck" view render.
    "parent_task_id",
    "goal_id",
    "owner_employee_id",
    "ref",
    "size",
    "acceptance_criteria",
    "blocked_reason",
    "verified_at",
    "verified_by",
    "due_date",
    "source",
    # Developer accountability (migration 132). The board renders the
    # completion TRI-STATE (verified / failed_verification / unverified) and the
    # automation badge from the list read, so all five ride the projection —
    # omitting the failure stamps would make a refused card render as merely
    # unverified, which is the exact ambiguity 131 exists to remove.
    "automation_maturity",
    "automation_note",
    "verification_failed_at",
    "verification_failed_by",
    "verification_failed_reason",
)

# Columns deliberately withheld from list reads, and what replaces them.
_BOARD_LIST_OMITTED: frozenset[str] = frozenset({"swarm_json"})

# Flattened stand-ins for the withheld envelope. ``swarm_phase`` drives the
# Review/Testing/Integration kanban columns; ``swarm_integration`` marks the
# auto-appended integration card. Both are NULL/absent on non-swarm cards.
#
# The ``json_valid`` guard is load-bearing: bare ``json_extract`` on a malformed
# value raises ``OperationalError: malformed JSON`` and takes the WHOLE board
# read down with it, so one bad row would 500 the kanban for every card. The
# CASE feeds NULL instead, degrading that card to "no swarm metadata" (Running).
#
# ``company_slug`` (multi-company Work OS, 2026-08-13) is SERVER TRUTH derived
# from the card's goal: goal_id -> company_goals.org_company_id ->
# org_companies.slug. A correlated scalar subquery, not a JOIN, so the list SQL
# builder and every WHERE clause stay untouched; NULL when the card has no goal
# or the goal's company row is gone — never a guess. This is deliberately a
# DIFFERENT channel from the org_json envelope enrichment (which follows the
# project chain): the board's company chip must agree with the team queues,
# which read the same goal join.
_BOARD_LIST_DERIVED: tuple[str, ...] = (
    "json_extract(CASE WHEN json_valid(swarm_json) THEN swarm_json END, '$.swarm_phase')"
    " AS swarm_phase",
    "json_extract(CASE WHEN json_valid(swarm_json) THEN swarm_json END, '$.integration')"
    " AS swarm_integration",
    "(SELECT oc.slug FROM company_goals cg JOIN org_companies oc"
    " ON oc.id = cg.org_company_id WHERE cg.id = board_tasks.goal_id)"
    " AS company_slug",
)

_BOARD_LIST_PROJECTION = ", ".join((*_BOARD_LIST_COLUMNS, *_BOARD_LIST_DERIVED))


def _board_list_sql(conditions: list[str], limit: int | None) -> str:
    """Build a projected board LIST query (never ``SELECT *``)."""
    sql = f"SELECT {_BOARD_LIST_PROJECTION} FROM board_tasks"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
    return sql


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize access through the composed H1 store's connection lock."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("CollabStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _parse_json_list(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _agent_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["expertise"] = _parse_json_list(out.pop("expertise_json", "[]"))
    return out


def append_task_event(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    actor: str,
    event: str,
    from_status: str | None = None,
    to_status: str | None = None,
    note: str = "",
) -> str:
    """Append one ``task_events`` row on an ALREADY-OPEN transaction.

    Exported (no leading underscore) because ``omniagentos.team.store`` appends
    to the same audit trail for evidence/verify/unverify, and two spellings of
    "write an event" is how an audit trail acquires rows nobody can join. The
    caller owns the transaction: the event and the mutation it describes must
    commit together or not at all, so this never opens one of its own.
    """
    event_id = new_id("tve")
    connection.execute(
        f"INSERT INTO {_EVENTS_TABLE} "
        "(id, task_id, actor, event, from_status, to_status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, task_id, actor, event, from_status, to_status, note, utc_now_iso()),
    )
    return event_id


def _validate_team_rules(
    connection: sqlite3.Connection,
    task_id: str,
    current: dict[str, Any],
    written: dict[str, Any],
) -> None:
    """Refuse a patch that would break a migration-123 invariant.

    Runs on an open ``BEGIN IMMEDIATE`` transaction with ``current`` read inside
    it, so every probe below is authoritative against every other writer. Raises
    ``ValueError``; the transaction then rolls back and NOTHING is written.

    The blocked/done rules are evaluated against the EFFECTIVE row — the written
    fields overlaid on the current ones — not against the patch alone. Keying
    them on ``"status" in written`` made every gate reachable by writing the
    OTHER half of its predicate second: PATCH the card to done while it is
    unowned, then PATCH the owner on; blank the ``blocked_reason`` of a card
    already sitting in Blocked; add acceptance criteria to a done card that has
    no evidence. Each of those left the board in exactly the state the rule
    exists to forbid, and each returned 200.
    """
    ref = written.get("ref")
    if "ref" in written and ref is not None:
        clash = connection.execute(
            "SELECT id FROM board_tasks WHERE ref = ? AND id <> ? LIMIT 1", (ref, task_id)
        ).fetchone()
        if clash is not None:
            raise ValueError("ref_conflict")

    parent_id = written.get("parent_task_id")
    if "parent_task_id" in written and parent_id is not None:
        parent_id = str(parent_id)
        if parent_id == task_id:
            raise ValueError("parent_task_id cannot be the task itself")
        parent = connection.execute(
            "SELECT id, parent_task_id FROM board_tasks WHERE id = ?", (parent_id,)
        ).fetchone()
        if parent is None:
            raise ValueError(f"parent task not found: {parent_id}")
        if parent["parent_task_id"] is not None:
            raise ValueError(f"task hierarchy is one level deep: {parent_id} is itself a subtask")
        # Becoming a child while holding children would make a grandchild of
        # every one of them — the same violation, seen from the other end.
        child = connection.execute(
            "SELECT id FROM board_tasks WHERE parent_task_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if child is not None:
            raise ValueError(f"task {task_id} has subtasks and cannot become a subtask itself")

    if not (_TEAM_RULE_FIELDS & written.keys()):
        return
    effective = {**current, **written}
    status = str(effective.get("status") or "")
    owner = effective.get("owner_employee_id")

    if status == BoardTaskStatus.BLOCKED.value and owner is not None:
        reason = effective.get("blocked_reason") or ""
        if not str(reason).strip():
            raise ValueError(
                f"task {task_id} cannot be blocked without blocked_reason "
                "(an owned card in the Blocked queue must say what it is waiting on)"
            )

    if status == BoardTaskStatus.DONE.value and owner is not None:
        # Subtasks first, and OUTSIDE the acceptance-criteria branch: a parent
        # whose children are still open is unfinished whether or not anybody
        # wrote acceptance text for it, and nesting this check under that text
        # made "leave acceptance_criteria empty" a way to close a parent over
        # live children.
        placeholders = ", ".join("?" for _ in _TERMINAL_TASK_STATUSES)
        open_subtask = connection.execute(
            "SELECT id FROM board_tasks "
            f"WHERE parent_task_id = ? AND status NOT IN ({placeholders}) LIMIT 1",
            (task_id, *sorted(_TERMINAL_TASK_STATUSES)),
        ).fetchone()
        if open_subtask is not None:
            raise ValueError(
                f"task {task_id} cannot be done while subtask {open_subtask['id']} is open"
            )
        acceptance = effective.get("acceptance_criteria") or ""
        if not str(acceptance).strip():
            return
        evidence = connection.execute(
            f"SELECT id FROM {_EVIDENCE_TABLE} WHERE task_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if evidence is None:
            raise ValueError(
                f"task {task_id} cannot be done without evidence "
                "(it names an owner and acceptance criteria)"
            )


def _validate_actor_rules(
    task_id: str, current: dict[str, Any], written: dict[str, Any], actor: str
) -> None:
    """Refuse the two patches that would rewrite a number somebody already used.

    Both are about a card whose worth has ALREADY been counted:

    * **A verified card's size is frozen.** Points are size, so re-sizing a card
      after it was verified silently rewrites last week's score — the report
      that already went out no longer reproduces. The operator may still do it
      (a genuine mis-size has to be fixable by someone), and the event names
      them.
    * **A baseline card is frozen outright** — size, status and owner. Those
      cards ARE the denominator of ``production_x``; shrinking one raises the
      ratio of every week measured against it. Re-baselining is an import, not a
      PATCH, so this one binds the operator too.
    * **A card's OWNER may not re-size their own card** (multi-company Work OS,
      2026-08-13). Size is priced by the assigner and confirmed by the
      verifier; letting the person doing the work re-price it turns S->L into a
      free 7 points. The rule binds only a principal who IS the current owner:
      system/agent writes (``actor='system'``) and third-party principals are
      untouched, and the operator keeps the same exemption the verified-size
      rule grants — without it, an operator-owned verified card would be
      re-sizable by nobody at all (rule one admits ONLY the operator, this one
      would then refuse them as owner).
    """
    if str(current.get("source") or "") == BASELINE_SOURCE and (
        {"size", "status", "owner_employee_id"} & written.keys()
    ):
        raise ValueError("baseline_immutable")
    if "source" in written:
        # The Work/Tasks boundary (v4) and the baseline denominator are both
        # SCORING inputs riding this column: relabeling a Task as Work mints
        # points, the reverse erases a verified score, and stripping the
        # baseline marker inflates production_x. Stamped at creation, never
        # PATCHed across — binds every actor, operator included.
        before = str(current.get("source") or "")
        after = str(written.get("source") or "")
        protected = {TASK_ADHOC_SOURCE, BASELINE_SOURCE, AUTOMATION_PROPOSAL_SOURCE}
        if before != after and (before in protected or after in protected):
            raise ValueError("source_boundary_immutable")
    # An automation PROPOSAL is decided by ``/task approve`` | ``/task reject``
    # (team.tasks) and by nothing else. Those primitives hold the the operator-only gate,
    # write the assignee hint, the compute-pool envelope, the rejection comment
    # and the DMs; a generic PATCH to ``open``/``cancelled`` would produce a
    # decided card with none of that and no record that a decision was made.
    # The source is already immutable above, so this cannot be side-stepped by
    # stripping the marker first. Refused for EVERY actor, operator included:
    # the operator's own decisions go through the verbs, which is where the effects live.
    if (
        str(current.get("source") or "") == AUTOMATION_PROPOSAL_SOURCE
        and str(current.get("status") or "") == BoardTaskStatus.AWAITING_APPROVAL.value
        and "status" in written
        and str(written["status"]) != BoardTaskStatus.AWAITING_APPROVAL.value
    ):
        raise ValueError(
            "automation_proposal_decision_required (use /task approve <REF> or "
            "/task reject <REF> — the decision verbs carry the operator gate, the "
            "assignee hint, the dispatch envelope and the notifications)"
        )
    if (
        "size" in written
        and current.get("verified_at") is not None
        and actor != OPERATOR_EMPLOYEE_ID
    ):
        raise ValueError("verified_task_immutable_size")
    owner = current.get("owner_employee_id")
    if (
        "size" in written
        and owner is not None
        and str(owner) == actor
        and actor != OPERATOR_EMPLOYEE_ID
    ):
        raise ValueError(
            "owner_cannot_resize (size is assigner-priced and verifier-confirmed; "
            "ask the assigner or the operator to re-size)"
        )


def _append_update_event(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    actor: str,
    current: dict[str, Any],
    written: dict[str, Any],
) -> None:
    """Append the ``task_events`` rows that describe THIS mutation."""
    previous = str(current["status"])
    status_changed = "status" in written and str(written["status"]) != previous
    owner_changed = "owner_employee_id" in written and written["owner_employee_id"] != current.get(
        "owner_employee_id"
    )
    if status_changed:
        to_status = str(written["status"])
        note = ""
        if to_status == BoardTaskStatus.BLOCKED.value:
            note = str(written.get("blocked_reason", current["blocked_reason"]) or "")
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="status_change",
            from_status=previous,
            to_status=to_status,
            note=note,
        )
    if owner_changed:
        owner = written["owner_employee_id"] or "none"
        append_task_event(
            connection,
            task_id=task_id,
            actor=actor,
            event="assign",
            note=f"owner:{owner}",
        )
    if status_changed or owner_changed:
        return
    # No status move: name the mutation by what it touched, so the trail stays
    # readable without diffing rows.
    assigning = {"parent_task_id", "goal_id"} & written.keys()
    append_task_event(
        connection,
        task_id=task_id,
        actor=actor,
        event="assign" if assigning else "comment",
        note=", ".join(sorted(written)),
    )


def _normalized_ref(value: str | None) -> str | None:
    """Blank external ref is NULL; everything else is UPPERCASE.

    ``ref = ''`` would occupy the partial unique index, so the SECOND card
    created without a ref would collide with the first — a uniqueness rule
    firing on the absence of a value. Blank collapses to NULL, which the index
    does not cover.

    Case is normalized because three subsystems compare against this column and
    they did not agree: Slack resolves ``u3`` case-INsensitively, the attribution
    matcher compares a branch segment case-SENSITIVELY, and the unique index is
    case-sensitive — so ``u3`` and ``U3`` were two cards that Slack could not
    tell apart and the sweep could only attach evidence to one of. Storing one
    canonical spelling makes all three agree by construction.
    """
    if value is None:
        return None
    text = value.strip().upper()
    return text or None


def _normalized_project_id(value: str | None) -> str | None:
    """Blank project scope is NULL (M1).

    ``project_id = ''`` is an id no project has, so it would record a THIRD
    state — "scoped, to nothing" — between unscoped and scoped-to-P. The
    broker's C11 binding check already collapses blank to NULL on the grant
    side; the write paths collapse it here so the two agree.
    """
    if value is None:
        return None
    text = value.strip()
    return text or None


def _task_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["required_expertise"] = _parse_json_list(out.pop("required_expertise_json", "[]"))
    # Add archived boolean: true iff archived_at is not null
    out["archived"] = out.get("archived_at") is not None
    # Multidimensional org envelope (migration 061). Absent/empty → {}.
    raw_org = out.pop("org_json", None)
    if isinstance(raw_org, dict):
        out["org"] = raw_org
    elif isinstance(raw_org, str) and raw_org.strip() and raw_org.strip() != "{}":
        try:
            parsed = json.loads(raw_org)
            out["org"] = parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            out["org"] = {}
    else:
        out["org"] = {}
    # List projection only: ``json_extract`` yields SQLite's 1/0 for a JSON
    # boolean (and NULL when the key — or the whole envelope — is absent).
    # Normalize STRICTLY, not with ``bool()``: a wrongly-typed envelope such as
    # ``{"integration": "false"}`` extracts as the truthy string ``"false"`` and
    # would move the card into Integration. Only json_extract's true
    # representation (integer 1, or a real True) counts — everything else, of
    # any type, is False. Matches the envelope fallback's ``=== true``.
    if "swarm_integration" in out:
        raw_integration = out["swarm_integration"]
        out["swarm_integration"] = isinstance(raw_integration, int) and raw_integration == 1
    return out


class CollabStore:
    """Auto-migrated collab store sharing the H1 SQLite database and connection discipline."""

    def __init__(self, db_path: str) -> None:
        # Local import avoids a module cycle with the H1 db package.
        from omniagentos.db.store import SqliteStore

        self._store = SqliteStore(db_path)

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

    # --- agent registry ---

    @_serialized
    def register_agent(self, a: Agent) -> None:
        """UNIQUE(name) upsert: insert or update an agent."""
        now = utc_now_iso()
        existing = self._connection.execute(
            "SELECT id FROM agents WHERE name = ?", (a.name,)
        ).fetchone()
        expertise_json = _json(list(a.expertise))
        if existing is not None:
            agent_id = str(existing["id"])
            a.id = agent_id
            self._store._write(
                "UPDATE agents SET lineage = ?, model = ?, expertise_json = ?, "
                "trust_level = ?, status = ?, updated_at = ? WHERE name = ?",
                (
                    a.lineage,
                    a.model,
                    expertise_json,
                    a.trust_level,
                    str(a.status),
                    now,
                    a.name,
                ),
            )
            a.updated_at = now
            return
        a.updated_at = now
        self._store._write(
            "INSERT INTO agents "
            "(id, name, lineage, model, expertise_json, trust_level, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.id,
                a.name,
                a.lineage,
                a.model,
                expertise_json,
                a.trust_level,
                str(a.status),
                a.created_at,
                a.updated_at,
            ),
        )

    @_serialized
    def list_agents(self) -> list[dict[str, Any]]:
        rows = _rows(
            self._connection.execute(
                "SELECT * FROM agents ORDER BY created_at ASC, id ASC"
            ).fetchall()
        )
        return [_agent_dict(row) for row in rows]

    @_serialized
    def set_agent_status(self, agent_id: str, status: str) -> None:
        self._store._write(
            "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now_iso(), agent_id),
        )

    @_serialized
    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        )
        return None if row is None else _agent_dict(row)

    # --- self-assignable board ---

    @_serialized
    def create_board_task(self, t: BoardTask, *, actor: str = "system") -> None:
        """Insert a board card, INCLUDING its project scope (M1).

        ``t.project_id`` is written at creation instead of being left for a
        later PATCH: the window between "card exists" and "card is in a
        project" is a window in which the card is invisible to every
        project-scoped read (`GET /api/board?project_id=`, per-project budget
        rollups) and unbindable by a project-scoped grant. ``None`` persists as
        NULL — unscoped, the historical behaviour for callers that name no
        project, and never a guessed default.

        An unknown project id cannot persist as a dangling scope: migration
        087's foreign key is live here (``SqliteStore`` opens every connection
        with ``PRAGMA foreign_keys=ON``). The HTTP route resolves the project
        first so the caller sees a 404 instead of that integrity error.

        Team Work OS (migration 123): the team fields ride the same INSERT, and
        the card's first ``task_events`` row (``create``) is written in the SAME
        transaction — a card whose audit trail starts at its second mutation
        would silently lose its own origin. A ``ref`` already used by another
        card raises ``ValueError('ref_conflict')`` instead of a bare
        ``IntegrityError`` naming an index the caller has never heard of.
        """
        ref = _normalized_ref(t.ref)
        # The card as it WILL exist. Passed as both sides of the validator so a
        # create is held to exactly the rules a patch is: a hierarchy the update
        # path refuses must not be reachable by creating the card that way
        # instead, and "done" must be earned on the create path too (a brand-new
        # card has no evidence, so an owned, acceptance-bearing card cannot be
        # born done — it is created open and moved once the work exists).
        proposed: dict[str, Any] = {
            "status": str(t.status),
            "parent_task_id": t.parent_task_id,
            "owner_employee_id": t.owner_employee_id,
            "acceptance_criteria": t.acceptance_criteria,
            "blocked_reason": t.blocked_reason,
            "ref": ref,
        }

        def body(connection: sqlite3.Connection) -> None:
            _validate_team_rules(connection, t.id, proposed, proposed)
            connection.execute(
                "INSERT INTO board_tasks "
                "(id, title, description, required_expertise_json, discipline, priority, "
                "status, claimed_by, claim_version, result_ref, created_at, updated_at, "
                "project_id, parent_task_id, goal_id, owner_employee_id, ref, size, "
                "acceptance_criteria, blocked_reason, verified_at, verified_by, due_date, "
                "source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.id,
                    t.title,
                    t.description,
                    _json(list(t.required_expertise)),
                    t.discipline,
                    t.priority,
                    str(t.status),
                    t.claimed_by,
                    t.claim_version,
                    t.result_ref,
                    t.created_at,
                    t.updated_at,
                    _normalized_project_id(t.project_id),
                    t.parent_task_id,
                    t.goal_id,
                    t.owner_employee_id,
                    ref,
                    t.size,
                    t.acceptance_criteria,
                    t.blocked_reason,
                    # NEVER the model's values. Verification is earned through
                    # TeamStore.verify_task, which is the only writer of these
                    # two columns — a create that could carry them would be the
                    # PATCH bypass this table already refuses, spelled as an
                    # INSERT (and would arrive pre-verified with no 'verify'
                    # event, so the audit trail would not even show it).
                    None,
                    None,
                    t.due_date,
                    t.source,
                ),
            )
            append_task_event(
                connection,
                task_id=t.id,
                actor=actor,
                event="create",
                to_status=str(t.status),
                note=t.title[:200],
            )

        try:
            self._store._execute_write_txn(body, op="collab.create_board_task")
        except sqlite3.IntegrityError as exc:
            if "idx_board_tasks_ref" in str(exc):
                raise ValueError("ref_conflict") from exc
            raise

    @_serialized
    def get_board_task(self, task_id: str) -> dict[str, Any] | None:
        # ``company_slug`` rides the single-card read too: the read contract
        # (tests/intake/test_board_read_contract) holds a single card to be a
        # SUPERSET of its list element, and the list projects the field.
        row = _row(
            self._connection.execute(
                "SELECT *, (SELECT oc.slug FROM company_goals cg JOIN org_companies oc "
                "ON oc.id = cg.org_company_id WHERE cg.id = board_tasks.goal_id) "
                "AS company_slug FROM board_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        )
        return None if row is None else _task_dict(row)

    @_serialized
    def list_board_tasks(
        self,
        status: str | None = None,
        expertise: list[str] | None = None,
        archived: int | None = 0,
        limit: int | None = None,
        statuses: list[str] | None = None,
        updated_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """List board tasks, optionally filtering by status and archived state.

        Rows come back through the LIST projection (see ``_BOARD_LIST_COLUMNS``):
        every column except ``swarm_json``, whose two consumed fields arrive
        flattened as ``swarm_phase`` and ``swarm_integration``. Callers that need
        the full envelope must fetch the card with ``get_board_task``.

        Args:
            status: optional status filter (e.g., 'open', 'claimed', 'done')
            expertise: optional expertise filter for self-assignment matching
            archived: 0 (default) = exclude archived, 1 = only archived, None = all
            limit: optional server-side ``LIMIT``. ``None`` (default) returns
                every matching row, i.e. the historical behaviour. NOTE it caps
                the rows SQL returns, and ``expertise`` filters what is left in
                Python — so ``limit`` combined with ``expertise`` can return
                fewer than ``limit`` claimable cards.
            statuses: optional MULTI-status filter (``status IN (…)``). Composes
                with ``status`` as an AND, so passing both narrows to the
                intersection; an EMPTY list matches nothing (an explicit "no
                statuses selected" is not silently widened to "all cards").
            updated_after: optional ``updated_at >= ?`` bound, an ISO-8601 UTC
                string compared lexicographically (the format this schema
                stores). Deliberately INCLUSIVE: a delta poll that re-sends the
                boundary card is idempotent, while an exclusive bound would drop
                a second card written in the same second as the cursor.
        """
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        conditions: list[str] = []
        parameters: list[Any] = []
        if archived == 0:
            # Default: exclude archived (archived_at IS NULL)
            conditions.append("archived_at IS NULL")
        elif archived == 1:
            # Only archived (archived_at IS NOT NULL)
            conditions.append("archived_at IS NOT NULL")
        # None or any other value: no archive predicate — return all.
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        if statuses is not None:
            if not statuses:
                return []
            conditions.append(f"status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(str(item) for item in statuses)
        if updated_after is not None:
            conditions.append("updated_at >= ?")
            parameters.append(str(updated_after))
        if limit is not None:
            parameters.append(int(limit))
        rows = _rows(
            self._connection.execute(
                _board_list_sql(conditions, limit), tuple(parameters)
            ).fetchall()
        )
        tasks = [_task_dict(row) for row in rows]
        if expertise is not None:
            tasks = [
                task
                for task in tasks
                if can_claim(list(task.get("required_expertise") or []), expertise)
            ]
        return tasks

    @_serialized
    def board_change_stamp(self, archived: int = 0) -> tuple[int, str]:
        """(row count, newest ``updated_at``) for the cards a list read covers.

        The cheap half of the board ETag: two aggregates over ``board_tasks``
        instead of the whole reconciled feed. It sees every board WRITE, and
        deliberately nothing else — a linked run changing state does not touch
        ``board_tasks`` until the reconciler writes the new column, so callers
        must mix in their own liveness signal before serving a 304.
        """
        predicate = ""
        if archived == 0:
            predicate = " WHERE archived_at IS NULL"
        elif archived == 1:
            predicate = " WHERE archived_at IS NOT NULL"
        row = self._connection.execute(
            f"SELECT COUNT(*) AS count, MAX(updated_at) AS newest FROM board_tasks{predicate}"
        ).fetchone()
        if row is None:
            return 0, ""
        return int(row["count"] or 0), str(row["newest"] or "")

    @_serialized
    def open_tasks_for(self, agent_expertise: list[str]) -> list[dict[str, Any]]:
        """Return ONLY OPEN tasks that the agent may claim (can_claim filter).

        Projected like :meth:`list_board_tasks`. Deliberately takes no ``limit``:
        the claim filter runs in Python after the fetch, so a server-side LIMIT
        would silently hide claimable work from an agent rather than page it.
        """
        rows = _rows(
            self._connection.execute(
                _board_list_sql(["status = ?", "archived_at IS NULL"], None),
                (BoardTaskStatus.OPEN.value,),
            ).fetchall()
        )
        tasks = [_task_dict(row) for row in rows]
        return [
            task
            for task in tasks
            if can_claim(list(task.get("required_expertise") or []), agent_expertise)
        ]

    @_serialized
    def board_task_project_map(self, task_ids: Iterable[str]) -> dict[str, str]:
        """Resolve board_task_id → project_id through the run chain.

        ``board_tasks`` carries no project column; a card's project lives one
        hop away: ``board_tasks.run_id`` → ``runs.task_id`` →
        ``tasks.project_id``. Cards with no linked run, or whose chain ends in
        an unscoped task (``project_id IS NULL``), are absent from the map.
        """
        ids = [str(task_id) for task_id in task_ids]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self._connection.execute(
            "SELECT bt.id AS board_task_id, t.project_id AS project_id "
            "FROM board_tasks bt "
            "JOIN runs r ON r.id = bt.run_id "
            "JOIN tasks t ON t.id = r.task_id "
            f"WHERE bt.id IN ({placeholders}) AND t.project_id IS NOT NULL",
            ids,
        ).fetchall()
        return {str(row["board_task_id"]): str(row["project_id"]) for row in rows}

    @_serialized
    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        expect_version: int,
        *,
        actor: str = "system",
        owner_employee_id: str | None = None,
    ) -> bool:
        """Compare-and-swap claim: only OPEN + matching claim_version wins.

        The winning claim appends its own ``task_events`` row inside the SAME
        transaction. Claiming is a status move like any other, and it was the
        one move that left no trace — a card's trail jumped from ``create`` to
        whatever the claimant did next, so "who picked this up, and when" was
        answerable only from ``claimed_by``, which the next release overwrites.
        ``actor`` is the human/agent identity to record (the Slack ``claim``
        verb passes the sender; the swarm leaves the default). When an
        employee claims a pool card, ``owner_employee_id`` transfers
        accountability in this SAME compare-and-swap update. Agent-only claims
        omit it and retain the existing owner unchanged.
        """
        now = utc_now_iso()
        next_version = expect_version + 1
        self._store._begin()
        try:
            assignments = [
                "status = ?",
                "claimed_by = ?",
                "claim_version = ?",
                "updated_at = ?",
            ]
            parameters: list[Any] = [
                BoardTaskStatus.CLAIMED.value,
                agent_id,
                next_version,
                now,
            ]
            if owner_employee_id is not None:
                assignments.append("owner_employee_id = ?")
                parameters.append(owner_employee_id)
            parameters.extend((task_id, BoardTaskStatus.OPEN.value, expect_version))
            ownership_guard = ""
            if owner_employee_id is not None:
                ownership_guard = (
                    " AND (owner_employee_id IS NULL OR owner_employee_id = ?) AND source <> ?"
                )
                parameters.extend((owner_employee_id, BASELINE_SOURCE))
            cursor = self._connection.execute(
                f"UPDATE board_tasks SET {', '.join(assignments)} "
                f"WHERE id = ? AND status = ? AND claim_version = ?{ownership_guard}",
                tuple(parameters),
            )
            won = cursor.rowcount > 0
            if won:
                append_task_event(
                    self._connection,
                    task_id=task_id,
                    actor=actor,
                    event="status_change",
                    from_status=BoardTaskStatus.OPEN.value,
                    to_status=BoardTaskStatus.CLAIMED.value,
                    note=agent_id,
                )
                if owner_employee_id is not None:
                    append_task_event(
                        self._connection,
                        task_id=task_id,
                        actor=actor,
                        event="assign",
                        note=f"owner:{owner_employee_id}",
                    )
            self._store._commit()
            return won
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def release_claim(
        self,
        task_id: str,
        expect_version: int | None = None,
        *,
        actor: str = "system",
        return_to_pool: bool = False,
    ) -> bool:
        """Compare-and-swap release for the re-enqueue path: claim → OPEN.

        Additive counterpart to :meth:`claim_task` (``update_board_task``
        refuses ``claimed_by`` writes by design — that refusal stands). Resets
        status to OPEN, clears ``claimed_by``, and BUMPS ``claim_version`` so
        any stale claimant's follow-up CAS loses. With the explicit
        ``return_to_pool`` opt-in, ownership is also cleared atomically; this
        is refused for immutable baseline cards. Only CLAIMED/IN_PROGRESS tasks
        release; pass ``expect_version`` to make the release itself CAS.

        Returns True if this call released the claim.
        """
        now = utc_now_iso()
        conditions = ["id = ?", "status IN (?, ?)"]
        parameters: list[Any] = [
            task_id,
            BoardTaskStatus.CLAIMED.value,
            BoardTaskStatus.IN_PROGRESS.value,
        ]
        if expect_version is not None:
            conditions.append("claim_version = ?")
            parameters.append(expect_version)
        if return_to_pool:
            conditions.append("source <> ?")
            parameters.append(BASELINE_SOURCE)
        self._store._begin()
        try:
            current = self._connection.execute(
                f"SELECT status, owner_employee_id FROM board_tasks "
                f"WHERE {' AND '.join(conditions)}",
                tuple(parameters),
            ).fetchone()
            if current is None:
                self._store._commit()
                return False
            owner_assignment = ", owner_employee_id = NULL" if return_to_pool else ""
            cursor = self._connection.execute(
                "UPDATE board_tasks SET status = ?, claimed_by = NULL, "
                f"claim_version = claim_version + 1, updated_at = ?{owner_assignment} "
                f"WHERE {' AND '.join(conditions)}",
                (BoardTaskStatus.OPEN.value, now, *parameters),
            )
            released = cursor.rowcount > 0
            if released:
                append_task_event(
                    self._connection,
                    task_id=task_id,
                    actor=actor,
                    event="status_change",
                    from_status=str(current["status"]),
                    to_status=BoardTaskStatus.OPEN.value,
                    note="claim released",
                )
                if return_to_pool and current["owner_employee_id"] is not None:
                    append_task_event(
                        self._connection,
                        task_id=task_id,
                        actor=actor,
                        event="assign",
                        note="owner:none",
                    )
            self._store._commit()
            return released
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def update_board_task(
        self,
        task_id: str,
        fields: dict[str, Any],
        *,
        expect_status: str | None = None,
        actor: str = "system",
    ) -> bool:
        """Update task fields (status, result_ref, archived_at, etc.), bump updated_at.

        Rejected fields (enforced through CAS-only paths):
        - claimed_by: can only be set by claim_task() CAS
        - verified_at/verified_by: can only be set by TeamStore.verify_task
        - status transition to CLAIMED: can only be set by claim_task() CAS

        When ``expect_status`` is provided the UPDATE is compare-and-set: the
        row is re-read INSIDE the write transaction and a status that no longer
        matches returns False, so a concurrent completion (or any other status
        move) is never overwritten. Returns True when a row was updated, False
        on CAS miss, a missing card, or empty fields.

        Team Work OS rules (migration 123), all evaluated on the row as it
        exists inside ``BEGIN IMMEDIATE`` — a read outside the transaction would
        let two writers each validate a stale snapshot and jointly commit a
        state neither proposed (the defect ``CompanyGoalsStore.update_goal``
        documents):

        * **Hierarchy is one level deep.** A card may not parent itself, may not
          be filed under a card that is itself a child, and may not become a
          child while it has children of its own. Depth ≤ 1 is what makes "are
          all my subtasks finished?" a single query rather than a walk, and it
          is why no cycle longer than a self-reference is expressible.
        * **A blocked card says what it is blocked on.** Enforced on the
          transition, and only for OWNED cards: the swarm scheduler blocks agent
          cards from its budget and retry paths with no human-readable reason
          (``SwarmScheduler._block_task``), and those cards never appear in a
          person's Blocked queue. An owned card with no reason is the failure
          this rule exists to prevent.
        * **Done is EARNED, not asserted.** A card that names an owner AND
          acceptance criteria may only reach ``done`` with at least one
          ``task_evidence`` row attached and every subtask terminal. Cards with
          no owner (every agent/swarm card) are unaffected — the gate binds the
          human contract, not the board.

        Every successful mutation appends a ``task_events`` row in the same
        transaction, so the audit trail can never disagree with the row.
        """
        unknown = fields.keys() - _BOARD_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")

        # Closed vocabularies, validated app-side (the columns are CHECK-free).
        # An unknown status used to write straight through — the card then
        # matched NO queue bucket and silently dropped out of every board view;
        # an unknown priority ranked LAST in the queues, demoting the card its
        # author thought they had escalated. Both are 400s now, not writes.
        if "status" in fields:
            status_value = str(fields["status"])
            legal_statuses = tuple(member.value for member in BoardTaskStatus)
            if status_value not in legal_statuses:
                raise ValueError(
                    f"status must be one of {sorted(legal_statuses)}; got {status_value!r}"
                )
        if "priority" in fields:
            priority_value = str(fields["priority"])
            if priority_value not in TASK_PRIORITIES:
                raise ValueError(
                    f"priority must be one of {sorted(TASK_PRIORITIES)}; got {priority_value!r}"
                )
        # Migration 132 leaves automation_maturity CHECK-free like the columns
        # above, so this is the only seam that refuses an unknown state. NULL
        # stays legal and means UNTRACKED (clearing the field is a valid edit).
        if fields.get("automation_maturity") is not None:
            maturity_value = str(fields["automation_maturity"])
            if maturity_value not in AUTOMATION_MATURITY:
                raise ValueError(
                    f"automation_maturity must be one of {sorted(AUTOMATION_MATURITY)}; "
                    f"got {maturity_value!r}"
                )

        # Reject status transitions to CLAIMED (must use claim_task CAS).
        if "status" in fields and fields["status"] == BoardTaskStatus.CLAIMED.value:
            raise ValueError(
                "status transition to CLAIMED is not allowed via PATCH; "
                "use POST /board/{id}/claim instead"
            )

        if not fields:
            return False
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "required_expertise":
                values["required_expertise_json"] = _json(list(value or []))
            elif key == "ref":
                values[key] = _normalized_ref(value)
            else:
                values[key] = value

        def body(connection: sqlite3.Connection) -> bool:
            current = _row(
                connection.execute("SELECT * FROM board_tasks WHERE id = ?", (task_id,)).fetchone()
            )
            if current is None:
                return False
            if expect_status is not None and str(current["status"]) != expect_status:
                return False

            _validate_actor_rules(task_id, current, values, actor)
            _validate_team_rules(connection, task_id, current, values)

            written = dict(values)
            # Leaving 'done' withdraws the verification IN THE SAME
            # TRANSACTION. A card that is reopened while still stamped
            # verified_at keeps scoring points for work that is, by the board's
            # own account, no longer finished — and nothing in the trail says
            # when that started being true.
            # Migration 132 extends the same reasoning to the FAILURE stamps: a
            # reopened card returns to clean unverified. A card sitting in
            # 'failed_verification' while it is back in progress describes a
            # verdict on work that no longer exists in that form, and the
            # refusal itself survives in the append-only 'verify_failed' event.
            reopening = (
                "status" in values
                and str(current["status"]) == BoardTaskStatus.DONE.value
                and str(values["status"]) != BoardTaskStatus.DONE.value
            )
            leaving_done = reopening and current["verified_at"] is not None
            if leaving_done:
                written["verified_at"] = None
                written["verified_by"] = None
            if reopening and current["verification_failed_at"] is not None:
                written["verification_failed_at"] = None
                written["verification_failed_by"] = None
                written["verification_failed_reason"] = None
            written["updated_at"] = utc_now_iso()
            assignments = ", ".join(f"{column} = ?" for column in written)
            cursor = connection.execute(
                f"UPDATE board_tasks SET {assignments} WHERE id = ?",
                (*written.values(), task_id),
            )
            if cursor.rowcount <= 0:  # pragma: no cover -- the row was read above
                return False
            _append_update_event(
                connection, task_id=task_id, actor=actor, current=current, written=values
            )
            if leaving_done:
                append_task_event(
                    connection,
                    task_id=task_id,
                    actor=actor,
                    event="unverify",
                    note=str(current["verified_by"] or ""),
                )
            return True

        try:
            return self._store._execute_write_txn(body, op="collab.update_board_task")
        except sqlite3.IntegrityError as exc:
            if "idx_board_tasks_ref" in str(exc):
                raise ValueError("ref_conflict") from exc
            raise

    @_serialized
    def restore_archived_board_task(self, task_id: str, *, actor: str = "system") -> bool:
        """Idempotent restore of an archived board task.

        Returns True if the task was restored (was previously archived),
        False if it was already unarchived or doesn't exist.
        """
        self._store._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE board_tasks SET archived_at = NULL, updated_at = ? "
                "WHERE id = ? AND archived_at IS NOT NULL",
                (utc_now_iso(), task_id),
            )
            restored = cursor.rowcount > 0
            if restored:
                append_task_event(
                    self._connection,
                    task_id=task_id,
                    actor=actor,
                    event="comment",
                    note="archived_at",
                )
            self._store._commit()
            return restored
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def purge_archived_board_tasks(self, older_than_days: int = 7, now: str | None = None) -> int:
        """Delete board cards archived more than N days ago.

        Args:
            older_than_days: retention window in days (default 7)
            now: optional ISO timestamp for deterministic testing (current UTC if None)

        Returns:
            count of rows deleted
        """
        if now is None:
            now = utc_now_iso()

        # Calculate cutoff: now - N days
        from datetime import datetime, timedelta

        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        cutoff_dt = now_dt - timedelta(days=older_than_days)
        cutoff = cutoff_dt.isoformat().replace("+00:00", "Z")

        # A live child may still point at an archived parent. Detach every such
        # child before deleting the purge set, in the SAME transaction, so the
        # parent foreign key cannot wedge retention and the surviving child
        # becomes an ordinary top-level card.
        self._store._begin()
        try:
            self._connection.execute(
                "UPDATE board_tasks SET parent_task_id = NULL "
                "WHERE parent_task_id IN ("
                "SELECT id FROM board_tasks "
                "WHERE archived_at IS NOT NULL AND archived_at < ?"
                ")",
                (cutoff,),
            )
            cursor = self._connection.execute(
                "DELETE FROM board_tasks WHERE archived_at IS NOT NULL AND archived_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            self._store._commit()
            return deleted
        except BaseException:
            self._store._rollback()
            raise

    # --- messaging + conversation log ---

    def _channel_members(self, channel_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT agent_id FROM channel_members WHERE channel_id = ? ORDER BY agent_id ASC",
            (channel_id,),
        ).fetchall()
        return [str(row["agent_id"]) for row in rows]

    def _channel_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["members"] = self._channel_members(str(out["id"]))
        return out

    def _find_direct_channel(self, member_a: str, member_b: str) -> str | None:
        pair = sorted([member_a, member_b])
        row = self._connection.execute(
            "SELECT c.id AS id FROM channels c "
            "JOIN channel_members m1 ON m1.channel_id = c.id AND m1.agent_id = ? "
            "JOIN channel_members m2 ON m2.channel_id = c.id AND m2.agent_id = ? "
            "WHERE c.kind = ? "
            "AND (SELECT COUNT(*) FROM channel_members cm WHERE cm.channel_id = c.id) = 2 "
            "LIMIT 1",
            (pair[0], pair[1], ChannelKind.DIRECT.value),
        ).fetchone()
        return None if row is None else str(row["id"])

    @_serialized
    def create_channel(self, c: Channel) -> None:
        """Create a channel. DIRECT channels dedupe by canonical member pair."""
        members = list(c.members)
        if str(c.kind) == ChannelKind.DIRECT.value and len(members) == 2:
            existing_id = self._find_direct_channel(members[0], members[1])
            if existing_id is not None:
                c.id = existing_id
                # Ensure both members are present (idempotent).
                for agent_id in members:
                    self._connection.execute(
                        "INSERT OR IGNORE INTO channel_members (channel_id, agent_id) "
                        "VALUES (?, ?)",
                        (existing_id, agent_id),
                    )
                return

        self._store._begin()
        try:
            self._connection.execute(
                "INSERT INTO channels (id, name, kind, topic, created_at) VALUES (?, ?, ?, ?, ?)",
                (c.id, c.name, str(c.kind), c.topic, c.created_at),
            )
            for agent_id in members:
                self._connection.execute(
                    "INSERT OR IGNORE INTO channel_members (channel_id, agent_id) VALUES (?, ?)",
                    (c.id, agent_id),
                )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def add_member(self, channel_id: str, agent_id: str) -> None:
        """Add an agent to a channel (no-op if already a member)."""
        self._store._write(
            "INSERT OR IGNORE INTO channel_members (channel_id, agent_id) VALUES (?, ?)",
            (channel_id, agent_id),
        )

    @_serialized
    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        row = _row(
            self._connection.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        )
        return None if row is None else self._channel_dict(row)

    @_serialized
    def list_channels(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        if agent_id is None:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM channels ORDER BY created_at ASC, id ASC"
                ).fetchall()
            )
        else:
            rows = _rows(
                self._connection.execute(
                    "SELECT c.* FROM channels c "
                    "JOIN channel_members m ON m.channel_id = c.id "
                    "WHERE m.agent_id = ? "
                    "ORDER BY c.created_at ASC, c.id ASC",
                    (agent_id,),
                ).fetchall()
            )
        return [self._channel_dict(row) for row in rows]

    @_serialized
    def post_message(self, m: Message) -> None:
        self._store._write(
            "INSERT INTO messages "
            "(id, channel_id, from_agent, to_agent, kind, body, ref, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.id,
                m.channel_id,
                m.from_agent,
                m.to_agent,
                str(m.kind),
                m.body,
                m.ref,
                m.ts,
            ),
        )

    @_serialized
    def list_messages(self, channel_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM messages WHERE channel_id = ? ORDER BY ts ASC, id ASC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        )

    @_serialized
    def search_messages(self, q: str, limit: int = 100) -> list[dict[str, Any]]:
        """Full-text substring match on body.

        Escapes LIKE wildcards (%, _) and the escape character in the query
        so they are matched literally, not as SQL LIKE metacharacters.
        """
        # Escape LIKE metacharacters: backslash, percent, underscore.
        escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_q}%"
        return _rows(
            self._connection.execute(
                "SELECT * FROM messages WHERE body LIKE ? ESCAPE '\\' "
                "ORDER BY ts ASC, id ASC LIMIT ?",
                (pattern, limit),
            ).fetchall()
        )
