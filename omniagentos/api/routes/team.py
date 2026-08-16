"""Team Work OS HTTP surface: queues, work hierarchy, evidence review, scoring."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.collab import CollabStoreDep, _emit, _resolve_project_id
from omniagentos.api.routes.control import fail
from omniagentos.api.routes.sessions import _authorized
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus, CollabEvents
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team import commitments, learning
from omniagentos.team import report as team_report
from omniagentos.team.contracts import (
    COMMITMENT_KINDS,
    OPERATOR_EMPLOYEE_ID,
    SLOTTED_COMMITMENT_KINDS,
)
from omniagentos.team.diagnostics import compute_diagnostics
from omniagentos.team.points import pace_statuses
from omniagentos.team.report import fleet_ledger_path
from omniagentos.team.scoring import (
    BASELINE_WEEK_END,
    BASELINE_WEEK_START,
    SCORE_VERSION,
    baseline_points,
    compute_scores,
    fleet_production,
    overall_production_x,
    pct_to_target,
    production_x,
)
from omniagentos.team.store import TeamStore, completion_state
from omniagentos.team.tasks import (
    assign_adhoc_task,
    match_automation_category,
    parse_deadline,
    propose_automation,
    resolve_company_goal,
    split_deadline,
)

# Evidence and verification history are internal work records.  Keep the
# complete namespace behind the session-token boundary; auth-posture tests
# intentionally reject any new GET that is neither explicitly public nor gated.
router = APIRouter(prefix="/api/team", tags=["team"], dependencies=[Depends(_authorized)])


class VerifyTaskBody(BaseModel):
    """A verification VERDICT: ``pass`` (default, unchanged) or ``fail``.

    ``outcome`` defaults to the historical behaviour, so every existing caller
    keeps working byte-for-byte. ``reason`` is required for a fail and ignored
    for a pass — a refusal nobody explained is a refusal the owner cannot act on.
    """

    verifier: str = Field(min_length=1)
    outcome: str = "pass"
    reason: str = ""


class UnverifyTaskBody(BaseModel):
    actor: str = Field(min_length=1)


class CreateEvidenceBody(BaseModel):
    kind: str
    ref: str
    actor: str = Field(min_length=1)
    repo: str = ""
    title: str = ""
    quality_gate: str = "pass"
    meta: dict[str, Any] = Field(default_factory=dict)


class ReattributeEvidenceBody(BaseModel):
    task_id: str | None
    actor: str = Field(min_length=1)


class CreatePoolTaskBody(BaseModel):
    """Explicit queue-ready intake; legacy agent-board creation stays permissive."""

    title: str = Field(max_length=1024)
    goal_id: str = ""
    acceptance_criteria: str = ""
    description: str = ""
    required_expertise: list[str] = Field(default_factory=list)
    discipline: str | None = None
    priority: str = "normal"
    project_id: str | None = None
    ref: str | None = None
    size: str = "M"
    due_date: str | None = None


_WINDOW_RE = re.compile(r"^(\d+)([dh])$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_WINDOW_DAYS = 3650


def _validated_day(value: str | None) -> date:
    raw = utc_now_iso()[:10] if value is None else str(value).strip()
    if _DAY_RE.fullmatch(raw) is None:
        fail(400, "validation", "day must be a valid YYYY-MM-DD date", {"day": value})
    try:
        return date.fromisoformat(raw)
    except ValueError:
        fail(400, "validation", "day must be a valid YYYY-MM-DD date", {"day": value})


def _window_bounds(window: str, *, today: str | None = None) -> tuple[str, str]:
    """``7d``/``48h`` -> inclusive ``(start_day, end_day)``.

    Whole-day resolution on purpose: the scoreboard answers the same question
    the 07:00 report does, and two windows that disagree by a few hours would
    make the API and the report contradict each other on the same morning.
    """
    match = _WINDOW_RE.match(str(window).strip().lower())
    if match is None:
        fail(400, "validation", "window must look like 7d or 48h", {"window": window})
    amount = int(match.group(1))
    if amount < 1:
        fail(400, "validation", "window must be at least 1", {"window": window})
    unit = match.group(2)
    if (unit == "d" and amount > _MAX_WINDOW_DAYS) or (
        unit == "h" and amount > _MAX_WINDOW_DAYS * 24
    ):
        fail(
            400,
            "validation",
            f"window must not exceed {_MAX_WINDOW_DAYS} days",
            {"window": window},
        )
    days = amount if unit == "d" else max(1, (amount + 23) // 24)
    end = _validated_day(today)
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def _sqlite_store(store: StoreDep) -> SqliteStore:
    """The API's live SQLite store, shared with the TeamStore transaction lock."""
    return cast(SqliteStore, store)


def _team_store(store: StoreDep) -> TeamStore:
    return TeamStore(_sqlite_store(store))


def _task_exists(store: StoreDep, task_id: str) -> bool:
    row = (
        _sqlite_store(store)
        ._connection.execute("SELECT 1 FROM board_tasks WHERE id = ?", (task_id,))
        .fetchone()
    )
    return row is not None


def _board_row(store: StoreDep, task_id: str) -> dict[str, Any] | None:
    row = (
        _sqlite_store(store)
        ._connection.execute("SELECT * FROM board_tasks WHERE id = ?", (task_id,))
        .fetchone()
    )
    return None if row is None else dict(row)


@router.get("/board")
def get_team_board(store: StoreDep, owner: str | None = None) -> dict[str, Any]:
    """Queue buckets for the whole roster, or for one named employee."""
    if owner is not None and CompanyGoalsStore(_sqlite_store(store)).get_employee(owner) is None:
        fail(404, "not_found", "employee not found", {"id": owner})
    team_store = _team_store(store)
    queues = team_store.team_queues(employee_ids=[owner] if owner is not None else None)
    payload = {
        employee_id: bucket.model_dump_with_counts() for employee_id, bucket in queues.items()
    }
    payload["pool"] = team_store.pool_payload()
    return payload


@router.post("/tasks", status_code=201)
def create_pool_task(body: CreatePoolTaskBody, collab: CollabStoreDep) -> Any:
    """Create one ownerless, top-level card that satisfies the pool contract."""
    title = body.title.strip()
    goal_id = body.goal_id.strip()
    acceptance_criteria = body.acceptance_criteria.strip()
    if not title:
        fail(400, "validation", "title is required")
    if not goal_id:
        fail(400, "validation", "goal_id is required")
    if not acceptance_criteria:
        fail(400, "validation", "acceptance_criteria is required")
    if CompanyGoalsStore(collab._store).get_goal(goal_id) is None:
        fail(400, "validation", "unknown goal", {"goal_id": goal_id})

    task = BoardTask(
        title=title,
        description=body.description,
        required_expertise=list(body.required_expertise),
        discipline=body.discipline,
        priority=body.priority,
        project_id=_resolve_project_id(collab, body.project_id),
        goal_id=goal_id,
        acceptance_criteria=acceptance_criteria,
        ref=body.ref,
        size=body.size,
        due_date=body.due_date,
    )
    try:
        collab.create_board_task(task)
    except ValueError as exc:
        if str(exc) == "ref_conflict":
            return JSONResponse(status_code=409, content={"detail": "ref_conflict"})
        fail(400, "validation", str(exc))
    try:
        from omniagentos.orgdims.service import OrgDimsService

        OrgDimsService().classify_board_task(
            task_id=task.id,
            title=task.title,
            description=task.description,
            discipline=task.discipline,
            priority=task.priority,
            apply=True,
        )
    except Exception:  # noqa: BLE001 — classification must never block create
        pass
    created = collab.get_board_task(task.id)
    if created is None:  # pragma: no cover - persistence postcondition
        fail(500, "internal", "board task not persisted")
    _emit(
        collab,
        CollabEvents.BOARD_UPDATED,
        "board.created",
        target_type="board_task",
        target_id=task.id,
        payload={"task_id": task.id, "status": str(task.status)},
    )
    return created


@router.get("/tree")
def get_team_tree(store: StoreDep) -> dict[str, list[dict[str, Any]]]:
    """The company -> goal -> task -> subtask tree, assembled from three bulk reads."""
    sqlite_store = _sqlite_store(store)

    # Exactly three bounded bulk reads: companies, goals, then all cards.  The
    # nested response is assembled below, never by recursively querying a row.
    companies = [
        dict(row)
        for row in sqlite_store._connection.execute(
            "SELECT id, slug, name, status, created_at FROM org_companies ORDER BY name, id"
        ).fetchall()
    ]
    goals = CompanyGoalsStore(sqlite_store).list_goals()
    tasks = [
        dict(row)
        for row in sqlite_store._connection.execute(
            "SELECT id, ref, title, status, owner_employee_id, size, verified_at, goal_id, "
            "parent_task_id FROM board_tasks ORDER BY created_at ASC, id ASC"
        ).fetchall()
    ]

    company_by_id: dict[str, dict[str, Any]] = {}
    for company in companies:
        company["goals"] = []
        company_by_id[str(company["id"])] = company

    goal_by_id: dict[str, dict[str, Any]] = {}
    for goal in goals:
        node = dict(goal)
        node["tasks"] = []
        goal_by_id[str(node["id"])] = node
        company_node: dict[str, Any] | None = company_by_id.get(str(node["org_company_id"]))
        if company_node is not None:
            company_node["goals"].append(node)

    task_by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_by_id[str(task["id"])] = {
            key: task[key]
            for key in ("id", "ref", "title", "status", "owner_employee_id", "size", "verified_at")
        }
        task_by_id[str(task["id"])]["subtasks"] = []

    for task in tasks:
        node = task_by_id[str(task["id"])]
        parent_id = task["parent_task_id"]
        if parent_id is not None and str(parent_id) in task_by_id:
            task_by_id[str(parent_id)]["subtasks"].append(node)
            continue
        goal_node: dict[str, Any] | None = goal_by_id.get(str(task["goal_id"] or ""))
        if goal_node is not None:
            goal_node["tasks"].append(node)

    return {"companies": companies}


@router.post("/tasks/{task_id}/verify")
def verify_task(task_id: str, body: VerifyTaskBody, store: StoreDep) -> dict[str, Any]:
    """Record a verification verdict, then feed the learning ladder.

    The learning hook runs HERE, at the route layer, strictly AFTER the store
    transaction has committed — never inside it. A metacog failure must not be
    able to roll back a verification (see
    :mod:`omniagentos.team.learning`'s docstring for the accepted residual).
    """
    outcome = str(body.outcome or "pass").strip().lower()
    if outcome not in ("pass", "fail"):
        fail(400, "validation", "outcome must be 'pass' or 'fail'", {"outcome": body.outcome})
    team_store = _team_store(store)
    try:
        if outcome == "fail":
            result = team_store.record_verification_failure(task_id, body.verifier, body.reason)
        else:
            result = team_store.record_verification(task_id, body.verifier)
    except ValueError as exc:
        fail(400, "validation", str(exc))
    if result is None:
        fail(404, "not_found", "board task not found", {"id": task_id})

    # Both facts the hook needs come from the store's own transaction: the
    # event it minted, and whether this was the first success. The route
    # derives NEITHER — a first-success guess from ``verified_at`` is wrong
    # after any fail/unverify (both clear the stamp), and an event guessed by
    # re-reading the trail can be somebody else's (Sol review, item 3).
    evidence = team_store.list_evidence(task_id)
    if outcome == "fail":
        learning.on_verification_failed(
            team_store,
            result.card,
            evidence,
            event_id=result.event_id,
            reason=body.reason,
        )
    elif result.first_success:
        learning.on_task_verified(team_store, result.card, evidence, event_id=result.event_id)
    return result.card


@router.post("/tasks/{task_id}/unverify")
def unverify_task(task_id: str, body: UnverifyTaskBody, store: StoreDep) -> dict[str, Any]:
    try:
        task = _team_store(store).unverify_task(task_id, body.actor)
    except ValueError as exc:
        fail(400, "validation", str(exc))
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    return task


@router.post("/tasks/{task_id}/evidence", status_code=201)
def create_evidence(
    task_id: str, body: CreateEvidenceBody, response: Response, store: StoreDep
) -> dict[str, Any]:
    if not _task_exists(store, task_id):
        fail(404, "not_found", "board task not found", {"id": task_id})
    try:
        evidence, outcome = _team_store(store).record_evidence(
            kind=body.kind,
            ref=body.ref,
            task_id=task_id,
            repo=body.repo,
            actor=body.actor,
            title=body.title,
            attribution="manual",
            quality_gate=body.quality_gate,
            meta=body.meta,
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    if outcome == "conflict":
        response.status_code = 409
        return {
            "detail": "evidence_exists",
            "evidence_id": evidence["id"],
            "task_id": evidence["task_id"],
        }
    response.status_code = 201 if outcome == "created" else 200
    return evidence


@router.get("/tasks/{task_id}/evidence")
def list_task_evidence(task_id: str, store: StoreDep) -> list[dict[str, Any]]:
    return _team_store(store).list_evidence(task_id)


@router.get("/tasks/{task_id}/events")
def list_task_events(task_id: str, store: StoreDep) -> list[dict[str, Any]]:
    return _team_store(store).list_events(task_id)


@router.patch("/evidence/{evidence_id}")
def reattribute_evidence(
    evidence_id: str, body: ReattributeEvidenceBody, store: StoreDep
) -> dict[str, Any]:
    try:
        evidence = _team_store(store).reattribute_evidence(evidence_id, body.task_id, body.actor)
    except ValueError as exc:
        fail(400, "validation", str(exc))
    if evidence is None:
        fail(404, "not_found", "evidence not found", {"id": evidence_id})
    return evidence


@router.get("/evidence/unattributed")
def list_unattributed_evidence(
    store: StoreDep, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict[str, Any]]:
    return _team_store(store).list_unattributed(limit=limit)


@router.get("/scoreboard")
def get_scoreboard(
    store: StoreDep,
    window: str = Query(default="7d"),
    day: str | None = Query(default=None),
    detail: bool = Query(default=False),
) -> dict[str, Any]:
    """Verified-output points and production_x per person for a rolling window.

    The SAME functions the 07:00 report uses — the dashboard and the report can
    never disagree, because there is only one implementation of the score.
    """
    start, end = _window_bounds(window, today=day)
    team_store = _team_store(store)
    scores = compute_scores(team_store, period_start=start, period_end=end)
    people: list[dict[str, Any]] = []
    total_points = 0
    total_baseline = 0
    for employee_id, breakdown in scores.items():
        baseline = baseline_points(team_store, employee_id)
        total_points += breakdown.score
        total_baseline += baseline
        ratio = production_x(breakdown.score, baseline)
        person = {
            "employee_id": employee_id,
            "score": breakdown.score,
            "baseline_points": baseline,
            "production_x": ratio,
            "pct_to_10x": pct_to_target(ratio),
        }
        if detail:
            person.update(
                {
                    "counted": breakdown.counted,
                    "excluded": breakdown.excluded,
                }
            )
        people.append(person)
    people.sort(key=lambda entry: (-int(entry["score"]), str(entry["employee_id"])))
    team_ratio = production_x(total_points, total_baseline)
    ledger = fleet_ledger_path()
    fleet_merged = fleet_production(ledger, start, end)
    fleet_baseline = fleet_production(ledger, BASELINE_WEEK_START, BASELINE_WEEK_END)
    fleet_ratio = production_x(fleet_merged, fleet_baseline) if fleet_merged is not None else None
    overall_ratio = overall_production_x(team_ratio, fleet_ratio)
    return {
        "period": {"start": start, "end": end},
        "score_version": SCORE_VERSION,
        "people": people,
        "team": {
            "score": total_points,
            "baseline_points": total_baseline,
            "production_x": team_ratio,
            "pct_to_10x": pct_to_target(team_ratio),
        },
        "overall": {
            "humans_x": team_ratio,
            "fleet_merged": fleet_merged,
            "fleet_baseline_merged": fleet_baseline,
            "fleet_x": fleet_ratio,
            "production_x": overall_ratio,
            "pct_to_10x": pct_to_target(overall_ratio),
        },
    }


@router.get("/diagnostics")
def get_diagnostics(
    store: StoreDep,
    owner: str | None = None,
    window: str = Query(default="7d"),
    day: str | None = Query(default=None),
) -> dict[str, Any]:
    """Work-shape numbers (sessions, concurrency, merged PRs, first-pass rate).

    Descriptive only. Nothing here is an input to the scoreboard above — see
    :mod:`omniagentos.team.diagnostics` for why that direction is one-way.
    """
    start, end = _window_bounds(window, today=day)
    team_store = _team_store(store)
    scores = compute_scores(team_store, period_start=start, period_end=end)
    measured = compute_diagnostics(
        team_store,
        period_start=start,
        period_end=end,
        verified_outcomes={
            employee_id: len(breakdown.counted) for employee_id, breakdown in scores.items()
        },
    )
    people = {
        employee_id: diagnostics.as_dict()
        for employee_id, diagnostics in sorted(measured.items())
        if owner is None or employee_id == owner
    }
    return {"window": window, "period": {"start": start, "end": end}, "people": people}


@router.get("/report/preview")
def get_report_preview(store: StoreDep, day: str | None = None) -> dict[str, Any]:
    """Today's report text, rendered live. READ-ONLY — no snapshot, no Slack post."""
    target_day = _validated_day(day).isoformat()
    gathered = team_report.gather(_team_store(store), target_day)
    return {"day": target_day, "text": team_report.render(gathered)}


# --------------------------------------------------------------------------
# daily commitments (migration 132)
# --------------------------------------------------------------------------


class CreateCommitmentBody(BaseModel):
    """An operator-added commitment. ``source`` is fixed at ``operator``."""

    employee_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=1024)
    day: str | None = None
    kind: str = "task"
    task_id: str | None = None
    expected_outcome: str = ""


class PatchCommitmentBody(BaseModel):
    """Either a resolution (``status`` + note) or a note APPEND (note alone)."""

    status: str | None = None
    resolution_note: str = ""
    actor: str = OPERATOR_EMPLOYEE_ID


@router.get("/commitments")
def list_commitments(
    store: StoreDep,
    day: str | None = Query(default=None),
    employee_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Commitments for a LOCAL day (default today), optionally one person's."""
    target = commitments.local_today() if day is None else _validated_day(day).isoformat()
    rows = _team_store(store).list_commitments(day=target, employee_id=employee_id)
    return {"day": target, "commitments": rows}


@router.post("/commitments", status_code=201)
def create_commitment(body: CreateCommitmentBody, store: StoreDep, response: Response) -> Any:
    """Add one commitment by hand. Idempotent on the same unique keys the
    generator uses — a duplicate returns 200 with the existing row, never a
    second commitment for the same person, day and card."""
    if body.kind not in COMMITMENT_KINDS:
        fail(
            400, "validation", f"kind must be one of {sorted(COMMITMENT_KINDS)}", {"id": body.kind}
        )
    if body.kind in SLOTTED_COMMITMENT_KINDS and body.task_id is not None:
        # A slotted commitment (the improvement slot, an automation slot) is a
        # standing expectation, not a promise about one card — and the store
        # indexes it by slot, so a card-bearing slotted row would be unfindable
        # by the key its own idempotency check reads. Refused here with the
        # reason rather than as a bare constraint error.
        fail(
            400,
            "validation",
            f"a {body.kind} commitment cannot name a task_id",
            {"kind": body.kind, "task_id": body.task_id},
        )
    day = commitments.local_today() if body.day is None else _validated_day(body.day).isoformat()
    if CompanyGoalsStore(_sqlite_store(store)).get_employee(body.employee_id) is None:
        fail(404, "not_found", "employee not found", {"id": body.employee_id})
    if body.task_id is not None and not _task_exists(store, body.task_id):
        fail(404, "not_found", "board task not found", {"id": body.task_id})
    try:
        row, outcome = _team_store(store).create_commitment(
            day=day,
            employee_id=body.employee_id,
            kind=body.kind,
            task_id=body.task_id,
            title=body.title,
            expected_outcome=body.expected_outcome,
            source="operator",
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    response.status_code = 201 if outcome == "created" else 200
    return row


@router.patch("/commitments/{commitment_id}")
def patch_commitment(
    commitment_id: str, body: PatchCommitmentBody, store: StoreDep
) -> dict[str, Any]:
    """Resolve a commitment by hand, or append a note to a resolved one.

    The immutability rules are the point of this endpoint (review S10 /
    round-3 §3), not an obstacle to it:

    * only ``committed -> delivered|missed`` is a legal transition. ``carried``
      is minted by ``resolve_day`` alone, and a MISS is history — rewriting one
      to ``delivered`` tomorrow would make the whole record unfalsifiable;
    * a resolution must carry a ``resolution_note``: an unexplained override is
      indistinguishable from a mistake;
    * marking a TASK commitment delivered requires the linked card to actually
      be ``done``. The board is the evidence; this endpoint is the ruling;
    * on an already-resolved row, the note APPENDS and nothing else changes.
    """
    team_store = _team_store(store)
    current = team_store.get_commitment(commitment_id)
    if current is None:
        fail(404, "not_found", "commitment not found", {"id": commitment_id})
    note = body.resolution_note.strip()

    if body.status is None:
        if not note:
            fail(400, "validation", "resolution_note is required")
        updated = team_store.append_resolution_note(commitment_id, note, actor=body.actor)
        return cast(dict[str, Any], updated)

    if str(current["status"]) != "committed":
        fail(
            400,
            "validation",
            "a resolved commitment is history; only a resolution_note may be appended",
            {"status": current["status"]},
        )
    if body.status not in ("delivered", "missed"):
        fail(
            400,
            "validation",
            "status must be 'delivered' or 'missed' ('carried' is minted by resolve_day)",
            {"status": body.status},
        )
    if not note:
        fail(400, "validation", "resolution_note is required when resolving a commitment")
    if body.status == "delivered" and str(current["kind"]) == "task":
        card = None if current["task_id"] is None else _board_row(store, str(current["task_id"]))
        if card is None or str(card.get("status")) != BoardTaskStatus.DONE.value:
            fail(
                400,
                "validation",
                "the linked card is not done; a task commitment cannot be delivered without it",
                {"task_id": current["task_id"]},
            )
    updated = team_store.resolve_commitment(
        commitment_id,
        status=body.status,
        resolution_note=note,
        resolved_by=body.actor,
    )
    return cast(dict[str, Any], updated)


# --------------------------------------------------------------------------
# the owner accountability view (spec §8)
# --------------------------------------------------------------------------


def _evidence_items(team_store: TeamStore, task_id: str) -> list[dict[str, Any]]:
    """Per-card evidence DETAIL, never a bare count (review S12)."""
    return [
        {
            "kind": row["kind"],
            "repo": row["repo"],
            "ref": row["ref"],
            "quality_gate": row["quality_gate"],
        }
        for row in team_store.list_evidence(task_id)
    ]


def _done_today(store: StoreDep, employee_id: str, day: str) -> list[dict[str, Any]]:
    """Cards this person finished during the LOCAL ``day``.

    Keyed on the ``status_change -> done`` EVENT inside the half-open local-day
    window, not on ``updated_at``: any later edit moves ``updated_at``, which
    would quietly migrate a card from the day it was finished to the day it was
    touched.
    """
    start, end = commitments.local_day_bounds(day)
    rows = (
        _sqlite_store(store)
        ._connection.execute(
            "SELECT b.* FROM board_tasks b WHERE b.owner_employee_id = ? AND b.status = ? "
            "AND b.archived_at IS NULL AND EXISTS ("
            "  SELECT 1 FROM task_events e WHERE e.task_id = b.id AND e.event = 'status_change' "
            "  AND e.to_status = ? AND e.created_at >= ? AND e.created_at < ?"
            ") ORDER BY b.updated_at ASC, b.id ASC",
            (employee_id, BoardTaskStatus.DONE.value, BoardTaskStatus.DONE.value, start, end),
        )
        .fetchall()
    )
    return [dict(row) for row in rows]


def _blocked_reasons(store: StoreDep, employee_id: str) -> dict[str, str]:
    """``task_id -> blocked_reason`` for this person's blocked cards.

    A blocked list with no reasons tells a reader that somebody is stuck and
    nothing about what would unstick them — and the reason is mandatory on the
    transition (migration 123's rule), so it is always there to show. Read as a
    MAP over the queue's own blocked bucket rather than as a second query for
    the list itself: the queue stays the single source of which cards are
    blocked, and this only adds the column it does not project.
    """
    return {
        str(row["id"]): str(row["blocked_reason"] or "")
        for row in _sqlite_store(store)._connection.execute(
            "SELECT id, blocked_reason FROM board_tasks WHERE owner_employee_id = ? "
            "AND status = ? AND archived_at IS NULL",
            (employee_id, BoardTaskStatus.BLOCKED.value),
        )
    }


def _evidence_today(store: StoreDep, employee_id: str, day: str) -> int:
    """Evidence rows filed against this person's cards during the LOCAL day.

    Activity, deliberately reported NEXT TO outcomes rather than instead of
    them: it is the honest answer to "was anything produced today?" on a day
    whose work is real but unfinished, and it is never a score — the scoreboard
    counts verified cards, and nothing here feeds it.
    """
    start, end = commitments.local_day_bounds(day)
    row = (
        _sqlite_store(store)
        ._connection.execute(
            "SELECT COUNT(*) AS filed FROM task_evidence e "
            "JOIN board_tasks b ON b.id = e.task_id "
            "WHERE b.owner_employee_id = ? AND e.created_at >= ? AND e.created_at < ?",
            (employee_id, start, end),
        )
        .fetchone()
    )
    return 0 if row is None else int(row["filed"])


def _learning_captures(store: StoreDep, employee_id: str, day: str) -> int:
    """Learning candidates captured from this person's cards during ``day``.

    Counted from the durable ``learning_capture`` markers the hook writes, not
    from metacog: the marker is what survives, and a count that disagreed with
    the audit trail would be the wrong number by definition.
    """
    start, end = commitments.local_day_bounds(day)
    row = (
        _sqlite_store(store)
        ._connection.execute(
            "SELECT COUNT(*) AS captures FROM task_events e "
            "JOIN board_tasks b ON b.id = e.task_id "
            "WHERE b.owner_employee_id = ? AND e.event = 'comment' "
            "AND e.note LIKE 'learning_capture: %' AND e.created_at >= ? AND e.created_at < ?",
            (employee_id, start, end),
        )
        .fetchone()
    )
    return 0 if row is None else int(row["captures"])


def _overdue_count(store: StoreDep, employee_id: str, day: str) -> int:
    """Owned, non-terminal cards whose deadline is already in the past."""
    row = (
        _sqlite_store(store)
        ._connection.execute(
            "SELECT COUNT(*) AS overdue FROM board_tasks WHERE owner_employee_id = ? "
            "AND archived_at IS NULL AND due_date IS NOT NULL AND substr(due_date, 1, 10) < ? "
            "AND status NOT IN (?, ?)",
            (
                employee_id,
                day,
                BoardTaskStatus.DONE.value,
                BoardTaskStatus.CANCELLED.value,
            ),
        )
        .fetchone()
    )
    return 0 if row is None else int(row["overdue"])


def _pace_by_employee(team_store: TeamStore, devs: list[str], day: str) -> dict[str, Any]:
    """Friday pace per person — FAIL-SAFE, and never silently favourable.

    A pace that cannot be computed reads ``None`` (the field is present and
    explicitly unknown), never 0: "we could not measure" and "you scored zero"
    are different answers, and only one of them is an accusation.
    """
    try:
        statuses = pace_statuses(team_store, devs, today=date.fromisoformat(day))
    except Exception:  # noqa: BLE001 -- a pace failure must not take the view down
        return dict.fromkeys(devs)
    return {
        employee_id: {
            "points": status.points,
            "floor": status.floor,
            "prorated_target": round(status.prorated_target, 2),
            "on_pace": status.on_pace,
        }
        for employee_id, status in statuses.items()
    }


@router.get("/accountability")
def get_accountability(store: StoreDep, day: str | None = Query(default=None)) -> dict[str, Any]:
    """One LOCAL day, per active dev: promised, delivered, stuck, learned.

    Per-person and deliberately NOT a leaderboard (open question 6): the
    scoreboard already ranks. This view answers "what did I say I would do, and
    what does the board say happened" for one person at a time.
    """
    target = commitments.local_today() if day is None else _validated_day(day).isoformat()
    team_store = _team_store(store)
    devs = commitments.active_devs(team_store)
    names = {
        str(row["id"]): str(row["name"])
        for row in CompanyGoalsStore(_sqlite_store(store)).list_employees()
    }
    queues = team_store.team_queues(employee_ids=devs)
    pace = _pace_by_employee(team_store, devs, target)
    people: list[dict[str, Any]] = []
    for employee_id in devs:
        rows = team_store.list_commitments(day=target, employee_id=employee_id)
        improvement = next((row for row in rows if str(row["kind"]) == "improvement"), None)
        bucket = queues.get(employee_id)
        reasons = _blocked_reasons(store, employee_id)
        done_cards = [
            {
                "id": card["id"],
                "ref": card["ref"],
                "title": card["title"],
                "size": card["size"],
                "completion_state": completion_state(card),
                "automation_maturity": card["automation_maturity"],
                "automation_note": card["automation_note"],
                "verification_failed_reason": card["verification_failed_reason"],
                "evidence": _evidence_items(team_store, str(card["id"])),
            }
            for card in _done_today(store, employee_id, target)
        ]
        people.append(
            {
                "employee_id": employee_id,
                "name": names.get(employee_id, employee_id),
                "commitments": rows,
                "improvement_of_day": improvement,
                "counts": {} if bucket is None else bucket.counts,
                "done_today": done_cards,
                "blocked": []
                if bucket is None
                else [
                    {
                        "id": card.id,
                        "ref": card.ref,
                        "title": card.title,
                        "blocked_reason": reasons.get(str(card.id), ""),
                    }
                    for card in bucket.blocked
                ],
                "overdue": _overdue_count(store, employee_id, target),
                "evidence_today": _evidence_today(store, employee_id, target),
                "learning_captures": _learning_captures(store, employee_id, target),
                "points_pace": pace.get(employee_id),
            }
        )
    return {"day": target, "people": people}


# --------------------------------------------------------------------------
# natural-language assignment (spec §2 gap) — DETERMINISTIC, no LLM
# --------------------------------------------------------------------------


class NlAssignBody(BaseModel):
    text: str = Field(min_length=1, max_length=2048)


#: The whole grammar. Two shapes, both anchored, both requiring a name and a
#: title — a parser that guesses is a parser that files somebody else's work
#: under your name.
#: The grammar, as an EXACT closed set (Sol review, item 6) — the dashboard
#: composer's client-side intercept mirrors these same three shapes, and the two
#: must agree or the surface either swallows chat as assignments or drops
#: assignments into an LLM turn. Every pattern is anchored, case-insensitive,
#: and requires BOTH a name and a non-empty title: a parser that guesses files
#: somebody else's work under your name. The fixture strings both test suites
#: run against live in ``tests/team/test_nl_assign.py``
#: (``NL_ASSIGN_ACCEPTED`` / ``NL_ASSIGN_REJECTED``).
_NL_GRAMMAR: tuple[re.Pattern[str], ...] = (
    # (a) the Slack /task spelling, so muscle memory works in the dashboard too.
    re.compile(
        r"^/task\s+assign\s+@?(?P<name>[A-Za-z][\w.-]*)\s+(?P<title>\S.*)$",
        re.IGNORECASE,
    ),
    # (b) "give <name> a task to ..." and its colon form.
    re.compile(
        r"^give\s+@?(?P<name>[A-Za-z][\w.-]*)\s+a\s+task\s*(?:to\s+|:\s*)(?P<title>\S.*)$",
        re.IGNORECASE,
    ),
    # (c) the terse form.
    re.compile(
        r"^assign\s+@?(?P<name>[A-Za-z][\w.-]*)\s+(?P<title>\S.*)$",
        re.IGNORECASE,
    ),
)

#: The PROPOSE half of the same surface (automation backlog, 2026-08-14). Two
#: shapes, checked BEFORE the assign grammar because "propose an automation to
#: X" must never be read as an assignment. A proposal names no assignee — it
#: lands in ``awaiting_approval`` for the operator — so these patterns capture a title
#: and nothing else.
_NL_PROPOSE_GRAMMAR: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^propose\s+an?\s+automation\s*(?:to\s+|:\s*)(?P<title>\S.*)$",
        re.IGNORECASE,
    ),
    re.compile(r"^propose\s+automation\s*:\s*(?P<title>\S.*)$", re.IGNORECASE),
)

_FOR_HINT_RE = re.compile(r"(?:^|\s)for\s+(?P<who>owner|alice|bob|ai)\s*$", re.IGNORECASE)

_AC_SUFFIX_RE = re.compile(r"\|\s*ac:\s*(?P<criteria>.+)$", re.IGNORECASE)
_COMPANY_RE = re.compile(r"(?:^|\s)#(?P<slug>[a-z0-9-]+)", re.IGNORECASE)

#: The automation CATEGORY token, boundary-checked and identical to the Slack
#: surface's (``slack_updates._CATEGORY_FLAG_RE``). Underscores are part of the
#: token: ``#dev_tooling`` and ``#dev-tooling`` name the same category, and a
#: pattern that stopped at the underscore would consume ``#dev`` and file the
#: work under whatever that matched instead.
_CATEGORY_RE = re.compile(r"(?:^|\s)#(?P<slug>[A-Za-z0-9_-]+)(?=\s|$)")

_NL_PROPOSE_HELP = (
    "try 'propose an automation to <title>' or 'propose automation: <title>' "
    "(optional: #category, 'for owner|alice|bob|ai', and '| ac: <criteria>')"
)

_NL_HELP = (
    "try '/task assign @name <title>', 'give <name> a task to <title>' "
    "(or 'a task: <title>'), or 'assign <name> <title>' "
    "(optional: #company, a trailing deadline like 'tomorrow', "
    "and '| ac: <criteria>' to set acceptance criteria) — or "
    "'propose an automation to <title>' to file one for the operator's approval"
)


def _resolve_employee_by_name(store: StoreDep, name: str) -> str | None:
    """``bob`` -> ``emp_bob``, case-insensitively, ACTIVE roster only.

    Matches either the id's suffix or the first token of the display name, so
    both "Bob" and "bob" land, and an inactive teammate never does.
    """
    wanted = name.strip().lstrip("@").casefold()
    for row in CompanyGoalsStore(_sqlite_store(store)).list_employees(status="active"):
        employee_id = str(row["id"])
        first_name = str(row["name"] or "").split(" ")[0].casefold()
        if wanted in {employee_id.casefold(), employee_id.removeprefix("emp_").casefold()}:
            return employee_id
        if first_name and wanted == first_name:
            return employee_id
    return None


def _nl_propose(store: StoreDep, collab: Any, remainder: str, acceptance: str) -> dict[str, Any]:
    """File an automation proposal from one dashboard sentence.

    Same primitive the Slack ``/task propose`` verb calls, so a proposal made
    from the dashboard is byte-identical to one made in a channel — including
    the category resolution and the stored ``for X`` hint, which approval reads
    back. ``proposed_by`` is the operator for the documented reason every
    dashboard write is: the surface carries one shared token.
    """
    remainder, hint = remainder.strip(), None
    hint_match = _FOR_HINT_RE.search(remainder)
    if hint_match is not None:
        hint = hint_match.group("who").lower()
        remainder = remainder[: hint_match.start()].strip()
    category = None
    category_match = _CATEGORY_RE.search(remainder)
    if category_match is not None:
        category = category_match.group("slug").lower()
        remainder = (
            remainder[: category_match.start()] + " " + remainder[category_match.end() :]
        ).strip()
    title = " ".join(remainder.split())
    if not title:
        fail(400, "validation", f"the proposal needs a title — {_NL_PROPOSE_HELP}")
    ambiguity = match_automation_category(collab, category)
    if ambiguity.ambiguous:
        # Named candidates, not a guess: with "customer service" and "customer
        # success" both on the ladder, silently taking the older one files the
        # work where the person who typed it will not look for it.
        fail(
            400,
            "validation",
            f"#{category} matches {len(ambiguity.ambiguous)} categories: "
            + ", ".join(ambiguity.ambiguous)
            + " — name one exactly",
            {"category": category, "candidates": list(ambiguity.ambiguous)},
        )
    try:
        task = propose_automation(
            collab,
            title=title,
            proposed_by=OPERATOR_EMPLOYEE_ID,
            category=category,
            assignee_hint=hint,
            acceptance_criteria=acceptance,
            description="Proposed from the dashboard composer.",
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return {
        "task_id": task.id,
        "kind": "automation_proposal",
        "title": title,
        "category": category,
        "assignee_hint": hint,
        "goal_id": task.goal_id,
        "acceptance_criteria": task.acceptance_criteria,
        "status": str(task.status),
        "message": (
            f"Proposed: {title}"
            + (f" [#{category}]" if category else "")
            + (f" for {hint}" if hint else "")
            + " — awaiting the operator's approval."
        ),
    }


@router.post("/nl-assign", status_code=201)
def nl_assign(body: NlAssignBody, store: StoreDep, collab: CollabStoreDep) -> dict[str, Any]:
    """Assign a card from one sentence. Deterministic grammar, no model call.

    The dashboard's missing owner-assigning path (chats only ever produced
    ownerless cards). It reuses the /task engine's own pieces — ``parse_deadline``,
    ``resolve_company_goal``, ``assign_adhoc_task`` — so a card created from the
    dashboard is byte-identical to one created from Slack.

    EVIDENCE ALWAYS BINDS (review S1): ``acceptance_criteria`` defaults to the
    TITLE rather than empty, so the store's evidence-before-done gate applies to
    every card this route creates. An explicit ``| ac: <criteria>`` overrides it.

    ``actor`` is the operator: the dashboard is the operator's surface and
    carries a single shared token (the same documented residual as
    TaskOverview's hard-coded verifier).
    """
    text = " ".join(str(body.text).split())
    acceptance = ""
    ac_match = _AC_SUFFIX_RE.search(text)
    if ac_match is not None:
        acceptance = ac_match.group("criteria").strip()
        text = text[: ac_match.start()].strip()

    proposal = next(
        (found for pattern in _NL_PROPOSE_GRAMMAR if (found := pattern.match(text))), None
    )
    if proposal is not None:
        return _nl_propose(store, collab, proposal.group("title"), acceptance)

    match = next((found for pattern in _NL_GRAMMAR if (found := pattern.match(text))), None)
    if match is None:
        fail(
            400, "validation", f"could not read that as an assignment — {_NL_HELP}", {"text": text}
        )

    remainder = match.group("title").strip()
    company_match = _COMPANY_RE.search(remainder)
    slug = None
    if company_match is not None:
        slug = company_match.group("slug").lower()
        remainder = (
            remainder[: company_match.start()] + " " + remainder[company_match.end() :]
        ).strip()
    title, deadline_phrase = split_deadline(remainder)
    title = " ".join(title.split())
    if not title:
        fail(400, "validation", f"the task needs a title — {_NL_HELP}", {"text": body.text})

    owner = _resolve_employee_by_name(store, match.group("name"))
    if owner is None:
        fail(
            400,
            "validation",
            f"no active teammate called {match.group('name')!r}",
            {"name": match.group("name")},
        )

    goal_id = None if slug is None else resolve_company_goal(collab, slug)
    if slug is not None and goal_id is None:
        fail(400, "validation", f"unknown company #{slug}", {"company": slug})

    task = assign_adhoc_task(
        collab,
        title=title,
        description="",
        owner_employee_id=owner,
        actor=OPERATOR_EMPLOYEE_ID,
        goal_id=goal_id,
        acceptance_criteria=acceptance or title,
        due_date=parse_deadline(deadline_phrase),
    )
    return {
        "task_id": task.id,
        "owner_employee_id": owner,
        "title": title,
        "acceptance_criteria": acceptance or title,
        "goal_id": goal_id,
        "due_date": task.due_date,
        "message": (
            f"Assigned to {owner}: {title}"
            + (f" (due {task.due_date})" if task.due_date else "")
            + ". Acceptance criteria default to the title — add "
            "'| ac: <criteria>' to set your own."
        ),
    }


class SessionReportBody(BaseModel):
    """One machine's collector report (omniagentos.team.session_collector)."""

    schema_version: int = Field(default=1, alias="schema")
    employee_id: str
    host: str = ""
    generated_at: str = ""
    sessions: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    active_count: int | None = None
    recent_count: int | None = None
    # Per-machine Claude account balances (session_collector.collect_claude_usage).
    # Without an explicit field pydantic drops the key on model_dump, so a remote
    # laptop's balance would silently vanish from its drop-file — the exact
    # missing-source-as-favourable shape the balance alert exists to prevent.
    claude_usage: dict[str, Any] | None = None
    # Owner kill-switch marker (session_collector opt-out). Same pydantic trap
    # as claude_usage: without explicit fields the marker is dropped and an
    # opted-out dev renders as measured zero activity instead of a choice.
    opted_out: bool | None = None
    opted_out_since: str | None = None

    model_config = {"populate_by_name": True}


@router.post("/sessions/report", status_code=204)
def post_session_report(body: SessionReportBody, store: StoreDep) -> Response:
    """Land a collector drop-file for the session tracker.

    Reports are informational (they never touch scoring or the board), so the
    write is deliberately simple: roster-validated employee, capped session
    list, atomic replace of ``var/team-sessions/<employee>.json``. The shared
    tunnel principal cannot distinguish which teammate posted, so the body's
    ``employee_id`` is trusted here — an accepted residual documented in
    docs/operations/remote-board-access.md ("Who can see what").
    """
    if re.fullmatch(r"[A-Za-z0-9_-]+", body.employee_id) is None:
        fail(400, "validation", "employee_id has an invalid shape")
    if CompanyGoalsStore(_sqlite_store(store)).get_employee(body.employee_id) is None:
        fail(404, "not_found", "employee not found", {"id": body.employee_id})
    for session in body.sessions:
        title = str(session.get("description") or "")
        if len(title) > 300:
            session["description"] = title[:300]
    from omniagentos.runtime_paths import resolve_var_root
    from omniagentos.team.session_collector import _write_atomic

    _write_atomic(
        Path(resolve_var_root()) / "team-sessions" / f"{body.employee_id}.json",
        body.model_dump(by_alias=True),
    )
    return Response(status_code=204)
