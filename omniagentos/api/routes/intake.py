"""HTTP surface for the intake loop: clarify -> plan -> dispatch -> live board.

* ``POST /api/intake/clarify`` — one turn of the clarifying agent.
* ``POST /api/intake/dispatch`` — turn a confirmed spec into a live board card, then
  either a queued run (``execute="readonly"|"tools"``, default) or a live, MONITORED
  Claude Code session via the Session Bridge (``execute="session"``).
* ``POST /api/intake/quick`` — the cockpit's no-dialog path: create a board card
  immediately, then plan and queue orchestration in the background.
* ``POST /api/intake/plan`` — kick off Fable PLAN + ROUTE as a background job (never
  runs inline on the request — see the job helpers below).
* ``GET  /api/intake/plan/{job_id}`` — poll a plan job for its PLAN + ROUTE preview.
* ``POST /api/intake/plan/{job_id}/confirm`` — materialise a ready plan job: create
  the project hierarchy, then dispatch every task (``execute="session"`` spawns one
  monitored session PER task).
* ``GET  /api/board`` — the live kanban, reconciled from each card's linked run.
  Bounded on request (``status`` / ``updated_after`` / ``limit``) and
  conditional (``ETag`` / ``If-None-Match`` → ``304``); with no parameters it is
  the same unbounded feed it has always been.
* ``GET  /api/board/{task_id}`` — ONE reconciled card, same shape as a list
  element, so a detail view no longer downloads the whole board to find a row.
* ``GET  /api/board/needs-response`` — ranked Needs Response band (strict
  predicate: only conversations that cannot proceed until the operator acts).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.deps import PolicyDep, StoreDep, get_store
from omniagentos.api.routes.categories import LonghaulStoreDep
from omniagentos.api.routes.collab import CollabStoreDep, fail
from omniagentos.api.routes.sessions import _authorized
from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.intake.contracts import ClarifyTurn, RefinedSpec
from omniagentos.intake.fallback import FAST_PLANNER_MODEL, speed_planner_llm
from omniagentos.intake.fastlane import classify_task_speed, parse_target_location
from omniagentos.intake.needs_response import list_needs_response_payload
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.planner import (
    HierarchyDAL,
    PlannerLLM,
    ProjectPlan,
    ProjectStoreHierarchyDAL,
    RouteDecision,
    RouterLLM,
    default_planner_llm,
    default_router_llm,
    plan_goal,
    provision_plan,
    route_project,
)
from omniagentos.intake.service import (
    ClarifyLLM,
    _emit_board_event,
    _reconcile_sessions_dal,
    _sqlite_db_path,
    clarify_intake,
    compute_board_eta,
    create_pending_plan_card,
    create_queued_goal_card,
    default_clarify_llm,
    dispatch_spec,
    pause_board_task_work,
    persist_card_intake_directives,
    quick_project_routing_mode,
    reconcile_board,
    reconcile_board_card,
    refined_spec_from_plan,
)
from omniagentos.plans.store import PlansStore

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

AuthorizedDep = Annotated[None, Depends(_authorized)]


def _resolve_resume_orchestration() -> Any:
    """Lazy resolver for resume_orchestration — patchable for tests."""
    from omniagentos.intake.service import resume_orchestration

    return resume_orchestration


def get_clarify_llm() -> ClarifyLLM:
    """The clarify LLM seam (overridable in tests via dependency_overrides)."""
    return default_clarify_llm


def get_planner_llm() -> PlannerLLM:
    """The planner (Fable high/max) seam — overridable in tests."""
    return default_planner_llm


def get_router_llm() -> RouterLLM:
    """The project-router (Fable) seam — overridable in tests."""
    return default_router_llm


ClarifyLLMDep = Annotated[ClarifyLLM, Depends(get_clarify_llm)]
PlannerLLMDep = Annotated[PlannerLLM, Depends(get_planner_llm)]
RouterLLMDep = Annotated[RouterLLM, Depends(get_router_llm)]


class IntakeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ClarifyRequest(IntakeModel):
    draft: str = ""
    history: list[ClarifyTurn] = Field(default_factory=list)


class DispatchRequest(IntakeModel):
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    suggested_discipline: str | None = None
    suggested_priority: str = "normal"
    required_expertise: list[str] = Field(default_factory=list)
    harness: str | None = None
    project_id: str | None = None
    # Execution posture. "readonly" (default) keeps the safe text-only path;
    # "tools" opts the dispatched run into real file/shell work -- which the
    # runner's policy gate then parks for human approval before anything runs;
    # "session" skips the runner queue and launches a live, MONITORED Claude Code
    # session via the Session Bridge; "orchestrate" hands the spec to the Orchestrator
    # "worker-as-planner" loop (see intake.service.dispatch_spec); "single" (D10)
    # is orchestrate with the auto solo-vs-swarm upgrade HARD-suppressed — the
    # Mode dial's Single Task.
    execute: Literal["readonly", "tools", "session", "orchestrate", "single"] = "readonly"
    # When true, provision a scoped project first (determine the working dir +
    # connector APIs the goal needs) and scope the dispatched run to exactly it.
    # Implies tools mode.
    provision: bool = False
    # "session" mode only: the model the live Claude Code session runs as.
    # Defaults to the intake/Fable model (FABLE_MODEL) when unset.
    model: str | None = None
    # "orchestrate" mode only: the master knob (fast|balanced|quality) and optional
    # pins {planner_model, planner_effort, executor_model_or_lineage, executor_effort}
    # that DRIVE the orchestrator's tier/model selection. Ignored by other modes.
    priority: Literal["fast", "balanced", "quality"] = "balanced"
    pins: dict[str, Any] | None = None
    # D10 Mode dial: the speed knob. OPTIONAL so an omitted field stays
    # distinguishable from the dial's explicit Auto position (F3): None (what
    # every legacy client sends) keeps today's behavior byte-identically —
    # the legacy ``priority`` field wins and planning effort stays
    # complexity-derived. An EXPLICIT speed is a Mode-dial choice: fast→'fast',
    # auto→'balanced', ultra→'quality', where a non-auto speed WINS over the
    # legacy ``priority`` and 'auto' plans on Fable at xhigh (D10). The UI
    # always sends an explicit value.
    speed: Literal["fast", "auto", "ultra"] | None = None


class PlanRequest(IntakeModel):
    # The clarified goal to plan (clarify runs upstream; this is the confirmed ask).
    goal: str
    harness: str | None = None
    # "single" (D10 Mode dial) is accepted wherever confirm-time dispatch can
    # happen: each planned task then dispatches as orchestrate with the auto
    # solo-vs-swarm upgrade HARD-suppressed (Plan-first + Single Task must
    # never auto-swarm at approval).
    execute: Literal["readonly", "tools", "session", "single"] = "readonly"
    # D10 Mode dial: persisted on the plan job (alongside execute) so the
    # approval-time dispatch honors the dial. None (omitted — legacy clients)
    # keeps today's planning + dispatch behavior byte-identically.
    speed: Literal["fast", "auto", "ultra"] | None = None


class QuickRequest(IntakeModel):
    """The no-dialog cockpit path: plan once, then execute or leave it pending."""

    goal: str = ""
    # executor/priority/category stay TOLERATED-OPTIONAL FOREVER (D11): the
    # simplified composer no longer sends them, but existing automation
    # (fastlane callers, board sweep, curl scripts) still may, and their
    # meaning is preserved.
    executor: str = "auto"
    priority: str = "balanced"
    plan: bool = False
    project_id: str | None = None
    lane: Literal["auto", "fast", "longhaul"] = "auto"
    category: str | None = None
    # WP10 passthrough + D10 Mode dial: execute="swarm" routes the goal into
    # Swarm Mode dispatch (plan -> provision run(s) as board cards ->
    # activation ONLY behind OMNIAGENTOS_SWARM_EXECUTE); execute="single" is
    # the Mode dial's Single Task — a HARD suppress of the auto solo-vs-swarm
    # upgrade (the dispatch orchestrates, and the swarm planner NEVER runs).
    # An omitted/None execute keeps today's auto-default behavior exactly
    # (API/automation backcompat). Anything else is a 422; the other execution
    # postures keep their dedicated endpoints.
    # Precedence: plan=true (plan-first preview) and an EXPLICIT lane
    # (fast/longhaul) both take absolute priority over this field -- an explicit
    # lane is never intercepted by swarm planning (see contracts/swarm-api.md).
    execute: Literal["swarm", "single"] | None = None
    # D10 Mode dial: fast|auto|ultra, OPTIONAL (F3). None (omitted — every
    # legacy client) keeps today's behavior byte-identically: the legacy
    # ``priority`` field wins and planning effort stays complexity-derived.
    # An EXPLICIT speed maps centrally onto the orchestrator's priority knob
    # (fast→'fast', auto→'balanced', ultra→'quality'); a non-auto speed WINS
    # over the legacy ``priority`` field, while speed="auto" keeps a legacy
    # priority untouched. An explicit speed also drives the planner chain
    # (fast plans on gemini-3.6-flash with Fable fallback; auto is Fable at
    # xhigh; ultra is Fable at max) and is persisted on swarm runs. The UI
    # always sends an explicit value.
    speed: Literal["fast", "auto", "ultra"] | None = None
    history: list[ClarifyTurn] = Field(default_factory=list)


class PlanConfirmRequest(IntakeModel):
    # "auto" (default) accepts Fable's own route decision; an existing project or
    # sub-project id nests the plan's root project under it instead; "new:<name>"
    # forces a new top-level project with that name. Drives the dashboard's project
    # selector (W4-frontdoor): Auto / pick from the tree / create new.
    project_override: str = "auto"
    harness: str | None = None
    # "single" mirrors PlanRequest (D10): planned tasks dispatch orchestrate
    # with the auto solo-vs-swarm upgrade HARD-suppressed.
    execute: Literal["readonly", "tools", "session", "single"] | None = None
    # D10 Mode dial: an explicit confirm-time speed OVERRIDES the speed the
    # plan job persisted; None keeps the job's own (the UI sends the same
    # value on both calls).
    speed: Literal["fast", "auto", "ultra"] | None = None


# Who an approval is attributed to. Deliberately NOT a request field: the
# control plane has exactly one principal (the local operator behind the
# session-token gate), so the audit row records the AUTHENTICATED approver
# rather than an unverified name supplied by the caller. An approval row with a
# null approver answers the wrong half of "who approved this".
_DEFAULT_DECIDER = "operator"


class TaskMessageRequest(IntakeModel):
    content: str


def _require_board_task(task_id: str, collab_store: Any) -> dict[str, Any]:
    task = collab_store.get_board_task(task_id)
    if task is None or task.get("archived_at") is not None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    return task


def _swarm_attempts_for_board_task(task: dict[str, Any], collab_store: Any) -> list[dict[str, Any]]:
    """Project a swarm card's attempts into the task-detail attempt shape.

    A run-root card represents the whole run, matching the sessions projection;
    member cards show only attempts whose ``board_task_id`` is that card. Swarm
    persists the executor name as ``provider`` while the shared dashboard
    timeline calls it ``harness``, so preserve the raw row and add that alias.
    """
    from omniagentos.swarm.dal import SwarmDal

    task_id = str(task["id"])
    run_id = str(task["swarm_run_id"])
    swarm_dal = SwarmDal(_sqlite_db_path(collab_store))
    try:
        run = swarm_dal.get_run(run_id)
        if run is not None and str(run.get("board_task_id") or "") == task_id:
            attempts = swarm_dal.attempts_for_run(run_id)
        else:
            attempts = swarm_dal.list_attempts(task_id)
    finally:
        swarm_dal.close()
    return [
        {
            **attempt,
            "harness": attempt.get("harness") or attempt.get("provider") or "",
        }
        for attempt in attempts
    ]


def _turn_with_delivery(turn: dict[str, Any]) -> dict[str, Any]:
    """Expose delivery metadata without leaking the storage JSON envelope."""
    import json

    out = dict(turn)
    try:
        meta = json.loads(out.get("meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    delivery = meta.get("delivery") if isinstance(meta, dict) else None
    out["delivery_status"] = delivery
    return out


def _emit_task_event(store: Any, event_type: str, task_id: str, payload: dict[str, Any]) -> None:
    """Emit only the identifier/sequence/status payload allowed for task SSE."""
    try:
        store.insert_event(
            event_type,
            "api",
            event_type,
            target_type="board_task",
            target_id=task_id,
            payload=payload,
        )
    except Exception:
        return


def get_hierarchy_dal(store: StoreDep) -> HierarchyDAL:
    """The hierarchy DAL seam (W3-hierarchy backs it; overridable in tests)."""
    return ProjectStoreHierarchyDAL(store)


HierarchyDep = Annotated[HierarchyDAL, Depends(get_hierarchy_dal)]


def _get_plans_store(store: StoreDep) -> PlansStore:
    """The plans DAL seam, composed over the REQUEST's store (overridable in tests).

    Deliberately not ``PlansStore(SqliteStore(db_path))``: that constructor runs
    the whole migration ladder (measured 6.9 ms) on every call, and the
    dashboard polls this route in a loop.
    """
    return PlansStore(cast(SqliteStore, store))


PlanStoreDep = Annotated[PlansStore, Depends(_get_plans_store)]


def _process_plans_store() -> PlansStore | None:
    """The process-wide plans DAL, for callers that pass no store of their own.

    ``omniagentos/api/routes/chats.py`` (plan mode, §3.6) drives ``_run_plan_job``
    with the pre-durability signature and no store argument. Resolving the
    lru_cached ``get_store()`` singleton — the very store the API serves requests
    from — keeps that path durable too, without a second connection and without
    editing another lane's file. Returns None only if the control-plane store
    itself cannot be built, which is logged, never swallowed.
    """
    try:
        return PlansStore(cast(SqliteStore, get_store()))
    except Exception:
        LOG.error("no durable plans store available for this process", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Plan jobs — in-memory, process-local job store.
#
# Fable planning (effort high/max) can legitimately run for minutes; no HTTP
# request may ever be held open for it (W4-frontdoor: "never-wedged reliability").
# ``POST /intake/plan`` only registers a job and schedules its body as a
# ``BackgroundTasks`` callback, returning immediately; the dashboard polls
# ``GET /intake/plan/{job_id}`` until the PLAN + ROUTE preview is ready, then
# commits it via ``POST /intake/plan/{job_id}/confirm``. A single operator's
# control plane never needs this to survive a restart or scale across processes,
# so a locked dict (capped so a long-lived process can't grow it unbounded) is
# the right amount of machinery -- no new table, no new store.
# ---------------------------------------------------------------------------

_PLAN_JOBS: dict[str, dict[str, Any]] = {}
_PLAN_JOBS_LOCK = threading.Lock()
_MAX_PLAN_JOBS = 500


def _new_plan_job_id() -> str:
    return f"planjob_{uuid.uuid4().hex[:20]}"


def _evict_plan_jobs_locked() -> None:
    """Cap the cache. Call with ``_PLAN_JOBS_LOCK`` held.

    Eviction used to lose a plan outright. It no longer does: an evicted READY
    job is rehydrated from its durable row by ``_plan_job_get_with_fallback`` on
    the next poll or confirm. That is only true because the durable write is
    verified — see ``_sync_plan_to_database``, which reports failure instead of
    returning quietly.
    """
    if len(_PLAN_JOBS) < _MAX_PLAN_JOBS:
        return
    oldest = sorted(_PLAN_JOBS.items(), key=lambda kv: kv[1]["created_at"])
    for old_id, _ in oldest[: len(_PLAN_JOBS) - _MAX_PLAN_JOBS + 1]:
        _PLAN_JOBS.pop(old_id, None)


def _plan_job_create(goal: str, harness: str | None, execute: str, speed: str | None = None) -> str:
    """Register a new plan job in the RUNNING state and return its id."""
    job_id = _new_plan_job_id()
    with _PLAN_JOBS_LOCK:
        _evict_plan_jobs_locked()
        _PLAN_JOBS[job_id] = {
            "status": "running",
            "goal": goal,
            "harness": harness,
            # D10 (F1): execute + speed are PERSISTED on the job so the
            # approval-time dispatch (confirm -> provision_plan) honors the
            # dial exactly as chosen when planning started.
            "execute": execute,
            "speed": speed,
            "plan": None,
            "route": None,
            "route_target_name": None,
            "error": None,
            "provision_result": None,
            # False until the durable write is CONFIRMED (never assumed).
            "durable": False,
            # The approved execution parameters, once there are any.
            "execution": None,
            "created_at": time.monotonic(),
        }
    return job_id


def _plan_job_get(job_id: str) -> dict[str, Any] | None:
    with _PLAN_JOBS_LOCK:
        job = _PLAN_JOBS.get(job_id)
        return dict(job) if job is not None else None


def _plan_job_put(job_id: str, job: dict[str, Any]) -> None:
    """Seed the cache with a job rehydrated from its durable row.

    ``_plan_job_update`` only patches an entry that already exists, so using it
    to "refresh the cache" after a restart was a no-op and every poll paid for a
    fresh database read.
    """
    with _PLAN_JOBS_LOCK:
        _evict_plan_jobs_locked()
        _PLAN_JOBS[job_id] = dict(job)


def _plan_job_update(job_id: str, **patch: Any) -> None:
    with _PLAN_JOBS_LOCK:
        job = _PLAN_JOBS.get(job_id)
        if job is not None:
            job.update(patch)


def _plan_row_payload(job: dict[str, Any]) -> dict[str, Any]:
    """The durable columns a READY job maps onto.

    Everything ``plan_confirm`` needs is here, not just the plan text: a row with
    a plan and no route/dial cannot be approved after a restart (route would be
    None and ``RouteDecision.model_validate`` would 500).
    """
    import json

    route = job.get("route")
    return {
        "plan_json": json.dumps(job.get("plan") or {}),
        "goal": job.get("goal"),
        "harness": job.get("harness"),
        "execute_mode": job.get("execute"),
        "speed": job.get("speed"),
        "route_json": json.dumps(route) if route else None,
        "route_target_name": job.get("route_target_name"),
    }


def _sync_plan_to_database(job_id: str, plans: PlansStore | None = None) -> bool:
    """Write a READY job through to its durable row. Returns whether it landed.

    Every failure mode here used to be silent, and each one produced the same
    operator-visible symptom (a plan that "dies on restart") with no trace:

    * no store — now resolved from the process-wide singleton, and logged if
      even that is unavailable;
    * the job evicted from the LRU cache before the write — now ERROR, not a
      quiet ``return``;
    * a database error — now ERROR, and the caller marks the job non-durable so
      the poll route can say so instead of implying durability.
    """
    if plans is None:
        plans = _process_plans_store()
        if plans is None:
            return False

    job = _plan_job_get(job_id)
    if job is None:
        LOG.error(
            "plan job %s vanished before its durable write (LRU eviction?) — "
            "the plan is unreachable and will not survive a restart",
            job_id,
        )
        return False

    if job.get("status") != "ready" or not job.get("plan"):
        # Transient (running/error): there is no planned content to persist yet.
        return False

    payload = _plan_row_payload(job)

    def prepare(row: dict[str, Any]) -> dict[str, Any]:
        patch = dict(payload)
        # Never drag a decided plan back to 'ready' (the store would refuse it):
        # a re-sync after approval refreshes content only.
        if row.get("status") in ("draft", "ready"):
            patch["status"] = "ready"
        return patch

    try:
        if plans.get_plan(job_id) is None:
            plans.create_plan(plan_id=job_id, version=1, status="ready", **payload)
        else:
            plans.update_plan(job_id, prepare)
    except Exception:
        LOG.error("durable plan write FAILED for %s", job_id, exc_info=True)
        return False
    return True


def _planner_brief_markdown(plan: Any, job: dict[str, Any]) -> str:
    """Render the confirmed plan as the card's ``org_json.planner_brief`` (§3.9).

    Markdown, not a JSON dump — the drawer's Plan tab renders this directly.
    """
    lines = [f"# Plan: {plan.project_name}"]
    goal = str(job.get("goal") or "").strip()
    if goal:
        lines += ["", f"**Goal:** {goal}"]
    if plan.description:
        lines += ["", plan.description]
    lines += ["", f"Complexity: {plan.complexity} · Effort: {plan.effort}"]
    if plan.tasks:
        lines += ["", "## Tasks"]
        for task in plan.tasks:
            title = str(getattr(task, "title", "") or "").strip()
            if title:
                lines.append(f"- {title}")
    for sub in plan.sub_projects:
        sub_name = str(getattr(sub, "name", "") or "").strip()
        if sub_name:
            lines.append(f"\n## {sub_name}")
        for task in getattr(sub, "tasks", []) or []:
            title = str(getattr(task, "title", "") or "").strip()
            if title:
                lines.append(f"- {title}")
    return "\n".join(lines)


def _run_plan_job(
    job_id: str,
    goal: str,
    hierarchy: HierarchyDAL,
    planner_llm: PlannerLLM,
    router_llm: RouterLLM,
    speed: str | None = None,
    plans: PlansStore | None = None,
) -> None:
    """Background body of ``POST /intake/plan``: PLAN then ROUTE, never inline.

    Runs after the kickoff response has already gone out (FastAPI
    ``BackgroundTasks``), so the request that started this never holds the
    connection through Fable's planning budget. ``plan_goal``/``route_project``
    already degrade to deterministic heuristics when the model is unavailable; this
    still guards against any other failure (a bad hierarchy read, a bug) so it lands
    the job in ``error`` rather than raising into the background worker.
    """
    llm = planner_llm
    if llm is default_planner_llm and speed is not None:
        # D10 (F1/F3): an EXPLICIT dial speed picks the planning chain/effort
        # (fast → gemini-3.6-flash with Fable fallback; auto → Fable xhigh;
        # ultra → Fable max); an omitted speed keeps the legacy
        # complexity-derived Fable effort byte-identically. An injected/
        # overridden planner seam always wins.
        llm = speed_planner_llm(speed)
    try:
        existing = hierarchy.list_projects()
        plan = plan_goal(goal, llm=llm)
        route = route_project(plan, existing, llm=router_llm)
        target_name: str | None = None
        if route.decision == "existing" and route.parent_project_id:
            target_name = next(
                (
                    str(p.get("name"))
                    for p in existing
                    if str(p.get("id")) == route.parent_project_id
                ),
                None,
            )
    except Exception as exc:  # noqa: BLE001 -- a background job must never crash the worker.
        LOG.warning("plan job %s failed", job_id, exc_info=True)
        # Nothing durable to write: a failed job has no plan. (The old code
        # called the sync here anyway, where it could only no-op.)
        _plan_job_update(job_id, status="error", error=str(exc) or "planning failed")
        return
    _plan_job_update(
        job_id,
        status="ready",
        plan=plan.model_dump(),
        route=route.model_dump(),
        route_target_name=target_name,
    )
    # The plan is only real once it is durable: this is the write that lets the
    # operator poll AND approve it after a restart. Its outcome is recorded on
    # the job so ``GET /intake/plan/{job_id}`` can report it rather than imply it.
    _plan_job_update(job_id, durable=_sync_plan_to_database(job_id, plans))


@router.post("/intake/clarify")
def clarify(body: ClarifyRequest, llm: ClarifyLLMDep) -> dict[str, Any]:
    if not body.draft or not body.draft.strip():
        fail(400, "validation", "draft is required")
    result = clarify_intake(body.draft, body.history, llm=llm)
    return result.model_dump_public()


# D10 Mode dial: speed -> orchestrator priority (the existing tiers machinery:
# 'fast' forces CHEAP/superfast; 'quality' gives planner max + FUSION_ULTRA).
_SPEED_TO_PRIORITY: dict[str, str] = {
    "fast": "fast",
    "auto": "balanced",
    "ultra": "quality",
}


def _resolve_speed_priority(speed: str | None, priority: str) -> str:
    """The effective priority: a non-auto DIAL speed wins over the legacy field.

    ``None`` (field omitted — every legacy client, F3) and the dial's explicit
    "auto" position both keep the legacy ``priority`` untouched: "auto"'s own
    mapping, "balanced", IS the priority default, so existing automation that
    still sends priority behaves exactly as today.
    """
    if speed is not None and speed != "auto":
        return _SPEED_TO_PRIORITY[speed]
    return priority


def _pins_for_speed(speed: str | None, pins: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fast Mode pins the orchestrator's planner onto gemini-3.6-flash.

    ``orchestrator.core._plan`` resolves the planner adapter by lineage, so the
    pin makes the orchestrator's own planning pass run on the gemini CLI (with
    Fable fallback on any failure/format break). The model id is explicit — no
    modelintel registry lookup — so a stale registry cannot break Fast Mode.
    An operator-supplied planner_model pin always wins over the speed default.
    """
    if speed != "fast":
        return pins
    merged = dict(pins or {})
    merged.setdefault("planner_model", FAST_PLANNER_MODEL)
    return merged


# D12 force hatch: with the composer sending no execute (router decides), a
# leading "solo:" / "single:" / "swarm:" in the brief is the ONE cockpit way
# to force topology.
_GOAL_PREFIX_EXECUTE: dict[str, Literal["swarm", "single"]] = {
    "solo": "single",
    "single": "single",
    "swarm": "swarm",
}


def _parse_goal_execute_prefix(
    goal: str,
) -> tuple[str, Literal["swarm", "single"] | None]:
    """Split a leading topology-force prefix off a quick-dispatch goal.

    Returns ``(goal_without_prefix, forced_execute_or_None)``. The prefix is
    case-insensitive and must be followed by actual task text — a bare
    ``"swarm:"`` is treated as a normal goal. Stripping matters: the fastlane
    classifier and the planners must never see the literal prefix.
    """
    head, sep, rest = goal.partition(":")
    if not sep:
        return goal, None
    forced = _GOAL_PREFIX_EXECUTE.get(head.strip().lower())
    if forced is None:
        return goal, None
    rest = rest.strip()
    if not rest:
        return goal, None
    return rest, forced


def _parse_goal_prefixes(goal: str) -> tuple[str, bool, str | None]:
    """Parse wide: and pin:<formation>: prefixes from a title or goal.

    Returns (cleaned_goal, wide, pinned_formation).
    """
    wide = False
    pinned_formation = None
    cleaned = goal.strip()

    while True:
        lower_cleaned = cleaned.lower()
        if lower_cleaned.startswith("wide:"):
            wide = True
            cleaned = cleaned[5:].strip()
        elif lower_cleaned.startswith("pin:"):
            parts = cleaned.split(":", 2)
            if len(parts) >= 2:
                pinned_formation = parts[1].strip()
                cleaned = parts[2].strip() if len(parts) > 2 else ""
            else:
                break
        else:
            break

    return cleaned, wide, pinned_formation


@router.post("/intake/dispatch", status_code=201)
def dispatch(
    body: DispatchRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    if not body.title or not body.title.strip():
        fail(400, "validation", "title is required")

    cleaned_title, wide, pinned_formation = _parse_goal_prefixes(body.title)

    spec = RefinedSpec(
        title=cleaned_title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        suggested_discipline=body.suggested_discipline,
        suggested_priority=body.suggested_priority,
        required_expertise=body.required_expertise,
    )
    res = dispatch_spec(
        store,
        collab_store,
        policy_cfg,
        spec,
        harness=body.harness,
        project_id=body.project_id,
        execute=body.execute,
        provision=body.provision,
        model=body.model,
        priority=_resolve_speed_priority(body.speed, body.priority),
        pins=_pins_for_speed(body.speed, body.pins),
        speed=body.speed,
    )

    board_task_id = res.get("task_id") or res.get("board_task", {}).get("id")
    project_id = res.get("project_id")
    run_id = res.get("run_id")

    if board_task_id:
        try:
            from omniagentos.intake.preflight import build_preflight

            preflight = build_preflight(
                db_path=cast(SqliteStore, store)._db_path,
                task_id=board_task_id,
                title=cleaned_title,
                description=body.description,
                discipline=body.suggested_discipline,
                priority=body.suggested_priority,
                wide=wide,
                pinned_formation=pinned_formation,
            )

            import json

            collab_store._store._connection.execute(
                "UPDATE board_tasks SET preflight_json = ? WHERE id = ?",
                (json.dumps(preflight), board_task_id),
            )

            from omniagentos.formation.selector import _all, topology_for_formation
            from omniagentos.formation.telemetry import record_selection

            form = _all().get(preflight["formation_id"])
            if form is not None:
                record_selection(
                    collab_store._store._connection,
                    task_id=board_task_id,
                    goal=body.description or cleaned_title,
                    arm="formation",
                    formation_id=form.id,
                    confidence=preflight["confidence"],
                    low_confidence=bool(preflight["confidence"] < 0.5),
                    topology=topology_for_formation(form.id),
                    implementers=list(form.implementers or []),
                    reviewer=form.reviewer,
                    planner=form.planner,
                    mechanical_gate=bool(form.mechanical_gate),
                    models={
                        "implementers": list(form.implementers or []),
                        "reviewer": form.reviewer,
                        "planner": form.planner,
                    },
                    outcome="predicted",
                    source="production",
                )

            if project_id and run_id:
                for path in preflight["owned_paths"]:
                    collab_store._store._connection.execute(
                        "INSERT INTO project_scope_requests "
                        "(id, project_id, run_id, scope_kind, scope_value, action_class, "
                        "decision, status, reason, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id("psr"),
                            project_id,
                            run_id,
                            "dir",
                            path,
                            "file_write",
                            "planner_declared",
                            "granted",
                            "preflight owned path",
                            utc_now_iso(),
                        ),
                    )
            res["preflight"] = preflight
        except Exception:
            logging.getLogger(__name__).warning(
                "Preflight step failed during dispatch", exc_info=True
            )

    return res


_QUICK_EXECUTOR_PINS: dict[str, dict[str, str] | None] = {
    "auto": None,
    "sol": {"executor_model_or_lineage": "gpt-5.6-sol"},
    "terra": {"executor_model_or_lineage": "gpt-5.6-terra"},
    "luna": {"executor_model_or_lineage": "luna"},
    "opus": {"executor_model_or_lineage": "opus"},
}
_QUICK_PRIORITIES = {"fast", "balanced", "quality"}

# Phase 1 (A1): below this router confidence, quick intake spends its ONE
# clarify round before committing to a registry routing decision (enforce mode
# only — shadow never gates the response, it only logs what it computed).
_QUICK_ROUTING_LOW_CONFIDENCE = 0.5


def _quick_route_probe_plan(goal: str) -> ProjectPlan:
    """A minimal, planner-free :class:`ProjectPlan` for the synchronous low-
    confidence probe — the full Fable planning pass runs later, in the
    background dispatch, and is what an ``enforce`` hit/miss actually applies."""
    title = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal.strip())[:120]
    return ProjectPlan(project_name=title or "Quick task", description=goal.strip())


def _run_quick_dispatch(
    goal: str,
    *,
    executor: str,
    priority: str,
    plan_first: bool,
    project_id: str | None,
    board_task_id: str,
    run_id: str | None,
    store: Any,
    collab_store: Any,
    policy_cfg: Any,
    planner_llm: PlannerLLM,
    lane: Literal["auto", "fast", "longhaul"] = "auto",
    category: str | None = None,
    speed: str | None = None,
    execute: Literal["orchestrate", "single"] = "orchestrate",
    clarify_history: Sequence[ClarifyTurn] | None = None,
) -> None:
    """Background PLAN then queue orchestration without holding the HTTP request.

    Phase 1 (A1, ``OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE``): mode "off" is
    byte-for-byte today's behavior (the block below never runs). In "shadow"/
    "enforce" the project registry is consulted — and, in "enforce", applied —
    and ``build_preflight`` runs, BOTH strictly before ``dispatch_spec`` below
    ever resolves a workspace, exactly as the reviewer gate requires.
    """
    try:
        if lane == "longhaul":
            # Longhaul does its own light acceptance prep at dispatch — the
            # full planning pass is exactly the overhead this lane removes.
            title = goal.strip().splitlines()[0][:120]
            spec = RefinedSpec(title=title, description=goal)
            project_plan: ProjectPlan | None = None
        else:
            llm = planner_llm
            if llm is default_planner_llm and speed is not None:
                # D10 speed-aware planning (EXPLICIT speed only, F3): fast
                # plans on gemini-3.6-flash (Fable fallback); auto is Fable at
                # xhigh; ultra is Fable at max. An omitted speed (legacy
                # clients) keeps the complexity-derived Fable effort
                # byte-identically; an injected/overridden planner seam
                # always wins.
                llm = speed_planner_llm(speed)
            planning_goal = goal
            if clarify_history:
                # Exactly one clarify round already happened upstream (the
                # synchronous low-confidence gate in quick_dispatch) — fold its
                # answer(s) into the planning context instead of asking again.
                answered = "\n".join(
                    f"{turn.q.strip()} {turn.a.strip()}".strip()
                    for turn in clarify_history
                    if turn.a.strip()
                )
                if answered:
                    planning_goal = f"{goal}\n\n{answered}"
            project_plan = plan_goal(planning_goal, llm=llm)
            spec = refined_spec_from_plan(project_plan, goal)
        if plan_first:
            collab_store.update_board_task(
                board_task_id,
                {
                    "title": spec.title,
                    "description": spec.description,
                    "discipline": spec.suggested_discipline,
                    "priority": spec.suggested_priority,
                },
            )
            return

        routing_mode = quick_project_routing_mode()
        if routing_mode != "off" and project_plan is not None:
            try:
                hierarchy = ProjectStoreHierarchyDAL(store)
                route_decision = route_project(
                    project_plan,
                    hierarchy.list_projects(),
                    require_root_dir=True,
                )
                LOG.info(
                    "quick project routing [%s]: decision=%s parent=%s root=%s "
                    "confidence=%s reason=%s",
                    routing_mode,
                    route_decision.decision,
                    route_decision.parent_project_id,
                    route_decision.root_dirs[0] if route_decision.root_dirs else None,
                    route_decision.confidence,
                    route_decision.reason,
                )
                if (
                    routing_mode == "enforce"
                    and project_id is None
                    and route_decision.decision == "existing"
                    and route_decision.parent_project_id
                    and route_decision.root_dirs
                ):
                    # Registry HIT: dispatch through the matched project's own
                    # root_dirs[0] — dispatch_spec resolves the working dir from
                    # project_id (_resolve_project_dir), so setting it here BEFORE
                    # dispatch_spec is what makes this "before workspace
                    # resolution". A genuine MISS (decision == "new") leaves
                    # project_id untouched, so dispatch_spec's existing fallback
                    # creates a scratch project (root_dirs already assigned) —
                    # never explicitly duplicated here.
                    project_id = route_decision.parent_project_id
            except Exception:
                LOG.warning(
                    "quick project routing failed for board task %s", board_task_id, exc_info=True
                )

        if routing_mode == "enforce" and project_plan is not None:
            # ENFORCE ONLY, and the gate is the whole point.
            #
            # This block used to sit under `routing_mode != "off"`, so simply
            # turning the flag to shadow made every quick dispatch run
            # build_preflight — which classifies with apply=True
            # (`orgdims/classify.py` PERSISTS it) and writes `preflight_json`,
            # which `swarm/spawn.py` then reads to reorder the skill hits in the
            # brief a worker is actually given. Shadow therefore changed both
            # stored state and delivered prompts, contradicting this repo's
            # ratified three-rung convention (FEATURE-FLAGS.md: "shadow computes
            # and records the decision but applies nothing") and this module's
            # own docstring. It was caught in review as Lane A followup D1
            # ("shadow changes execution; gate on enforce",
            # devtasks/phase1-ledger.md) and the launch-path default landing on
            # shadow is what made it live.
            #
            # Preflight must still run BEFORE dispatch (not after, as an earlier
            # attempt had it) — it needs no id dispatch_spec itself produces.
            try:
                cleaned_title, wide, pinned_formation = _parse_goal_prefixes(spec.title)
                from omniagentos.intake.preflight import build_preflight

                preflight = build_preflight(
                    db_path=cast(SqliteStore, store)._db_path,
                    task_id=board_task_id,
                    title=cleaned_title,
                    description=spec.description,
                    discipline=spec.suggested_discipline,
                    priority=spec.suggested_priority,
                    wide=wide,
                    pinned_formation=pinned_formation,
                )
                import json

                collab_store._store._connection.execute(
                    "UPDATE board_tasks SET preflight_json = ? WHERE id = ?",
                    (json.dumps(preflight), board_task_id),
                )
            except Exception:
                LOG.warning(
                    "build_preflight failed for quick work %s", board_task_id, exc_info=True
                )

        dispatch_spec(
            store,
            collab_store,
            policy_cfg,
            spec,
            project_id=project_id,
            execute=execute,
            priority=priority,
            pins=_pins_for_speed(speed, _QUICK_EXECUTOR_PINS[executor]),
            async_orchestrate=True,
            board_task_id=board_task_id,
            orchestration_run_id=run_id,
            lane=None if lane == "auto" else lane,  # explicit lane always wins
            category=category,
            speed=speed,
        )
    except Exception as exc:  # noqa: BLE001 -- surface a planning failure on its board card.
        LOG.exception("quick intake failed for board task %s", board_task_id)
        if run_id is not None:
            lifecycle = OrchestrationsDal(_sqlite_db_path(store))
            try:
                lifecycle.set_status(run_id, "failed", error=str(exc) or "unknown error")
            finally:
                lifecycle.close()
        collab_store.update_board_task(
            board_task_id,
            {"status": "blocked", "description": f"Planning failed: {str(exc) or 'unknown error'}"},
        )
        _emit_board_event(collab_store, board_task_id, run_id=run_id)


def _run_quick_session(
    goal: str,
    *,
    executor: str,
    target_write_root: str | None,
    project_id: str | None,
    board_task_id: str,
    run_id: str | None,
    store: Any,
    collab_store: Any,
    policy_cfg: Any,
    category: str | None = None,
    speed: str | None = None,
) -> None:
    """Superfast lane: spawn a live session straight from the goal, NO planning.

    For a small, bounded task ("make a folder on my desktop called tiger") the heavy
    Fable planning pass is pure latency + cost -- this dispatches ``execute="session"``
    immediately, granting the vetted ``target_write_root`` (e.g. ~/Desktop) as a
    sandbox write root and marking the session hands-off (auto-approve safe /
    escalate money+delete). Runs in a BackgroundTask, so the HTTP request that started
    it already returned. An explicit dial ``speed`` (F2) rides into
    ``dispatch_spec`` so the spawned session's task records it durably."""
    try:
        title = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal.strip())[:120]
        spec = RefinedSpec(title=title or "Quick task", description=goal.strip()).normalized()
        model = "opus" if executor == "opus" else None
        dispatch_spec(
            store,
            collab_store,
            policy_cfg,
            spec,
            project_id=project_id,
            execute="session",
            model=model,
            fast=True,
            target_write_root=target_write_root,
            board_task_id=board_task_id,
            orchestration_run_id=run_id,
            lane="fast",
            category=category,
            speed=speed,
        )
    except Exception as exc:  # noqa: BLE001 -- surface a spawn failure on its board card.
        LOG.exception("quick session failed for board task %s", board_task_id)
        collab_store.update_board_task(
            board_task_id,
            {"status": "blocked", "description": f"Failed to start: {str(exc) or 'unknown error'}"},
        )


def _run_quick_swarm(
    goal: str,
    *,
    project_id: str | None,
    board_task_id: str,
    run_id: str,
    store: Any,
    collab_store: Any,
    policy_cfg: Any,
    priority: str = "balanced",
    speed: str | None = None,
) -> None:
    """WP10 passthrough: dispatch the goal into Swarm Mode away from the request.

    Runs in a BackgroundTask (planning can take a while at high Fable effort).
    ``dispatch_spec(execute="swarm")`` owns everything from here: bundle
    detection (N unrelated asks -> N independent board cards), fleet admission
    (over-cap runs provision ``queued``, never block), flag-gated activation
    (``OMNIAGENTOS_SWARM_EXECUTE`` off = provision-only), and archiving the
    instant placeholder card once real bundle/root cards exist. A single SOLO
    plan falls through to the existing orchestrate path -- ``async_orchestrate``
    + the placeholder/run ids are threaded so that fall-through reuses THIS
    card and run id exactly like the planned lane would have.
    """
    try:
        title = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal.strip())[:120]
        spec = RefinedSpec(title=title or "Swarm run", description=goal.strip()).normalized()
        dispatch_spec(
            store,
            collab_store,
            policy_cfg,
            spec,
            project_id=project_id,
            execute="swarm",
            priority=priority,
            pins=_pins_for_speed(speed, None),
            async_orchestrate=True,
            board_task_id=board_task_id,
            orchestration_run_id=run_id,
            speed=speed,
        )
    except Exception as exc:  # noqa: BLE001 -- surface a dispatch failure on its board card.
        LOG.exception("quick swarm dispatch failed for board task %s", board_task_id)
        collab_store.update_board_task(
            board_task_id,
            {
                "status": "blocked",
                "description": f"Swarm dispatch failed: {str(exc) or 'unknown error'}",
            },
        )


@router.post("/intake/quick", status_code=201)
def quick_dispatch(
    body: QuickRequest,
    background_tasks: BackgroundTasks,
    store: StoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
    planner_llm: PlannerLLMDep,
    router_llm: RouterLLMDep,
    clarify_llm: ClarifyLLMDep,
) -> dict[str, Any]:
    """Plan a goal and immediately hand it to the Orchestrator.

    This is intentionally separate from the dialog's inspect-and-confirm plan job:
    the normal cockpit action has no clarify or confirmation branch.  A plan-first
    request uses the same planner but creates a non-claimable pending board card.
    """
    goal = body.goal.strip()
    # D12 force hatch: a leading "solo:" / "swarm:" in the brief outranks any
    # body execute (most-explicit wins — it sits in the user's own text, not
    # an automation default).
    goal, forced_execute = _parse_goal_execute_prefix(goal)
    execute = forced_execute or body.execute
    executor = body.executor.strip().lower()
    priority = body.priority.strip().lower()
    speed = body.speed
    if not goal:
        fail(400, "validation", "goal is required")
    if executor not in _QUICK_EXECUTOR_PINS:
        fail(400, "validation", "invalid executor", {"executor": body.executor})
    if priority not in _QUICK_PRIORITIES:
        fail(400, "validation", "invalid priority", {"priority": body.priority})
    # D10 Mode dial: a non-default speed wins over the legacy priority field;
    # speed="auto" keeps a legacy priority untouched (automation backcompat).
    priority = _resolve_speed_priority(speed, priority)

    # Determine the SUPERFAST lane up front (also used by the fast-lane branch
    # below) — Phase 1 registry routing never applies to it: a bounded,
    # no-planning session spawn has no plan to route.
    is_fast_lane = body.lane != "longhaul" and (
        body.lane == "fast" or (speed != "ultra" and classify_task_speed(goal) == "fast")
    )

    # Phase 1 (A1, OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE): "enforce" spends
    # its ONE clarify round HERE, synchronously, before any board card exists —
    # a cheap router-only probe (no full Fable planning pass) decides whether
    # the goal is too ambiguous to auto-route. "off"/"shadow" never reach this
    # branch: shadow must stay non-invasive (compute + log only, see
    # _run_quick_dispatch), off is legacy-unchanged. Only the FIRST turn (no
    # prior clarify history) is eligible — once the operator has answered,
    # quick_dispatch proceeds on its best judgment below (folded into planning
    # via `clarify_history`), never asking a second time.
    if (
        quick_project_routing_mode() == "enforce"
        and not body.plan
        and not is_fast_lane
        and execute != "swarm"
        and not body.history
    ):
        try:
            hierarchy = ProjectStoreHierarchyDAL(store)
            probe_route = route_project(
                _quick_route_probe_plan(goal),
                hierarchy.list_projects(),
                llm=router_llm,
                require_root_dir=True,
            )
        except Exception:
            LOG.exception("quick intake: low-confidence route probe failed")
            probe_route = None
        if (
            probe_route is not None
            and probe_route.confidence is not None
            and probe_route.confidence < _QUICK_ROUTING_LOW_CONFIDENCE
        ):
            clarify_res = clarify_intake(goal, [], llm=clarify_llm)
            return clarify_res.model_dump_public()

    if body.plan:
        board_task = create_pending_plan_card(
            collab_store,
            RefinedSpec(title=goal, description="Planning the work…"),
        )
        # D10 (F1): persist the dial on the pending card so any approval-time
        # dispatch honors it — Plan-first + Single Task must NEVER auto-swarm
        # at approval. Additive JSON on the existing longhaul_json column; a
        # legacy payload (no execute, no speed) writes nothing.
        if execute is not None or speed is not None:
            persist_card_intake_directives(
                store,
                str(board_task["id"]),
                execute=execute,
                speed=speed,
            )
        background_tasks.add_task(
            _run_quick_dispatch,
            goal,
            executor=executor,
            priority=priority,
            plan_first=True,
            project_id=body.project_id,
            board_task_id=str(board_task["id"]),
            run_id=None,
            store=store,
            collab_store=collab_store,
            policy_cfg=policy_cfg,
            planner_llm=planner_llm,
            speed=speed,
        )
        return {
            "board_task_id": board_task["id"],
            "run_id": None,
            "status": "pending",
            "message": "Plan is ready on the board ↓",
        }

    # WP10 passthrough: execute="swarm" hands the goal to Swarm Mode dispatch.
    # Gated on lane == "auto" on purpose -- an EXPLICIT fast/longhaul lane keeps
    # absolute priority and is never intercepted by swarm planning. Mirrors the
    # fast lane's shape: instant placeholder card + a correlation run id with no
    # orchestrations row (dispatch archives the placeholder once real bundle/
    # root cards exist; a solo fall-through reuses it as the planned lane would).
    if execute == "swarm" and body.lane == "auto":
        run_id = new_id("orch")
        board_task = create_queued_goal_card(collab_store, goal, run_id)
        background_tasks.add_task(
            _run_quick_swarm,
            goal,
            project_id=body.project_id,
            board_task_id=str(board_task["id"]),
            run_id=run_id,
            store=store,
            collab_store=collab_store,
            policy_cfg=policy_cfg,
            priority=priority,
            speed=speed,
        )
        return {
            "board_task_id": board_task["id"],
            "run_id": run_id,
            "status": "queued",
            "lane": "swarm",
            "message": "On it — swarm mode: planning the DAG, cards land on the board ↓",
        }

    # Superfast lane: a small, bounded task ("make a folder on my desktop called
    # tiger") spawns a live session IMMEDIATELY with no planning pass -- so it is
    # seconds not minutes, and cheap. Anything multi-step / batch / research falls to
    # the planned orchestrate lane below (Fable plans first, then spawns).
    # D10 (F2): an EXPLICIT speed='ultra' SKIPS the fastlane heuristic entirely
    # — Ultra Code chose depth over latency, so even a fastlane-shaped goal
    # takes the planned lane below with ultra semantics. An explicit
    # lane="fast" keeps absolute priority as ever; speed='fast', explicit
    # 'auto', and an omitted speed all keep today's heuristic.
    if body.lane != "longhaul" and (
        body.lane == "fast" or (speed != "ultra" and classify_task_speed(goal) == "fast")
    ):
        run_id = new_id("orch")
        board_task = create_queued_goal_card(collab_store, goal, run_id)
        target = parse_target_location(goal)
        background_tasks.add_task(
            _run_quick_session,
            goal,
            executor=executor,
            target_write_root=target,
            project_id=body.project_id,
            board_task_id=str(board_task["id"]),
            run_id=run_id,
            store=store,
            collab_store=collab_store,
            policy_cfg=policy_cfg,
            category=body.category,
            speed=speed,
        )
        where = f" → {target}" if target else ""
        return {
            "board_task_id": board_task["id"],
            "run_id": run_id,
            "status": "queued",
            "lane": "fast",
            "message": f"On it — fast lane{where} ↓",
        }

    run_id = new_id("orch")
    board_task = create_queued_goal_card(collab_store, goal, run_id)
    lifecycle = OrchestrationsDal(_sqlite_db_path(store))
    try:
        lifecycle.create(
            run_id,
            board_task_id=str(board_task["id"]),
            working_dir="",
        )
        lifecycle.set_status(run_id, "queued", stage="planning")
    finally:
        lifecycle.close()
    background_tasks.add_task(
        _run_quick_dispatch,
        goal,
        executor=executor,
        priority=priority,
        plan_first=False,
        project_id=body.project_id,
        board_task_id=str(board_task["id"]),
        run_id=run_id,
        store=store,
        collab_store=collab_store,
        policy_cfg=policy_cfg,
        planner_llm=planner_llm,
        lane=body.lane,
        category=body.category,
        speed=speed,
        # D10: Single Task rides through dispatch_spec as execute="single" —
        # orchestrate with the auto solo-vs-swarm upgrade HARD-suppressed.
        execute="single" if execute == "single" else "orchestrate",
        clarify_history=body.history or None,
    )
    if body.lane == "longhaul":
        return {
            "board_task_id": board_task["id"],
            "run_id": run_id,
            "status": "queued",
            "lane": "longhaul",
            "message": "On it — long-haul lane: one coder end-to-end, steer from the task card ↓",
        }
    return {
        "board_task_id": board_task["id"],
        "run_id": run_id,
        "status": "queued",
        "lane": "planned",
        "message": "On it — planning, then tracking on the board ↓",
    }


@router.post("/intake/plan", status_code=202)
def plan(
    body: PlanRequest,
    background_tasks: BackgroundTasks,
    plans: PlanStoreDep,
    hierarchy: HierarchyDep,
    planner_llm: PlannerLLMDep,
    router_llm: RouterLLMDep,
) -> dict[str, Any]:
    """Kick off Fable PLAN + ROUTE as a background job and return immediately.

    PLAN (Fable high/max) -> ROUTE (Fable new-vs-existing) run in the background;
    this endpoint only registers the job. Poll ``GET /intake/plan/{job_id}`` for the
    preview, then ``POST /intake/plan/{job_id}/confirm`` to actually create the
    project (+sub-projects/tasks) and dispatch. ``/intake/dispatch`` remains the
    one-shot path for a single bounded request that needs no decomposition.
    """
    if not body.goal or not body.goal.strip():
        fail(400, "validation", "goal is required")
    job_id = _plan_job_create(body.goal, body.harness, body.execute, body.speed)
    # The request's own store rides into the background task. SqliteStore hands
    # out one connection per thread, so the background thread gets its own
    # handle from the same (already-migrated) store — no second construction.
    background_tasks.add_task(
        _run_plan_job,
        job_id,
        body.goal,
        hierarchy,
        planner_llm,
        router_llm,
        body.speed,
        plans,
    )
    return {"job_id": job_id, "status": "running"}


# Durable plan status -> the job status the poll/confirm contract speaks.
# 'approved' is the row a confirm leaves behind, so after a restart the route
# reports 'confirmed' rather than pretending the plan is still awaiting approval.
_JOB_STATUS_FOR_PLAN_STATUS: dict[str, str] = {
    "draft": "running",
    "ready": "ready",
    "approved": "confirmed",
    "rejected": "error",
    "superseded": "error",
}


def _json_or_none(raw: Any) -> Any:
    import json

    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        LOG.error("unparseable JSON column on a durable plan row: %r", raw)
        return None


def _job_from_plan_row(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the in-memory job shape from a durable plan row.

    Everything confirm reads is restored — goal, harness, execute, speed, plan
    AND route. A rehydrated job with a null route was the reason a post-restart
    confirm could only 404 or 500.
    """
    plan_status = str(row.get("status") or "draft")
    reason = row.get("reason")
    return {
        "status": _JOB_STATUS_FOR_PLAN_STATUS.get(plan_status, "running"),
        "goal": row.get("goal"),
        "harness": row.get("harness"),
        "execute": row.get("execute_mode"),
        "speed": row.get("speed"),
        "plan": _json_or_none(row.get("plan_json")),
        "route": _json_or_none(row.get("route_json")),
        "route_target_name": row.get("route_target_name"),
        "error": (
            f"plan {plan_status}: {reason}" if plan_status in ("rejected", "superseded") else None
        ),
        # Re-provisioning is NOT idempotent across a restart, so the cached
        # result is deliberately not reconstructed: confirm refuses an
        # already-approved plan instead of dispatching it a second time.
        "provision_result": None,
        "durable": True,
        "execution": _json_or_none(row.get("execution_metadata")),
        "created_at": time.monotonic(),
    }


def _plan_job_get_with_fallback(job_id: str, plans: PlansStore | None) -> dict[str, Any] | None:
    """The plan job from the process cache, or rehydrated from its durable row.

    This is the read that makes a plan survive a restart. It is used by the poll
    route AND by confirm — a plan the operator can look at but cannot approve is
    still a plan that died.
    """
    job = _plan_job_get(job_id)
    if job is not None:
        return job
    if plans is None:
        return None
    try:
        row = plans.get_plan(job_id)
    except Exception:
        # A broken durable read must not be reported as "no such plan": that is
        # how a storage fault becomes a phantom 404.
        LOG.error("durable plan read FAILED for %s", job_id, exc_info=True)
        raise
    if row is None:
        return None
    job = _job_from_plan_row(row)
    _plan_job_put(job_id, job)
    return job


@router.get("/intake/plan/{job_id}")
def plan_status(job_id: str, plans: PlanStoreDep) -> dict[str, Any]:
    """Poll a plan job: ``running`` -> ``ready`` (plan + route preview) or ``error``."""
    job = _plan_job_get_with_fallback(job_id, plans)
    if job is None:
        fail(404, "not_found", "plan job not found", {"job_id": job_id})
    return {
        "job_id": job_id,
        "status": job["status"],
        "plan": job["plan"],
        "route": job["route"],
        "route_target_name": job["route_target_name"],
        "error": job["error"],
        # Whether this plan is written down. False means a restart loses it —
        # the operator should be told, not shown a plan that silently evaporates.
        "durable": bool(job.get("durable")),
        # The approved execution parameters (owned paths, formation, the cards
        # and runs the approval produced), once the plan has been approved.
        "execution": job.get("execution"),
    }


def _confirm_preflight_key(task_res: dict[str, Any]) -> str:
    """The id the confirm loop keys a dispatched entry (and its preflight) by.

    This is the CONTROL-PLANE task id (``tasks.id``). ``dispatch_spec`` returns
    that AND the board card id (``task_res["board_task"]["id"]``) and they are
    different rows — only the card id may be written to ``plans.board_task_id``,
    whose foreign key points at ``board_tasks``.
    """
    return str(task_res.get("task_id") or (task_res.get("board_task") or {}).get("id") or "")


def _plan_execution_metadata(
    *,
    result: Any,
    preflights: dict[str, dict[str, Any]],
    harness: str | None,
    execute: str | None,
    speed: str | None,
) -> dict[str, Any]:
    """What this approval actually bought: every card, its run, and its preflight.

    Aggregated across ALL dispatched cards on purpose — a per-card write would
    leave an N-card plan recording only whichever card happened to be last.
    """
    cards: list[dict[str, Any]] = []
    owned: set[str] = set()
    for task_res in result.dispatched:
        board_task = task_res.get("board_task") or {}
        preflight = preflights.get(_confirm_preflight_key(task_res), {})
        paths = [str(path) for path in (preflight.get("owned_paths") or [])]
        owned.update(paths)
        cards.append(
            {
                "board_task_id": board_task.get("id"),
                "task_id": task_res.get("task_id"),
                "run_id": task_res.get("run_id"),
                "project_id": task_res.get("project_id"),
                "title": board_task.get("title"),
                "formation_id": preflight.get("formation_id"),
                "confidence": preflight.get("confidence"),
                "risk_class": preflight.get("risk_class"),
                "parallelism": preflight.get("parallelism"),
                "owned_paths": paths,
            }
        )
    return {
        "root_project_id": result.root_project_id,
        "task_count": result.task_count,
        "harness": harness,
        "execute": execute,
        "speed": speed,
        "owned_paths": sorted(owned),
        "cards": cards,
    }


def _record_plan_approval(
    plans: PlansStore | None,
    job_id: str,
    *,
    job: dict[str, Any],
    execution: dict[str, Any],
    decided_by: str,
) -> bool:
    """Record the approval on the durable plan row. Returns whether it landed.

    This is the row that answers "what did the operator approve, and who
    approved it". It is written once, after provisioning, and its outcome is
    reported to the caller — the previous version swallowed every failure at
    DEBUG and returned 201, so an approval that never touched the database was
    indistinguishable from one that did.
    """
    if plans is None:
        LOG.error("no durable plans store: approval of %s was NOT recorded", job_id)
        return False

    import json

    cards = execution.get("cards") or []
    # Only bind board_task_id when the approval produced exactly ONE card:
    # with several, no single card is "the" card and the column would assert a
    # primary that does not exist. Every card id is in execution_metadata.cards.
    sole_card = (
        str(cards[0]["board_task_id"])
        if len(cards) == 1 and cards[0].get("board_task_id")
        else None
    )
    now = utc_now_iso()

    def prepare(row: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "status": "approved",
            "decided_by": decided_by,
            "decided_at": now,
            "execution_metadata": json.dumps(execution),
        }
        if sole_card and not row.get("board_task_id"):
            patch["board_task_id"] = sole_card
        return patch

    try:
        if plans.get_plan(job_id) is None:
            # The planning-time write never landed (store unavailable, eviction).
            # The approval is the record that matters most, so seed the row here
            # rather than losing that too; the approval patch below then lands on
            # it through the ordinary validated path.
            LOG.error("durable plan row missing for %s at approval time; creating it now", job_id)
            plans.create_plan(plan_id=job_id, version=1, status="ready", **_plan_row_payload(job))
        updated = plans.update_plan(job_id, prepare)
    except Exception:
        LOG.error("durable approval write FAILED for plan %s", job_id, exc_info=True)
        return False
    if updated is None:
        LOG.error("durable approval write for plan %s matched no row", job_id)
        return False
    return True


@router.post("/intake/plan/{job_id}/confirm", status_code=201)
def plan_confirm(
    job_id: str,
    body: PlanConfirmRequest,
    store: StoreDep,
    plans: PlanStoreDep,
    collab_store: CollabStoreDep,
    policy_cfg: PolicyDep,
    hierarchy: HierarchyDep,
) -> dict[str, Any]:
    """Materialise a ``ready`` plan job: create the hierarchy, then dispatch.

    Reads through the SAME durable fallback as the poll route: a plan the
    operator can still see after a restart must also be approvable, or the
    durability is cosmetic.

    Idempotent per job id — confirming an already-``confirmed`` job returns the
    cached result instead of re-provisioning, so a double-click or a client retry
    after a dropped response can never create the project twice. Across a
    restart the cached result is gone, so an already-approved plan is REFUSED
    (409) rather than provisioned a second time.
    """
    job = _plan_job_get_with_fallback(job_id, plans)
    if job is None:
        fail(404, "not_found", "plan job not found", {"job_id": job_id})
    if job["status"] == "confirmed":
        if job["provision_result"] is not None:
            return job["provision_result"]
        fail(
            409,
            "already_confirmed",
            "plan was already approved and dispatched",
            {"job_id": job_id, "status": "confirmed"},
        )
    if job["status"] == "error":
        fail(409, "plan_failed", job["error"] or "planning failed")
    if job["status"] != "ready":
        fail(409, "not_ready", "plan is not ready yet", {"status": job["status"]})

    plan = ProjectPlan.model_validate(job["plan"])
    route = RouteDecision.model_validate(job["route"])

    override = (body.project_override or "auto").strip()
    if override and override != "auto":
        if override.startswith("new:"):
            name = override[len("new:") :].strip() or plan.project_name
            route = RouteDecision(
                decision="new",
                project_name=name,
                reason="operator chose to create a new project",
            )
        else:
            valid_ids = {str(p.get("id")) for p in hierarchy.list_projects() if p.get("id")}
            if override not in valid_ids:
                fail(
                    400,
                    "validation",
                    f"unknown project {override!r}",
                    {"project_override": override},
                )
            route = RouteDecision(
                decision="existing",
                parent_project_id=override,
                project_name=plan.project_name,
                reason="operator chose an existing project",
            )

    # provision_plan (planner.py, frozen) names the created root project from
    # plan.project_name -- it never reads route.project_name. Keep them in sync for
    # a "new" decision so the project actually created matches exactly what was
    # previewed ("Fable will create a new project 'X'"), whether X is Fable's own
    # router suggestion or an operator's "new:<name>" override.
    if route.decision == "new" and route.project_name and route.project_name != plan.project_name:
        plan = plan.model_copy(update={"project_name": route.project_name})

    harness = body.harness if body.harness is not None else job["harness"]
    execute = body.execute if body.execute is not None else job["execute"]
    # D10 (F1): the dial's speed was persisted on the plan job at kickoff; an
    # explicit confirm-time speed overrides it. Mapping onto priority/pins
    # happens HERE at the API edge, exactly like /intake/quick and
    # /intake/dispatch; a None speed (legacy) passes nothing new through.
    speed = body.speed if body.speed is not None else job.get("speed")

    result = provision_plan(
        store,
        collab_store,
        policy_cfg,
        plan,
        route,
        hierarchy,
        harness=harness,
        execute=execute,
        speed=speed,
        priority=(_resolve_speed_priority(speed, "balanced") if speed is not None else None),
        pins=_pins_for_speed(speed, None),
    )

    # Chat v2 (§3.9): the confirmed plan payload rides onto each created card
    # as org_json.planner_brief so the drawer's Plan tab renders the planner's
    # own brief instead of a JSON dump.
    planner_brief = _planner_brief_markdown(plan, job)

    # Per-card preflights, kept so the approval binding can be written ONCE for
    # the whole plan. Writing it inside the loop made an N-card plan overwrite
    # the record N times, last card wins.
    preflights: dict[str, dict[str, Any]] = {}

    for task_res in result.dispatched:
        task_id = task_res.get("task_id") or task_res.get("board_task", {}).get("id")
        t_proj_id = task_res.get("project_id")
        t_run_id = task_res.get("run_id")
        t_title = task_res.get("board_task", {}).get("title") or ""
        t_desc = task_res.get("board_task", {}).get("description") or ""

        if task_id:
            try:
                # §3.9 planner_brief write (org_json merge, one per card).
                import json

                org_row = collab_store._store._connection.execute(
                    "SELECT org_json FROM board_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                org_env = json.loads(org_row["org_json"]) if org_row and org_row["org_json"] else {}
                if isinstance(org_env, dict):
                    org_env["planner_brief"] = planner_brief
                    collab_store._store._connection.execute(
                        "UPDATE board_tasks SET org_json = ? WHERE id = ?",
                        (json.dumps(org_env), task_id),
                    )
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug(
                    "planner_brief write skipped for %s", task_id, exc_info=True
                )
            try:
                cleaned_title, wide, pinned_formation = _parse_goal_prefixes(t_title)
                from omniagentos.intake.preflight import build_preflight

                preflight = build_preflight(
                    db_path=cast(SqliteStore, store)._db_path,
                    task_id=task_id,
                    title=cleaned_title,
                    description=t_desc,
                    discipline=task_res.get("board_task", {}).get("discipline"),
                    priority=task_res.get("board_task", {}).get("priority"),
                    wide=wide,
                    pinned_formation=pinned_formation,
                )

                import json

                collab_store._store._connection.execute(
                    "UPDATE board_tasks SET preflight_json = ? WHERE id = ?",
                    (json.dumps(preflight), task_id),
                )
                # The preflight is what this card ACTUALLY executes with
                # (owned paths, formation, confidence) — kept for the one
                # approval binding written after the loop.
                preflights[_confirm_preflight_key(task_res)] = preflight

                from omniagentos.formation.selector import _all, topology_for_formation
                from omniagentos.formation.telemetry import record_selection

                form = _all().get(preflight["formation_id"])
                if form is not None:
                    record_selection(
                        collab_store._store._connection,
                        task_id=task_id,
                        goal=t_desc or cleaned_title,
                        arm="formation",
                        formation_id=form.id,
                        confidence=preflight["confidence"],
                        low_confidence=bool(preflight["confidence"] < 0.5),
                        topology=topology_for_formation(form.id),
                        implementers=list(form.implementers or []),
                        reviewer=form.reviewer,
                        planner=form.planner,
                        mechanical_gate=bool(form.mechanical_gate),
                        models={
                            "implementers": list(form.implementers or []),
                            "reviewer": form.reviewer,
                            "planner": form.planner,
                        },
                        outcome="predicted",
                        source="production",
                    )

                if t_proj_id and t_run_id:
                    for path in preflight["owned_paths"]:
                        collab_store._store._connection.execute(
                            "INSERT INTO project_scope_requests "
                            "(id, project_id, run_id, scope_kind, scope_value, action_class, "
                            "decision, status, reason, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                new_id("psr"),
                                t_proj_id,
                                t_run_id,
                                "dir",
                                path,
                                "file_write",
                                "planner_declared",
                                "granted",
                                "preflight owned path",
                                utc_now_iso(),
                            ),
                        )
                task_res["preflight"] = preflight
            except Exception:
                logging.getLogger(__name__).warning(
                    "Preflight step failed during plan confirm", exc_info=True
                )

    # ONE approval binding for the whole plan, written after every card exists.
    execution = _plan_execution_metadata(
        result=result,
        preflights=preflights,
        harness=harness,
        execute=execute,
        speed=speed,
    )
    durable_approval = _record_plan_approval(
        plans,
        job_id,
        job=job,
        execution=execution,
        decided_by=_DEFAULT_DECIDER,
    )

    response = {
        "root_project_id": result.root_project_id,
        "route": result.route.model_dump(),
        "sub_project_ids": result.sub_project_ids,
        "task_count": result.task_count,
        "dispatched": result.dispatched,
        # The cards and runs are already live by the time the approval row is
        # written, so a failed write cannot roll the dispatch back — but it is
        # NEVER reported as success. False here means: this ran, and the
        # approval was not written down.
        "durable_approval": durable_approval,
        "execution": execution,
    }
    _plan_job_update(
        job_id,
        status="confirmed",
        provision_result=response,
        execution=execution,
        durable=durable_approval,
    )
    return response


def _parse_statuses(raw: str | None) -> list[str] | None:
    """CSV ``status`` filter → the list the store binds into ``status IN (…)``.

    ``None``/blank means "no filter" (today's behaviour). Unknown status names
    are refused rather than silently matching nothing: a typo that returns an
    empty board reads exactly like an empty board.
    """
    if raw is None:
        return None
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    if not wanted:
        return None
    known = {member.value for member in BoardTaskStatus}
    unknown = [item for item in wanted if item not in known]
    if unknown:
        fail(
            400,
            "validation",
            "unknown board status",
            {"unknown": unknown, "known": sorted(known)},
        )
    return list(dict.fromkeys(wanted))


def _validate_updated_after(raw: str | None) -> str | None:
    """Validate the ISO-8601 ``updated_after`` cursor without rewriting it.

    The stored column is an ISO-8601 UTC string compared lexicographically, so
    the caller's own text is what must be bound — normalising it here would
    silently shift the cursor. This only refuses text that is not a timestamp.
    """
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(400, "validation", "updated_after must be an ISO-8601 timestamp", {"value": value})
    return value


# EVERY table :func:`omniagentos.intake.service.reconcile_board` reads, paired
# with the cheapest query whose value moves when the projection that table feeds
# moves. The board's OWN rows are stamped separately (``board_change_stamp``);
# this is the "anything the reconciler reads" half, and it has to be complete —
# a source missing from here is a source that can change a card's payload while
# the ETag says nothing moved, i.e. a stale 304.
#
# Every entry has a named case in ``tests/intake/test_board_etag_sources.py``;
# ``test_every_stamped_source_has_a_coverage_case`` fails if a source is added
# here without one, so a new reconciler input cannot be stamped untested (nor
# tested unstamped).
_BOARD_STAMP_SOURCES: tuple[tuple[str, str], ...] = (
    # Liveness backstop: nearly every write in the system also lands an event.
    # Append-only with an INTEGER PRIMARY KEY, so MAX(id) is an index lookup and
    # COUNT(*) would buy nothing on the system's largest table.
    ("events", "SELECT MAX(id) FROM events"),
    # work.state / agent / cost / error for run-linked cards (``_run_map``).
    ("runs", "SELECT COUNT(*), MAX(updated_at) FROM runs"),
    # work.* for session-linked cards (``sessions_dal.get_sessions_by_ids``).
    ("sessions", "SELECT COUNT(*), MAX(updated_at) FROM sessions"),
    # run_progress / checklist (``_step_count_map``). A step's status can flip
    # without touching a timestamp, so the stamp carries the SAME (done, total)
    # aggregate the board projects rather than a MAX of anything.
    (
        "steps",
        "SELECT COUNT(*), MAX(id), "
        "SUM(CASE WHEN status IN ('completed', 'skipped') THEN 1 ELSE 0 END) FROM steps",
    ),
    # pending_approval (``_pending_approval_map``): what matters is how many are
    # pending and when the newest arrived / the last decision landed.
    (
        "approvals",
        "SELECT COUNT(*), MAX(created_at), MAX(decided_at), "
        "SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) FROM approvals",
    ),
    # chat_origin AND the companion-card exclusion (``_chat_origin_map`` plus
    # the route's own companion query) — a chat retitled or deleted changes the
    # feed's CONTENTS, not just one card's decoration.
    ("chats", "SELECT COUNT(*), MAX(updated_at) FROM chats"),
    # work.* for orchestration-linked cards.
    ("orchestrations", "SELECT COUNT(*), MAX(updated_at) FROM orchestrations"),
    # blocked_reason, both attempt ledgers (``_blocked_reason_map``), plus
    # attempt_count off ``task_sessions``. An attempt is INSERTed live and later
    # UPDATEd with its ending, so both the row count and the ended/reason
    # aggregates are needed. (A ``detail`` edited in place with no other change
    # is not seen — no writer does that, and the cost of seeing it is a full
    # scan of a free-text column.)
    (
        "swarm_attempts",
        "SELECT COUNT(*), MAX(seq), MAX(ended_at), COUNT(end_reason) FROM swarm_attempts",
    ),
    (
        "task_sessions",
        "SELECT COUNT(*), MAX(seq), MAX(ended_at), COUNT(end_reason) FROM task_sessions",
    ),
    # card.category (name/colour) for cards with a category_id.
    ("task_categories", "SELECT COUNT(*), MAX(updated_at) FROM task_categories"),
    # org.organization_context.company_* (``enrich_company_from_projects``).
    # Neither dimension table has an ``updated_at`` and a RE-classification is
    # an UPDATE of one column, so no aggregate can see it. Both are small (one
    # row per project / per company, tens of rows) so the stamp reads the two
    # projected columns whole: exact, and bounded by the org dimensions rather
    # than by the board.
    ("projects", "SELECT id, org_company_id FROM projects ORDER BY id"),
    ("org_companies", "SELECT id, slug FROM org_companies ORDER BY id"),
)

# Non-printing separators: stamp values are ids and timestamps, and a separator
# that could appear INSIDE a value would let two different states hash equal.
_STAMP_VALUE_SEP = "\x1f"
_STAMP_ROW_SEP = "\x1e"


def _board_stamp_part(connection: Any, sql: str) -> str:
    """One source's contribution: every value of every row it returns, in order."""
    return _STAMP_ROW_SEP.join(
        _STAMP_VALUE_SEP.join("" if value is None else str(value) for value in tuple(row))
        for row in connection.execute(sql).fetchall()
    )


def _board_etag(
    store: StoreDep,
    collab_store: CollabStoreDep,
    archived: int,
    selector: tuple[Any, ...],
) -> str:
    """A weak-but-honest board ETag: board write stamp + every reconciler input.

    Two facts decide whether a board feed can have changed:

    * the board's own rows (count + newest ``updated_at``), and
    * anything the reconciler READS — a run finishing does not touch
      ``board_tasks`` until this endpoint writes the new column, so a board-only
      stamp would happily serve a 304 while the card's run had already finished.

    The second half is :data:`_BOARD_STAMP_SOURCES`, which enumerates EVERY
    table ``reconcile_board`` (and this route's own companion filter) reads, one
    query each. Completeness is the whole point: the tag is only allowed to say
    "nothing moved" if nothing that feeds a card's payload moved, so a table
    added to the reconciler without being added here is a stale-304 bug, and the
    coverage test named on that constant exists to make that failure loud.

    A missing table (minimal/legacy stores) contributes an empty part rather
    than an exception: no stamp source, no crash, just a tag that cannot see
    that source — the same conservative posture as a missing ``_connection``,
    which returns "" and disables 304s entirely.

    KNOWN BOUND (pre-existing, stated rather than hidden): every timestamp in
    this project is second-resolution (``utc_now_iso``), so a source stamped by
    ``MAX(updated_at)`` cannot distinguish an update that lands in the SAME
    second as the newest write it already saw. Row counts and the id/aggregate
    halves cover inserts and state transitions; an in-place edit inside that
    one-second window stays invisible until the next write anywhere in that
    table. Closing it would need a per-row digest of every card on the board —
    the cost the ETag exists to avoid.

    The stamp is taken BEFORE the reconcile, so a write that lands mid-request
    invalidates the tag it was computed from; the failure mode is a missed 304
    (a full, correct response), never a stale one.
    """
    parts: list[str] = [repr(selector)]
    try:
        count, newest = collab_store.board_change_stamp(archived)
        parts.append(f"{count}:{newest}")
    except Exception:  # noqa: BLE001 -- no stamp, no 304; never a broken board
        return ""
    connection = getattr(store, "_connection", None)
    if connection is None:
        return ""
    for _label, sql in _BOARD_STAMP_SOURCES:
        try:
            parts.append(_board_stamp_part(connection, sql))
        except Exception:  # noqa: BLE001 -- table absent in minimal/legacy stores
            parts.append("")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f'W/"board-{digest}"'


@router.get("/board")
def live_board(
    store: StoreDep,
    collab_store: CollabStoreDep,
    response: Response,
    archived: Annotated[int, Query(ge=0, le=1)] = 0,
    project_id: Annotated[str | None, Query()] = None,
    parent_task_id: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    updated_after: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=2000)] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Any:
    """The live board: every card, with its column reconciled from its linked run.

    By default (archived=0), excludes archived cards. Set archived=1 to view only
    archived cards.

    ``project_id`` filters server-side on the migration-087 column (P0-7);
    pre-087 cards with no run/chat link stay NULL and drop from scoped views.
    ``parent_task_id`` returns the children of one card (org_json parent link),
    INCLUDING chat companions — the drawer's sub-task disclosure.

    Default exclusion (P0-9): only companion cards — those pointed at by a
    chat's ``board_task_id`` — are hidden. Spawned sub-tasks (``origin='chat'``
    but real work) are visible in their project.

    BOUNDING (all optional, all default to off, so a request with no new
    parameter returns exactly the feed it returned before they existed —
    2.66 MB of it on the live board, which is the point of the parameters):

    * ``status`` — CSV of board statuses (``status IN (…)``, pushed to SQL).
    * ``updated_after`` — inclusive ISO-8601 ``updated_at`` cursor for delta
      polls. Inclusive on purpose: re-sending the boundary card is idempotent,
      dropping a card written in the same second is not.
    * ``limit`` — server-side cap, newest card first. Applied in SQL, BEFORE the
      companion/project/parent filters below run in Python — so a limited page
      can return fewer than ``limit`` cards once those filters bite. Stated,
      not hidden: a page that silently back-filled to ``limit`` would have to
      re-query, which is the cost the parameter exists to avoid.

    CONDITIONAL READS: the response carries a weak ``ETag``; sending it back as
    ``If-None-Match`` returns ``304`` with no body when nothing the board reads
    has moved. See :func:`_board_etag` for exactly what "moved" covers.
    """
    statuses = _parse_statuses(status)
    cursor = _validate_updated_after(updated_after)
    etag = _board_etag(
        store,
        collab_store,
        archived,
        (statuses, cursor, limit, project_id, parent_task_id, owner),
    )
    if etag:
        response.headers["ETag"] = etag
        if if_none_match and etag in [part.strip() for part in if_none_match.split(",")]:
            return Response(status_code=304, headers={"ETag": etag})
    tasks = reconcile_board(
        store,
        collab_store,
        archived=archived,
        statuses=statuses,
        updated_after=cursor,
        limit=limit,
    )
    if parent_task_id:
        return [
            task
            for task in tasks
            if isinstance(task.get("org"), dict)
            and task["org"].get("parent_task_id") == parent_task_id
        ]
    companions = {
        str(row["board_task_id"])
        for row in cast(SqliteStore, store)
        ._connection.execute("SELECT board_task_id FROM chats WHERE status != 'deleted'")
        .fetchall()
        if row["board_task_id"]
    }
    visible = [task for task in tasks if str(task.get("id")) not in companions]
    if project_id:
        visible = [task for task in visible if task.get("project_id") == project_id]
    if owner:
        visible = [task for task in visible if task.get("owner_employee_id") == owner]
    return visible


@router.get("/board/{task_id}/eta")
def board_eta(
    task_id: str,
    store: StoreDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """Honest completion estimate for one card (§3.10).

    First qualifying basis wins (run_steps → session_progress →
    discipline_history); ``estimate_seconds`` is null when nothing qualifies —
    the UI renders "Estimating…", never a fabricated number.
    """
    _require_board_task(task_id, collab_store)
    task = collab_store.get_board_task(task_id)
    assert task is not None
    try:
        sessions_dal = _reconcile_sessions_dal()
    except Exception:  # noqa: BLE001
        sessions_dal = None
    return compute_board_eta(store, sessions_dal, task)


@router.get("/board/needs-response")
def board_needs_response(
    store: StoreDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """Ranked Needs Response band — conversations blocked on the operator.

    Strict predicate (see ``omniagentos.intake.needs_response``): only cards
    whose session/run cannot proceed until a human decides. Blocked cards and
    open suggestions are backlog and are excluded. Ordering is the product —
    riskier first, then oldest wait. No client sort control.
    """
    board = reconcile_board(store, collab_store, archived=0)
    # list invariance: the payload helper reads rows as Mapping, never mutates.
    return list_needs_response_payload(cast(list[Mapping[str, Any]], board))


@router.get("/board/{task_id}")
def get_board_card(
    task_id: str,
    store: StoreDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """ONE reconciled card — the same shape as a ``GET /api/board`` element.

    Declared AFTER ``/board/needs-response`` so the literal path keeps winning
    over this parameterised one (otherwise "needs-response" is read as a card id
    and 404s).

    The detail drawer previously found its card by downloading the whole board —
    2.66 MB, and a second full download of the archived feed when the card was
    not in the first. This returns that one row, reconciled the same way.

    The payload is EQUAL to a list element (including the flattened
    ``swarm_phase``/``swarm_integration``), never a superset: this route is
    public exactly like ``GET /api/board``, so it deliberately does not ship the
    raw ``swarm_json`` envelope — acceptance text, verify commands and
    per-attempt dirty file paths are not part of what the board list already
    makes readable. See ``reconcile_board_card``.

    404 when no such card exists — archived cards are returned normally, since
    "archived" is a property of the card, not a reason to hide it from a direct
    lookup.
    """
    card = reconcile_board_card(store, collab_store, task_id)
    if card is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    return card


@router.post("/board/{task_id}/message")
def post_board_message(
    task_id: str,
    body: TaskMessageRequest,
    _: AuthorizedDep,
    collab_store: CollabStoreDep,
    longhaul_store: LonghaulStoreDep,
    audit_store: StoreDep,
) -> dict[str, Any]:
    _require_board_task(task_id, collab_store)
    if not body.content.strip():
        fail(400, "validation", "content is required")
    from omniagentos.longhaul.steering import SteeringManager

    manager = SteeringManager(longhaul_store)
    turn_row = manager.append_turn(task_id, "user", body.content)
    # Fan-in to the live claude attempt so the PostToolUse hook can deliver
    # mid-run; best-effort — prompt re-injection at the next (re)spawn is the
    # guaranteed path, and the engine refuses to finish with pending steering.
    attempt = longhaul_store.current_attempt(task_id)
    if (
        attempt is not None
        and attempt.get("session_id")
        and attempt.get("harness") == "cli-claude"
        and not attempt.get("ended_at")
    ):
        try:
            manager.enqueue_for_live_session(str(attempt["session_id"]), body.content)
        except Exception:  # pragma: no cover - queue is advisory
            logging.getLogger(__name__).warning(
                "longhaul: live steering enqueue failed for %s", task_id
            )
    turn = _turn_with_delivery(turn_row)
    _emit_task_event(
        audit_store,
        "task.message",
        task_id,
        {"task_id": task_id, "turn_id": turn["id"], "seq": turn["seq"]},
    )
    return turn


@router.get("/board/{task_id}/conversation")
def get_board_conversation(
    task_id: str,
    _: AuthorizedDep,
    collab_store: CollabStoreDep,
    longhaul_store: LonghaulStoreDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    """A task's steering conversation, OLDEST TURN FIRST.

    Ordering is now the same as ``GET /api/chats/{id}/messages``: the two
    conversation reads in this product disagreed (chats chronological, this one
    newest-first), so any component showing both had to know which one it was
    talking to. ``limit`` still selects the most RECENT turns — the window is
    unchanged, only its presentation order is — and the store keeps its
    newest-first query so the LIMIT still means "the last N".
    """
    _require_board_task(task_id, collab_store)
    newest_first = longhaul_store.task_turns(task_id, limit)
    return [_turn_with_delivery(turn) for turn in reversed(newest_first)]


@router.get("/board/{task_id}/sessions")
def get_board_sessions(
    task_id: str,
    _: AuthorizedDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """Every session working (or having worked) this card, across ALL
    execution paths — the task-details page's one authoritative lookup.

    Before this route, the details page could only follow ``result_ref``
    when it was a ``ses_`` id, which is set at COMPLETION for most lanes —
    so an ACTIVE task showed no session, no model, no checklist, and no
    transcript. Sources unified here:

    - orchestrate lane: ``result_ref = orch_…`` → orchestration_steps'
      session ids (per-step titles included),
    - swarm cards: ``swarm_attempts`` rows (provider/model/tier/effort),
    - direct/fast lane: ``result_ref = ses_…``.

    Longhaul attempt chains stay on ``GET /board/{task_id}/longhaul``.
    """
    task = _require_board_task(task_id, collab_store)
    from omniagentos.api.routes.sessions import get_sessions_dal

    sessions_dal = get_sessions_dal()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(session_id: str | None, source: str, **extra: Any) -> None:
        if not session_id or session_id in seen:
            return
        seen.add(session_id)
        row = sessions_dal.get_session(session_id)
        if row is None:
            return
        found.append(
            {
                "id": str(row["id"]),
                "state": str(row.get("state") or ""),
                "model": row.get("model"),
                "provider": row.get("provider"),
                "title": row.get("title"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "source": source,
                **extra,
            }
        )

    orchestration: dict[str, Any] | None = None
    ref = task.get("result_ref")
    if isinstance(ref, str) and ref.startswith("orch_"):
        lifecycle = OrchestrationsDal(_sqlite_db_path(collab_store))
        try:
            orch = lifecycle.get(ref)
            if orch is not None:
                orchestration = {
                    "id": str(orch["id"]),
                    "status": str(orch.get("status") or ""),
                    "stage": orch.get("stage"),
                    "error": orch.get("error"),
                }
                resume_state = lifecycle.load_resume_state(ref)
                for step in resume_state.steps if resume_state else []:
                    add(
                        getattr(step, "session_id", None),
                        "orchestration",
                        step_seq=getattr(step, "seq", None),
                        step_title=getattr(step, "title", None),
                        step_status=getattr(step, "status", None),
                    )
        finally:
            lifecycle.close()

    if task.get("swarm_run_id"):
        try:
            for attempt in _swarm_attempts_for_board_task(task, collab_store):
                add(
                    attempt.get("session_id"),
                    "swarm",
                    tier=attempt.get("tier"),
                    effort=attempt.get("effort"),
                    end_reason=attempt.get("end_reason"),
                    detail=attempt.get("detail"),
                )
        except Exception:  # noqa: BLE001 -- swarm detail is additive, never a 500.
            LOG.warning("swarm attempts unavailable for %s", task_id, exc_info=True)

    if isinstance(ref, str) and ref.startswith("ses_"):
        add(ref, "direct")

    live = [
        s for s in found if s["state"] in {"starting", "running", "resuming", "awaiting_approval"}
    ]
    return {
        "sessions": found,
        "orchestration": orchestration,
        "live_session_id": live[0]["id"] if live else None,
    }


@router.post("/board/{task_id}/pause")
def pause_board_task(task_id: str, store: StoreDep, collab_store: CollabStoreDep) -> dict[str, Any]:
    """Pause a card's live work WITHOUT archiving it (the board's Pause
    button). Cancels the linked run/session/orchestration best-effort via
    the same helper the archive flow uses, marks the card cancelled, and
    reports it resumable — POST /board/{id}/retry resumes (manual=True)."""
    task = collab_store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})

    from omniagentos.api.routes.sessions import get_sessions_dal

    paused_run, paused_session = pause_board_task_work(store, task, sessions_dal=get_sessions_dal())
    if task.get("status") not in {
        BoardTaskStatus.DONE.value,
        BoardTaskStatus.CANCELLED.value,
    }:
        collab_store.update_board_task(task_id, {"status": BoardTaskStatus.CANCELLED.value})
    _emit_board_event(collab_store, task_id, run_id=paused_run, session_id=paused_session)
    return {
        "task_id": task_id,
        "paused_run": paused_run,
        "paused_session": paused_session,
        "status": BoardTaskStatus.CANCELLED.value,
        "resumable": True,
    }


@router.get("/board/{task_id}/longhaul")
def get_board_longhaul(
    task_id: str,
    _: AuthorizedDep,
    collab_store: CollabStoreDep,
    longhaul_store: LonghaulStoreDep,
) -> dict[str, Any]:
    task = _require_board_task(task_id, collab_store)
    longhaul = longhaul_store.get_longhaul_json(task_id) or {}
    if task.get("swarm_run_id"):
        attempts = _swarm_attempts_for_board_task(task, collab_store)
        current_attempt = next(
            (attempt for attempt in reversed(attempts) if attempt.get("ended_at") is None),
            None,
        )
    else:
        attempts = [dict(attempt) for attempt in longhaul_store.list_attempts(task_id)]
        longhaul_current = longhaul_store.current_attempt(task_id)
        current_attempt = dict(longhaul_current) if longhaul_current is not None else None
    from omniagentos.longhaul.workbook import read_workbook

    return {
        "attempts": attempts,
        "workbook": read_workbook(task_id),
        "acceptance": longhaul.get("acceptance"),
        "park_state": task.get("park_state"),
        "current_worker": current_attempt,
    }


class BulkArchiveRequest(IntakeModel):
    task_ids: list[str] = Field(min_length=1, max_length=200)


def _archive_card(
    store: Any, collab_store: Any, task: dict[str, Any], *, sessions_dal: Any
) -> tuple[dict[str, Any], str | None, str | None]:
    """Pause linked work then archive one card, preserving archive idempotency."""
    task_id = str(task["id"])
    # If already archived, return immediately with no pause, no update, no event.
    if task.get("archived_at") is not None:
        row = collab_store.get_board_task(task_id)
        if row is None:
            raise RuntimeError(f"board task disappeared while archiving: {task_id}")
        row["paused_run"] = None
        row["paused_session"] = None
        return row, None, None

    paused_run, paused_session = pause_board_task_work(store, task, sessions_dal=sessions_dal)
    collab_store.update_board_task(task_id, {"archived_at": utc_now_iso()})
    row = collab_store.get_board_task(task_id)
    if row is None:
        raise RuntimeError(f"board task disappeared while archiving: {task_id}")
    _emit_board_event(collab_store, task_id, run_id=paused_run, session_id=paused_session)
    row["paused_run"] = paused_run
    row["paused_session"] = paused_session
    return row, paused_run, paused_session


@router.post("/board/archive")
def archive_board_tasks(
    body: BulkArchiveRequest,
    store: StoreDep,
    collab_store: CollabStoreDep,
) -> dict[str, Any]:
    """Archive up to 200 cards, pausing any live linked work first."""
    from omniagentos.api.routes.sessions import get_sessions_dal

    sessions_dal = get_sessions_dal()
    archived: list[str] = []
    skipped: list[dict[str, str]] = []
    paused_runs: list[str] = []
    paused_sessions: list[str] = []
    seen: set[str] = set()
    for task_id in body.task_ids:
        if task_id in seen:
            skipped.append({"id": task_id, "reason": "duplicate"})
            continue
        seen.add(task_id)
        task = collab_store.get_board_task(task_id)
        if task is None:
            skipped.append({"id": task_id, "reason": "not_found"})
            continue
        try:
            _, paused_run, paused_session = _archive_card(
                store, collab_store, task, sessions_dal=sessions_dal
            )
            archived.append(task_id)
            if paused_run is not None:
                paused_runs.append(paused_run)
            if paused_session is not None:
                paused_sessions.append(paused_session)
        except Exception as e:  # noqa: BLE001 -- always return full accounting
            skipped.append({"id": task_id, "reason": f"error:{type(e).__name__}"})
    return {
        "archived": archived,
        "skipped": skipped,
        "paused_runs": paused_runs,
        "paused_sessions": paused_sessions,
    }


@router.post("/board/{task_id}/cancel")
def cancel_board_task(task_id: str, collab_store: CollabStoreDep) -> dict[str, Any]:
    """Cancel a board card and request cancellation of its linked session."""
    task = collab_store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})

    session_id = task.get("result_ref")
    linked_session_id: str | None = None
    if isinstance(session_id, str) and session_id.startswith("ses_"):
        from omniagentos.api.routes.sessions import get_sessions_dal

        linked_session_id = session_id
        dal = get_sessions_dal()
        session = dal.get_session(session_id)
        if session is not None and str(session["state"]) not in {
            "completed",
            "failed",
            "cancelled",
            "killed",
        }:
            dal.request_cancel(session_id)

    if task.get("status") != BoardTaskStatus.CANCELLED.value:
        collab_store.update_board_task(task_id, {"status": BoardTaskStatus.CANCELLED.value})
        _emit_board_event(collab_store, task_id, session_id=linked_session_id)
    return {
        "task_id": task_id,
        "session_id": linked_session_id,
        "status": BoardTaskStatus.CANCELLED.value,
    }


@router.post("/board/{task_id}/archive")
def archive_board_task(
    task_id: str, store: StoreDep, collab_store: CollabStoreDep
) -> dict[str, Any]:
    """Archive a board card (mark with current timestamp). Idempotent."""
    task = collab_store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})

    from omniagentos.api.routes.sessions import get_sessions_dal

    row, _, _ = _archive_card(store, collab_store, task, sessions_dal=get_sessions_dal())
    return row


@router.post("/board/{board_task_id}/retry")
def retry_board_task(
    board_task_id: str, store: StoreDep, collab_store: CollabStoreDep
) -> dict[str, Any]:
    """Retry a failed or blocked board task via orchestration resume.

    Calls resume_orchestration with manual=True to allow retry even in
    edge states. Returns {"run_id","resumed","reason"} on success.

    - 404: board_task_id not found
    - 409: board_task not resumable (conductor live, longhaul, etc.)
    """
    task = collab_store.get_board_task(board_task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": board_task_id})

    resume_func = _resolve_resume_orchestration()
    try:
        result = resume_func(board_task_id, store=store, collab_store=collab_store, manual=True)
        return result
    except LookupError as exc:
        fail(404, "not_found", str(exc), {"id": board_task_id})
    except ValueError as exc:
        fail(409, "conflict", str(exc), {"id": board_task_id})


__all__ = ["router"]
