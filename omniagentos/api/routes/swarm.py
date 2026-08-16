"""HTTP surface for Swarm Mode (WP6a — plan/read/cancel slice).

Additive V2 route, same conventions as ``contracts/reliability-api.md``: JSON
bodies, the standard error envelope (``{"error": {"code", "message", "detail"}}``
via :func:`omniagentos.api.services.fail` / ``ApiError``), a session-token gate
on EVERY route (hoisted to the router constructor, board_files.py's F-016
idiom, so a route added here later can never forget it), and registration into
``omniagentos/api/main.py`` via one import + one ``include_router`` line.

Ships in two slices per the plan doc ("WP6 — API + activity"):

  * THIS slice: ``GET /api/swarm`` (fleet list), ``POST /api/swarm`` (plan +
    provision via WP4's planner), ``GET /api/swarm/{id}`` (detail),
    ``GET /api/swarm/{id}/activity`` (event feed),
    ``POST /api/swarm/{id}/cancel``.
  * WP5a's slice: the coordinator/scheduler that actually RUNS a provisioned
    plan. It landed, and with it the kill fanout cancel's status flip triggers
    on the scheduler's next status poll. ``cancel`` now ALSO signals the run's
    live sessions directly (see :func:`cancel_swarm_run`), because a status
    flip only reaches a coordinator that is still alive to read it.

**WP4 landed during this slice's own development** (this route was rebased
onto ``swarm/wp4-planner`` after it merged) — ``POST /api/swarm`` calls the
real :func:`omniagentos.swarm.planner.plan_swarm` /
:func:`~omniagentos.swarm.planner.provision_run`, not a placeholder. The
"IF importable" framing the original WP6a brief anticipated (written before
WP4 merged) is kept as a narrow resilience seam (see
``_wp4_planner_functions``) against a future partial revert/rename, not the
common path anymore.

Every route reads via :class:`omniagentos.swarm.dal.SwarmDal` (WP1) and emits
``swarm.event`` activity via :class:`omniagentos.swarm.activity.StoreSwarmEmitter`
(this slice).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, ConfigDict

from omniagentos.accounts.service import list_accounts
from omniagentos.api.deps import StoreDep
from omniagentos.api.routes._receipt_projection import (
    APPROVAL_ACTION,
    project_receipt_payload,
)
from omniagentos.api.routes.board_files import _enforce_workspace_floor
from omniagentos.api.routes.control import _authorized, fail
from omniagentos.budget.policy import MIN_RUN_BUDGET_USD, swarm_run_budget
from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.contracts import default_db_path
from omniagentos.routing import limit_state
from omniagentos.sessions.dal import TERMINAL_SESSION_STATES
from omniagentos.swarm.activity import StoreSwarmEmitter
from omniagentos.swarm.contracts import (
    ACTION_PLAN_CREATED,
    ACTION_PROVIDER_SWITCHED,
    ACTION_RUN_FAILED,
    TERMINAL_RUN_STATUSES,
    SwarmPlanDecision,
)
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.plan_safety import (
    PlanSafetyError,
    assert_plan_safe_for_provision,
    decide_from_plans,
)

LOG = logging.getLogger(__name__)

_SWARM_JOBS: dict[str, dict[str, Any]] = {}
_SWARM_JOBS_LOCK = threading.Lock()

router = APIRouter(prefix="/api/swarm", tags=["swarm"], dependencies=[Depends(_authorized)])

# Fleet-active statuses (planner/coordinator holding capacity); mirrors
# SwarmDal._ACTIVE_STATUSES, kept as a local literal so this route file does not
# reach into the dal module's private constant.
_ACTIVE_STATUSES = ("planning", "running", "merging")

# Budget + fleet-size defaults/ceilings (constants for now — the plan doc's
# "Fleet capacity model" names a real `configs/swarm.yaml` for these; that
# file does not exist yet, so these are placeholders a future config-loading
# pass should replace).
# Floor for a run whose caller named no budget; the real figure is sized to the
# plan once its task count is known (budget.policy.swarm_run_budget).
DEFAULT_BUDGET_USD_MAX = MIN_RUN_BUDGET_USD
# A typo guard on OPERATOR-SUPPLIED input ($2000, not $200000) -- deliberately not
# a work blocker: plan-derived budgets are not subject to it, and exceeding a
# budget no longer stops a run (budget.policy is advisory by default).
MAX_BUDGET_USD_MAX = 2_000.0
# Dashboard DENOMINATORS ONLY — admission is enforced from configs/swarm.yaml
# (intake.service reads max_concurrent_swarms; limit_state.fleet_available reads
# the session budget). These are plain constants rather than a per-request YAML
# read because they sit on the Command Center's polling routes; the mirror is
# pinned by tests/routing/test_fleet_scale.py, which fails if configs/swarm.yaml
# and these two drift apart (a stale denominator renders as ">100% utilized").
MAX_CONCURRENT_SWARMS = 20  # == configs/swarm.yaml max_concurrent_swarms

# Fleet-wide swarm-session ceiling = max_sessions_global - reserved_small_task_slots
# (the most sessions swarm runs may ever hold; the reserve is small/fast/longhaul's).
MAX_SWARM_TERMINALS = 220

# Every provider the Command Center's provider-health panel (C4) always shows a
# row for — claude (``limit_state.DEFAULT_PROVIDER``) plus the wrapped CLI
# providers (``omniagentos.swarm.provider_exec.SUPPORTED_PROVIDERS``). A provider
# with zero ``claude_accounts`` rows still gets ONE implicit row (account_id
# null, status "unknown") so the panel never silently omits a configured lane;
# a provider with account rows gets one row per account. Kept as an ordered
# literal (not the frozenset) so the panel's row order is stable.
_KNOWN_PROVIDERS = ("claude", "codex", "grok", "gemini", "kimi", "qwen")

# The status vocabulary the panel understands (claude_accounts.status CHECK-ish
# domain); anything else collapses to "unknown".
_PROVIDER_STATUSES = ("ok", "rate_limited", "error", "unknown")


# ---------------------------------------------------------------------------
# SwarmDal dependency — process-lifetime instance, mirrors
# omniagentos.api.routes.categories.get_longhaul_store.
# ---------------------------------------------------------------------------

_SWARM_DAL: SwarmDal | None = None


def get_swarm_dal() -> SwarmDal:
    """Process-lifetime SwarmDal instance (overridable in tests)."""
    global _SWARM_DAL
    if _SWARM_DAL is None:
        _SWARM_DAL = SwarmDal(default_db_path())
    return _SWARM_DAL


SwarmDalDep = Annotated[SwarmDal, Depends(get_swarm_dal)]


class _SwarmEventStore(Protocol):
    """SQLite extension used without widening the frozen Store contract.

    Mirrors ``control.py``'s own ``_RunEventStore`` idiom for
    ``get_events_for_run`` — ``get_events_for_target`` is a concrete
    ``SqliteStore`` addition (WP6a), not part of the frozen ``Store``
    protocol.
    """

    def get_events_for_target(
        self, target_type: str, target_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...


class SwarmModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateSwarmRunRequest(SwarmModel):
    # Either key names the goal text; `brief` is the primary name from the plan
    # doc's WP6 signature ("{brief|spec, working_dir, budget}"), `spec` is
    # accepted as an alias for a caller that already has a refined spec string.
    brief: str | None = None
    spec: str | None = None
    working_dir: str
    budget_usd_max: float | None = None
    # M1 project axis. Optional; omitted/blank provisions an UNSCOPED run
    # (project_id NULL on the run row and on every card), which is the
    # pre-M1 behaviour of this route. A named project must exist.
    project_id: str | None = None
    # Optional provision params passthrough — exposes provision_run's existing
    # documented knobs (notably the D10 mode dial, params["speed"]: fast pins
    # the start tier to 'simple', ultra floors 'complex') on the direct route;
    # previously reachable only via intake dispatch.
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# WP4 planner LLM seams — overridable in tests exactly like
# omniagentos.api.routes.intake's ClarifyLLMDep/PlannerLLMDep, so a route test
# never shells out to the real Claude CLI / Fable network call the way
# tests/swarm/test_planner.py's own suite always avoids too (every one of its
# tests injects a fake planner_llm/clarify_llm/recall_fn).
# ---------------------------------------------------------------------------


def get_swarm_planner_llm() -> Any:
    """The swarm-plan LLM seam (Fable, effort scaled by goal complexity)."""
    from omniagentos.swarm.planner import default_swarm_planner_llm

    return default_swarm_planner_llm


def get_swarm_clarify_llm() -> Any:
    """The swarm-plan's bounded, non-blocking clarify-pass LLM seam.

    ``None`` lets ``plan_swarm`` fall through to ``clarify_intake``'s own
    default (Fable via the Claude CLI, effort medium); tests override this to
    a fast deterministic fake.
    """
    return None


def get_swarm_recall_fn() -> Any:
    """The swarm-plan's prior-lessons recall seam."""
    from omniagentos.swarm.planner import default_recall_lessons

    return default_recall_lessons


SwarmPlannerLLMDep = Annotated[Any, Depends(get_swarm_planner_llm)]
SwarmClarifyLLMDep = Annotated[Any, Depends(get_swarm_clarify_llm)]
SwarmRecallFnDep = Annotated[Any, Depends(get_swarm_recall_fn)]


def _wp4_planner_functions() -> tuple[Any, Any] | None:
    """Resolve WP4's bundle planner/provisioner, if the module is present.

    ``omniagentos.swarm.planner`` merged during this slice's own development
    (this route was rebased onto it) — this indirection is now a resilience
    seam against a future partial revert/rename, not the "WP4 not merged yet"
    situation the original WP6a brief anticipated. If either name ever moves,
    ``POST /api/swarm`` 503s with a clear message instead of failing to import
    at process startup.
    """
    try:
        from omniagentos.swarm.planner import plan_swarm_bundles, provision_run
    except ImportError:
        return None
    return plan_swarm_bundles, provision_run


def _plan_decision_error(decision: SwarmPlanDecision) -> tuple[int, str, str, dict[str, Any]]:
    """Map a typed pre-run decision to the stable HTTP/background error shape."""
    issue_payload = [issue.model_dump(mode="json") for issue in decision.issues]
    issue_codes = {issue.code for issue in decision.issues}
    if "multiple_bundles" in issue_codes or "multi_bundle" in issue_codes:
        # Interim multi-bundle containment (R4): refuse all, persist none.
        # HTTP code matches the issue code so clients and tests share one token.
        status, code = 409, "multiple_bundles"
    else:
        status = {
            "policy_denied": 403,
            "planner_unavailable": 503,
            "invalid_plan": 422,
            "impossible": 422,
            "needs_clarification": 422,
            # A draft is well-formed but owns a non-file resource: it is
            # reviewable, never executable, and must not read as a defect.
            "draft": 422,
        }.get(decision.disposition, 422)
        code = str(decision.disposition)
    message = decision.reason or "swarm plan is not executable"
    return (
        status,
        code,
        message,
        {
            "disposition": decision.disposition,
            "issues": issue_payload,
            "reason": decision.reason,
        },
    )


def _fail_plan_decision(decision: SwarmPlanDecision) -> None:
    status, code, message, detail = _plan_decision_error(decision)
    fail(status, code, message, detail)


def _select_safe_plan(
    plans: list[Any],
    *,
    working_dir: str,
) -> Any:
    """Return the single ready plan, or raise a typed pre-run error."""
    decision = decide_from_plans(plans, workspace_dir=working_dir)
    if not decision.is_ready:
        _fail_plan_decision(decision)
    return decision.plans[0]


def _activate_run_safely(run_id: str, plan: Any, working_dir: str) -> None:
    """Revalidate safety immediately before activation (mutation: activation-skips-recheck)."""
    assert_plan_safe_for_provision(plan, workspace_dir=working_dir)
    from omniagentos.swarm.scheduler import activate_run_if_enabled

    activate_run_if_enabled(str(run_id))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_json_field(raw: Any, *, default: Any) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _task_progress(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {status.value: 0 for status in BoardTaskStatus}
    for task in tasks:
        status = str(task.get("status") or "")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(tasks)
    return counts


def _member_tasks(swarm_dal: SwarmDal, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Board cards that are real DAG work units for this run.

    Excludes the run's own root/container card (``run["board_task_id"]``) —
    WP4's ``provision_run`` gives it ``swarm_run_id`` too (so it is bound to
    the run) but it is never a work unit (``status="in_progress"``, never
    eligible, per that function's own docstring): counting it would inflate
    every progress/task-count view by one phantom "in_progress" task.
    """
    root_id = run.get("board_task_id")
    return [
        task for task in swarm_dal.tasks_for_run(str(run["id"])) if str(task["id"]) != str(root_id)
    ]


def _run_summary(swarm_dal: SwarmDal, run: dict[str, Any]) -> dict[str, Any]:
    """One fleet-list row — matches the dashboard's ``SwarmRunSummary`` shape
    (``dashboard/src/features/swarm/types.ts``, C1 scaffold)."""
    counts = _task_progress(_member_tasks(swarm_dal, run))
    total = counts.pop("total")
    done = counts.get(BoardTaskStatus.DONE.value, 0)
    return {
        "id": run["id"],
        "status": run["status"],
        "goal": run["goal"],
        "working_dir": run["working_dir"],
        "board_task_id": run.get("board_task_id"),
        "target_concurrency": run["target_concurrency"],
        "max_concurrency": run["max_concurrency"],
        "cost_usd": run.get("cost_usd", 0),
        "budget_usd_max": run.get("budget_usd_max"),
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "task_counts": counts,
        "progress": {"done": done, "total": total},
    }


def _serialize_event(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize one ``swarm.event`` row for the activity feed.

    ``approval.recorded`` payloads are routed through the same strict
    allow-list projector as the engine approval receipt
    (:mod:`omniagentos.api.routes._receipt_projection`) rather than copied
    verbatim: the raw payload is caller-supplied at write time (F001), so a
    hostile receipt smuggling nested credential-shaped fields under an
    allow-listed key must be stripped before it reaches this feed, the same
    as it is stripped from the approval-receipt endpoint.
    """
    out = dict(row)
    payload = _parse_json_field(row.get("payload_json"), default={})
    action = str(row.get("action") or "")
    if action == APPROVAL_ACTION and isinstance(payload, dict):
        payload = project_receipt_payload(payload)
    out["payload"] = payload
    out.pop("payload_json", None)
    return out


def _validate_working_dir(working_dir: str) -> Path:
    """Resolve + validate a swarm run's proposed working dir.

    Reuses the board-files F-015 approved-root floor
    (:func:`omniagentos.api.routes.board_files._enforce_workspace_floor`)
    rather than re-deriving it — the same reasoning as that module's own
    docstring: a workspace path handed in from outside the process must never
    be trusted as a capability on its own. Unlike board_files' own
    ``_require_workspace`` (which resolves a path a board card already points
    at), this validates a path an HTTP caller is naming directly, so a missing
    or non-directory path is the caller's mistake (400), not a 404.
    """
    if not working_dir or not working_dir.strip():
        fail(400, "validation", "working_dir is required")
    candidate = Path(working_dir).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        fail(400, "validation", "working_dir does not exist", {"working_dir": working_dir})
    if not resolved.is_dir():
        fail(400, "validation", "working_dir is not a directory", {"working_dir": working_dir})
    _enforce_workspace_floor(resolved)
    return resolved


def _validate_project_id(store: Any, project_id: str | None) -> str | None:
    """Resolve the project this swarm run belongs to, or ``None`` for unscoped.

    M1. The default is NULL and it is stated, not inferred: a caller that names
    no project gets an UNSCOPED run, never a "default project". Inventing one
    would be the silent wrong value — the broker (C11/D-31) reads a project
    binding as an authorization fact, so a fabricated project id is a
    fabricated grant scope, and `grant_project_unbound` (honest refusal) is the
    behaviour we want for unscoped work rather than a cross-project match.

    A project that is NAMED must exist. ``SwarmDal`` runs without
    ``PRAGMA foreign_keys=ON``, so the ``board_tasks.project_id`` FK declared by
    migration 087 does not fire on that connection — an unchecked id would
    persist as an unresolvable reference and every project-scoped query would
    then quietly return nothing for it.
    """
    if project_id is None:
        return None
    normalized = project_id.strip()
    if not normalized:
        return None
    connection = getattr(store, "_connection", None)
    if connection is None:  # in-memory store double: nothing to validate against
        return normalized
    row = connection.execute("SELECT id FROM projects WHERE id = ?", (normalized,)).fetchone()
    if row is None:
        fail(404, "not_found", "unknown project", {"project_id": normalized})
    return normalized


def _validate_budget(budget_usd_max: float | None) -> float | None:
    """Validate an operator-supplied budget; None means "size it to the plan"."""
    if budget_usd_max is None:
        return None
    if budget_usd_max <= 0:
        fail(
            400, "validation", "budget_usd_max must be positive", {"budget_usd_max": budget_usd_max}
        )
    if budget_usd_max > MAX_BUDGET_USD_MAX:
        fail(
            400,
            "validation",
            "budget_usd_max exceeds the hard ceiling",
            {"budget_usd_max": budget_usd_max, "ceiling": MAX_BUDGET_USD_MAX},
        )
    return budget_usd_max


# ---------------------------------------------------------------------------
# Overview (C2) helpers — fleet-level metric rollup across ACTIVE runs.
#
# WP7's `omniagentos/swarm/summary.py` is the intended owner of the frozen
# throughput estimates + est-completion; until it merges these are computed
# directly from `swarm_runs.metrics_json`/`plan_json` and the board cards'
# `swarm_json`, preferring any WP7-populated `metrics_json` value where one
# exists so the swap is a drop-in. See the per-field TODO(WP7) notes below.
# ---------------------------------------------------------------------------

_ACTIVE_TASK_STATUSES = (BoardTaskStatus.CLAIMED.value, BoardTaskStatus.IN_PROGRESS.value)


def _parse_ts(value: Any) -> datetime | None:
    """Tolerant parse of a stored ``utc_now_iso()`` timestamp to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _minutes_since(start: datetime | None, now: datetime) -> float:
    if start is None:
        return 0.0
    return max(0.0, (now - start).total_seconds() / 60.0)


def _phase_of(swarm_json: dict[str, Any]) -> str:
    """The kanban phase overlay for an in-flight card (columns.ts vocabulary).

    Prefers a scheduler-written ``swarm_json.swarm_phase`` (review/testing/…)
    when present; the integration task always overlays ``integration``; every
    other running card is plain ``running``.
    """
    phase = str(swarm_json.get("swarm_phase") or "").strip().lower()
    if phase:
        return phase
    if swarm_json.get("integration"):
        return "integration"
    return "running"


def _assignment_reason(attempt: dict[str, Any] | None, swarm_json: dict[str, Any]) -> str | None:
    """A short, human-readable "why this worker" for an in-flight card.

    No dedicated column persists a routing rationale yet, so this surfaces the
    effective assignment: the live attempt's provider/model (what the router
    actually picked), falling back to the planned/escalated tier when no attempt
    row exists. TODO(WP5b/WP7): swap to a persisted routing reason if one lands.
    """
    if attempt:
        parts = [str(attempt.get(key) or "").strip() for key in ("provider", "model")]
        label = " · ".join(part for part in parts if part)
        if label:
            return label
    tier = (
        swarm_json.get("current_tier")
        or swarm_json.get("tier_hint")
        or swarm_json.get("complexity")
    )
    return f"tier: {tier}" if tier else None


def _count_provider_switches(store: Any, run_id: str) -> int:
    """Count ``provider_switched`` swarm events for a run (delays avoided proxy).

    A rate-limited/timed-out attempt that reroutes to a different provider is a
    rate-limit wait that never happened. Best-effort + bounded (one 500-row
    fetch); WP7's ``metrics_json`` is preferred over this when it carries the
    field.
    """
    try:
        event_store = cast(_SwarmEventStore, store)
        rows = event_store.get_events_for_target("swarm_run", run_id, after_id=0, limit=500)
    except Exception:  # noqa: BLE001 -- observability count must never fail the route.
        return 0
    return sum(1 for row in rows if str(row.get("action") or "") == ACTION_PROVIDER_SWITCHED)


def _parse_formation_from_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Extract formation metadata from plan notes / plan fields if present."""
    if not isinstance(plan, dict):
        return None
    formation = plan.get("formation")
    if isinstance(formation, dict) and formation.get("id"):
        return {
            "id": str(formation.get("id")),
            "implementers": list(formation.get("implementers") or []),
            "reviewer": formation.get("reviewer"),
            "planner": formation.get("planner"),
            "mechanical_gate": bool(formation.get("mechanical_gate", True)),
            "confidence": formation.get("confidence"),
            "low_confidence": bool(formation.get("low_confidence", False)),
            "topology": formation.get("topology"),
            "reason": formation.get("reason"),
        }
    notes = plan.get("notes") or plan.get("assumptions") or []
    if isinstance(notes, str):
        notes = [notes]
    for note in notes:
        text = str(note).strip()
        if "formation:" in text:
            # allow leading bullets / prefixes
            if not text.startswith("formation:"):
                text = "formation:" + text.split("formation:", 1)[1]
        if text.startswith("formation:"):
            # formation: coding implementers=grok,gemini reviewer=opus mechanical=True
            # … confidence=0.4 … ; LOW_CONFIDENCE
            parts = text.split()
            fid = parts[1] if len(parts) > 1 else "unknown"
            impl: list[str] = []
            reviewer = None
            mechanical = True
            confidence = None
            low_confidence = "LOW_CONFIDENCE" in text or "low_confidence" in text.lower()
            for part in parts[2:]:
                if part.startswith("implementers="):
                    raw_impl = part.split("=", 1)[1].strip()
                    # Empty token, placeholder, or low-conf sentinel → no implementers.
                    if not raw_impl or raw_impl in {
                        "(none)",
                        "(none-low-conf)",
                        "none",
                        "-",
                    }:
                        impl = []
                        low_confidence = True
                    else:
                        impl = [
                            x
                            for x in raw_impl.split(",")
                            if x and x not in {"(none-low-conf)", "(none)"}
                        ]
                elif part.startswith("reviewer="):
                    reviewer = part.split("=", 1)[1]
                elif part.startswith("mechanical="):
                    mechanical = part.split("=", 1)[1].lower() in {"1", "true", "yes"}
                elif part.startswith("confidence="):
                    try:
                        confidence = float(part.split("=", 1)[1])
                    except ValueError:
                        confidence = None
            if confidence is not None and confidence < 0.5:
                low_confidence = True
            if not impl and "formation:" in text:
                # Empty implementers list is the low-conf signal from notes.
                if "LOW_CONFIDENCE" in text or (confidence is not None and confidence < 0.5):
                    low_confidence = True
            return {
                "id": fid,
                "implementers": impl,
                "reviewer": reviewer,
                "mechanical_gate": mechanical,
                "confidence": confidence,
                "low_confidence": low_confidence,
            }
    return None


def _build_team(swarm_dal: SwarmDal, store: Any) -> dict[str, Any]:
    """Assemble the team/fleet board for the Command Center.

    One query pattern per run (member tasks + live attempts + recent events).
    No token streams — only who is running and who was spawned.
    """
    from omniagentos.contracts import utc_now_iso

    active_runs = [run for run in swarm_dal.list_runs() if run["status"] in _ACTIVE_STATUSES]
    workers: list[dict[str, Any]] = []
    leaders: list[dict[str, Any]] = []
    recent_spawns: list[dict[str, Any]] = []
    leader_updates: list[dict[str, Any]] = []
    event_store = cast(_SwarmEventStore, store)

    for run in active_runs:
        run_id = str(run["id"])
        goal = str(run.get("goal") or "")
        plan = _parse_json_field(run.get("plan_json"), default={})
        if not isinstance(plan, dict):
            plan = {}
        formation = _parse_formation_from_plan(plan)
        leaders.append(
            {
                "swarm_run_id": run_id,
                "goal": goal[:200],
                "status": run.get("status"),
                "formation": formation,
                "target_concurrency": run.get("target_concurrency"),
                "max_concurrency": run.get("max_concurrency"),
                "started_at": run.get("started_at"),
            }
        )
        members = _member_tasks(swarm_dal, run)
        for task in members:
            task_id = str(task["id"])
            status = str(task.get("status") or "")
            swarm_json = _parse_json_field(task.get("swarm_json"), default={})
            if not isinstance(swarm_json, dict):
                swarm_json = {}
            attempt = swarm_dal.current_attempt(task_id)
            is_live = (
                status in _ACTIVE_TASK_STATUSES
                and attempt is not None
                and not attempt.get("ended_at")
            )
            role = "worker"
            if swarm_json.get("integration"):
                role = "integrator"
            title = str(task.get("title") or task_id)
            entry = {
                "swarm_run_id": run_id,
                "goal": goal[:120],
                "task_id": task_id,
                "task_title": title[:200],
                "task_status": status,
                "role": role,
                "phase": _phase_of(swarm_json),
                "session_id": (attempt or {}).get("session_id"),
                "provider": (attempt or {}).get("provider"),
                "model": (attempt or {}).get("model"),
                "tier": (attempt or {}).get("tier"),
                "attempt_id": (attempt or {}).get("id"),
                "started_at": (attempt or {}).get("started_at"),
                "assignment_reason": _assignment_reason(attempt, swarm_json),
                "live": bool(is_live),
            }
            if is_live or status in _ACTIVE_TASK_STATUSES:
                workers.append(entry)

        try:
            rows = event_store.get_events_for_target("swarm_run", run_id, after_id=0, limit=80)
        except Exception:  # noqa: BLE001
            rows = []
        for row in reversed(rows):  # newest first after reverse of ascending
            action = str(row.get("action") or "")
            payload = row.get("payload") or row.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            item = {
                "ts": row.get("ts") or row.get("created_at"),
                "swarm_run_id": run_id,
                "action": action,
                "payload": payload,
                "event_id": row.get("id"),
            }
            if action in {"worker_spawned", "task_assigned"}:
                recent_spawns.append(item)
            if action in {"leader_update", "plan_created", "run_started", "resize", "task_split"}:
                leader_updates.append(item)

    live_workers = [w for w in workers if w.get("live")]
    # newest first for feeds
    recent_spawns = sorted(recent_spawns, key=lambda x: str(x.get("ts") or ""), reverse=True)[:40]
    leader_updates = sorted(leader_updates, key=lambda x: str(x.get("ts") or ""), reverse=True)[:40]

    return {
        "generated_at": utc_now_iso(),
        "running_agents": len(live_workers),
        "claimed_agents": len(workers),
        "active_swarms": len(active_runs),
        "leaders": leaders,
        "workers": sorted(
            workers,
            key=lambda w: (0 if w.get("live") else 1, str(w.get("started_at") or "")),
            reverse=False,
        ),
        "live_workers": live_workers,
        "recent_spawns": recent_spawns,
        "leader_updates": leader_updates,
    }


def _build_overview(swarm_dal: SwarmDal, store: Any) -> dict[str, Any]:
    """Aggregate fleet-level metrics across every ACTIVE run (planning/running/
    merging). Per-run detail stays on ``GET /api/swarm/{id}``."""
    active_runs = [run for run in swarm_dal.list_runs() if run["status"] in _ACTIVE_STATUSES]
    now = datetime.now(UTC)

    done_total = 0
    blocked_total = 0
    task_total = 0
    est_manual_total = 0
    est_swarm_total = 0.0
    actual_minutes_total = 0.0
    consumed_usd = 0.0
    cap_total = 0.0
    cap_present = False
    delays_avoided = 0
    est_completion_candidates: list[str] = []
    overview_tasks: list[dict[str, Any]] = []

    for run in active_runs:
        metrics = _parse_json_field(run.get("metrics_json"), default={})
        plan = _parse_json_field(run.get("plan_json"), default={})
        try:
            parallelism = float(plan.get("parallelism_ratio") or 1.0)
        except (TypeError, ValueError):
            parallelism = 1.0
        parallelism = max(parallelism, 1.0)

        members = _member_tasks(swarm_dal, run)
        task_total += len(members)
        run_est_agent = 0
        for task in members:
            swarm_json = _parse_json_field(task.get("swarm_json"), default={})
            if not isinstance(swarm_json, dict):
                swarm_json = {}
            status = str(task.get("status") or "")
            if status == BoardTaskStatus.DONE.value:
                done_total += 1
            elif status == BoardTaskStatus.BLOCKED.value:
                blocked_total += 1
            est_manual_total += int(swarm_json.get("est_manual_minutes") or 0)
            run_est_agent += int(swarm_json.get("est_agent_minutes") or 0)
            if status in _ACTIVE_TASK_STATUSES:
                attempt = swarm_dal.current_attempt(str(task["id"]))
                overview_tasks.append(
                    {
                        "task_id": task["id"],
                        "phase": _phase_of(swarm_json),
                        "session_id": (attempt or {}).get("session_id"),
                        "assignment_reason": _assignment_reason(attempt, swarm_json),
                    }
                )
        # est_swarm_minutes = the DAG floor: Σ est_agent ÷ parallelism ratio.
        est_swarm_total += run_est_agent / parallelism
        actual_minutes_total += _minutes_since(_parse_ts(run.get("started_at")), now)

        consumed_usd += float(run.get("cost_usd") or 0.0)
        cap = run.get("budget_usd_max")
        if cap is not None:
            cap_present = True
            cap_total += float(cap)

        m_delays = metrics.get("rate_limit_delays_avoided")
        if isinstance(m_delays, (int, float)):
            delays_avoided += int(m_delays)
        else:
            delays_avoided += _count_provider_switches(store, str(run["id"]))

        est_completion = metrics.get("est_completion_at")
        if isinstance(est_completion, str) and est_completion.strip():
            est_completion_candidates.append(est_completion)

    active_terminals = swarm_dal.live_attempt_count()
    utilization_pct = (
        round(active_terminals / MAX_SWARM_TERMINALS * 100, 1) if MAX_SWARM_TERMINALS else 0.0
    )
    actual_minutes = round(actual_minutes_total, 1)
    speedup = round(est_manual_total / actual_minutes, 2) if actual_minutes > 0 else 0.0
    tasks_per_hour = round(done_total / (actual_minutes / 60.0), 2) if actual_minutes > 0 else 0.0

    if not active_runs:
        health = "idle"
    elif actual_minutes > 0 and speedup < 1.0:
        health = "degraded"
    elif blocked_total > 0:
        health = "degraded"
    else:
        health = "healthy"

    return {
        "active": len(active_runs),
        "progress": {
            "done": done_total,
            "total": task_total,
            "pct": round(done_total / task_total * 100) if task_total else 0,
        },
        # TODO(WP7): swap to summary.est_completion once swarm/summary.py lands;
        # until then the latest per-run metrics_json.est_completion_at wins (the
        # fleet finishes when its slowest active run does), else null.
        "est_completion_at": max(est_completion_candidates) if est_completion_candidates else None,
        "throughput": {
            "est_manual_minutes": est_manual_total,
            "est_swarm_minutes": round(est_swarm_total),
            "actual_minutes": actual_minutes,
            "speedup": speedup,
            "time_saved_minutes": round(est_manual_total - actual_minutes, 1),
            "active_terminals": active_terminals,
            "max_terminals": MAX_SWARM_TERMINALS,
            "utilization_pct": utilization_pct,
            "idle_pct": round(100.0 - utilization_pct, 1),
            "tasks_per_hour": tasks_per_hour,
            "rate_limit_delays_avoided": delays_avoided,
            "health": health,
        },
        "tasks": overview_tasks,
        "budget": {
            "consumed_usd": round(consumed_usd, 2),
            "cap_usd": round(cap_total, 2) if cap_present else None,
        },
    }


# ---------------------------------------------------------------------------
# Providers (C4) helpers — durable rate-limit / cooldown / inflight per
# provider account, sourced from WP2's `omniagentos.routing.limit_state` (the
# single durable authority) over the generalized `claude_accounts` table.
# ---------------------------------------------------------------------------


def _account_display_name(account: dict[str, Any]) -> str:
    """Prefer the account label; fall back to its config-dir basename, then id."""
    label = str(account.get("label") or "").strip()
    if label:
        return label
    config_dir = account.get("config_dir")
    if isinstance(config_dir, str) and config_dir.strip():
        base = Path(config_dir.rstrip("/")).name
        if base:
            return base
    return str(account.get("id") or "account")


def _provider_health_row(account: dict[str, Any], db_path: str) -> dict[str, Any]:
    """One provider-health row for a real ``claude_accounts`` account.

    ``active_sessions`` is the DURABLE inflight count (``limit_state`` — live,
    activity-fresh, non-kill-requested sessions attributed to the account plus
    open longhaul attempts and unexpired reservations), NOT a naive session
    scan; ``reset_in_seconds`` is the remaining durable cooldown (``None`` once
    it lapses even if a stale ``cooldown_until`` timestamp lingers)."""
    provider = str(account.get("provider") or limit_state.DEFAULT_PROVIDER)
    account_id = str(account["id"])
    status = str(account.get("status") or "unknown")
    if status not in _PROVIDER_STATUSES:
        status = "unknown"
    cooldown_until = account.get("cooldown_until")
    if not (isinstance(cooldown_until, str) and cooldown_until.strip()):
        cooldown_until = None
    remaining = limit_state.durable_cooldown_remaining(account_id, db_path=db_path)
    detail = account.get("status_detail")
    return {
        "provider": provider,
        "account_id": account_id,
        "display_name": _account_display_name(account),
        "status": status,
        "cooldown_until": cooldown_until,
        "reset_in_seconds": int(remaining) if remaining > 0 else None,
        "active_sessions": limit_state.inflight_count(account_id, db_path=db_path),
        "max_inflight": limit_state.max_inflight_per_account(provider),
        "status_detail": str(detail) if isinstance(detail, str) and detail.strip() else None,
    }


def _implicit_provider_row(provider: str) -> dict[str, Any]:
    """The single placeholder row for a provider with no ``claude_accounts`` rows.

    A wrapped CLI provider (codex/grok/gemini/kimi/qwen) commonly has no account row
    — it authenticates via its own config dir, not the multi-account pool — but
    the panel should still show the lane exists, with an "unknown" status and
    its configured per-account concurrency ceiling."""
    return {
        "provider": provider,
        "account_id": None,
        "display_name": provider,
        "status": "unknown",
        "cooldown_until": None,
        "reset_in_seconds": None,
        "active_sessions": 0,
        "max_inflight": limit_state.max_inflight_per_account(provider),
        "status_detail": None,
    }


def _build_providers(db_path: str) -> list[dict[str, Any]]:
    """Aggregate provider-health rows across every known + registered provider.

    Uses ``accounts.service.list_accounts`` (the SAME source the accounts-page
    fallback reads, so parity is exact) grouped by the migration-045
    ``provider`` column, then enriched with durable ``limit_state`` inflight +
    cooldown. Every provider in ``_KNOWN_PROVIDERS`` always appears (implicit
    row when account-less); any additional provider seen in the table appears
    after them, alphabetically."""
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for account in list_accounts(db_path):
        provider = str(account.get("provider") or limit_state.DEFAULT_PROVIDER)
        by_provider.setdefault(provider, []).append(account)

    extra = sorted(p for p in by_provider if p not in _KNOWN_PROVIDERS)
    rows: list[dict[str, Any]] = []
    for provider in (*_KNOWN_PROVIDERS, *extra):
        accounts = by_provider.get(provider, [])
        if not accounts:
            rows.append(_implicit_provider_row(provider))
            continue
        # Default-first, then enabled-first, then oldest — matches the accounts
        # page's own ordering intent within a provider group.
        accounts.sort(
            key=lambda a: (
                not bool(a.get("is_default")),
                not bool(a.get("enabled")),
                str(a.get("created_at") or ""),
            )
        )
        rows.extend(_provider_health_row(account, db_path) for account in accounts)
    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_swarm_fleet(swarm_dal: SwarmDalDep) -> dict[str, Any]:
    """Fleet list: every run with progress counts + live utilization.

    Matches the dashboard's ``SwarmFleet`` shape (``features/swarm/types.ts``,
    C1 scaffold): a flat ``runs`` array whose terminal statuses let the UI
    distinguish completed, failed, and cancelled board groups, plus
    ``utilization`` for the live subset. "Active" =
    planning/running/merging (holding fleet capacity); "queued" =
    admission-controlled, waiting for capacity to free (plan doc "Fleet
    capacity model"). ``utilization.active_sessions`` is a swarm-only stopgap
    (see :meth:`SwarmDal.live_attempt_count`) until WP2's cross-lane
    ``limit_state.py`` ledger exists; ``max_concurrent_swarms`` is the same
    placeholder constant as the budget ceiling, pending ``configs/swarm.yaml``.
    """
    all_runs = swarm_dal.list_runs()
    queued_count = sum(1 for run in all_runs if run["status"] == "queued")
    active_count = sum(1 for run in all_runs if run["status"] in _ACTIVE_STATUSES)
    return {
        "runs": [_run_summary(swarm_dal, run) for run in all_runs],
        "utilization": {
            "active_sessions": swarm_dal.live_attempt_count(),
            "active_swarms": active_count,
            "queued_runs": queued_count,
            "max_concurrent_swarms": MAX_CONCURRENT_SWARMS,
        },
    }


def _run_swarm_job(  # noqa: PLR0913 -- background-task positional contract
    job_id: str,
    brief: str,
    resolved_working_dir: str,
    requested_budget: float | None,
    params: dict[str, Any] | None,
    project_id: str | None,
    store: Any,
    db_path: str,
    planner_llm: Any,
    clarify_llm: Any,
    recall_fn: Any,
) -> None:
    try:
        from omniagentos.swarm.dal import SwarmDal

        swarm_dal = SwarmDal(db_path)
        planner_fns = _wp4_planner_functions()
        if planner_fns is None:
            raise RuntimeError("swarm planner is not available")
        plan_bundles_fn, provision_run_fn = planner_fns

        plans = list(
            plan_bundles_fn(
                brief,
                str(resolved_working_dir),
                planner_llm=planner_llm,
                clarify_llm=clarify_llm,
                dal=swarm_dal,
                recall_fn=recall_fn,
            )
        )
        try:
            plan = _select_safe_plan(plans, working_dir=str(resolved_working_dir))
        except Exception as decision_exc:
            # Typed pre-run refusal: zero side effects; surface disposition on the job.
            from omniagentos.api.services import ApiError

            if isinstance(decision_exc, ApiError):
                with _SWARM_JOBS_LOCK:
                    _SWARM_JOBS[job_id] = {
                        "status": "error",
                        "error": decision_exc.message,
                        "code": decision_exc.code,
                        "detail": decision_exc.detail,
                    }
                return
            raise
        try:
            assert_plan_safe_for_provision(plan, workspace_dir=str(resolved_working_dir))
            budget = swarm_run_budget(len(plan.tasks), requested_budget)
            provisioned = provision_run_fn(
                plan,
                dal=swarm_dal,
                working_dir=str(resolved_working_dir),
                project_id=project_id,
                budget_usd_max=budget,
                params=params,
            )
        except PlanSafetyError as safety_exc:
            status, code, message, detail = _plan_decision_error(safety_exc.decision)
            with _SWARM_JOBS_LOCK:
                _SWARM_JOBS[job_id] = {
                    "status": "error",
                    "error": message,
                    "code": code,
                    "detail": detail,
                    "http_status": status,
                }
            return
        run_row = provisioned["run"]
        StoreSwarmEmitter(store).emit(
            str(run_row["id"]),
            ACTION_PLAN_CREATED,
            {"task_count": len(plan.tasks), "parallelism_ratio": plan.parallelism_ratio},
        )

        if plan.formation is not None:
            try:
                from omniagentos.formation.telemetry import record_selection

                conn = swarm_dal._connection
                form = plan.formation
                fingerprint = getattr(plan, "plan_hash", None) or getattr(plan, "fingerprint", None)
                record_selection(
                    conn,
                    task_id=str(run_row.get("id") or provisioned.get("root_card_id")),
                    goal=brief,
                    arm="formation",
                    formation_id=form.id,
                    confidence=form.confidence or 1.0,
                    low_confidence=bool(getattr(form, "low_confidence", False)),
                    topology=form.topology or "monitor_and_repair",
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
                    task_fingerprint=fingerprint,
                )
            except Exception:
                LOG.warning(
                    "formation_selections production write failed during background swarm job",
                    exc_info=True,
                )

        try:
            _activate_run_safely(str(run_row["id"]), plan, str(resolved_working_dir))
        except PlanSafetyError as safety_exc:
            status, code, message, detail = _plan_decision_error(safety_exc.decision)
            with _SWARM_JOBS_LOCK:
                _SWARM_JOBS[job_id] = {
                    "status": "error",
                    "error": message,
                    "code": code,
                    "detail": detail,
                    "http_status": status,
                    "swarm_run_id": run_row["id"],
                }
            return
        except Exception:
            LOG.exception("swarm run activation hook failed for %s", run_row["id"])

        with _SWARM_JOBS_LOCK:
            _SWARM_JOBS[job_id] = {
                "status": "ready",
                "swarm_run_id": run_row["id"],
                "board_task_id": provisioned["root_card_id"],
            }
    except Exception as exc:
        LOG.exception("swarm job %s failed", job_id)
        with _SWARM_JOBS_LOCK:
            _SWARM_JOBS[job_id] = {
                "status": "error",
                "error": str(exc) or "planning failed",
            }


@router.get("/jobs/{job_id}")
def get_swarm_job_status(job_id: str) -> dict[str, Any]:
    """Poll a background swarm creation job."""
    with _SWARM_JOBS_LOCK:
        job = _SWARM_JOBS.get(job_id)
    if job is None:
        fail(404, "not_found", "swarm job not found", {"job_id": job_id})
    return job


@router.post("", status_code=202)
def create_swarm_run(
    body: CreateSwarmRunRequest,
    store: StoreDep,
    swarm_dal: SwarmDalDep,
    planner_llm: SwarmPlannerLLMDep,
    clarify_llm: SwarmClarifyLLMDep,
    recall_fn: SwarmRecallFnDep,
    background_tasks: BackgroundTasks,
    sync: int | None = None,
) -> dict[str, Any]:
    """Plan a swarm run from a brief/spec and a working dir, and provision it.

    ``project_id`` (M1, optional) scopes the run and every card it provisions.
    Omitted means UNSCOPED (NULL) — the pre-M1 behaviour of this route, and the
    only honest answer when the caller did not say which project the work is
    for. A named project must exist (404 otherwise).
    """
    brief = (body.brief or body.spec or "").strip()
    if not brief:
        fail(400, "validation", "brief or spec is required")
    resolved_working_dir = _validate_working_dir(body.working_dir)
    requested_budget = _validate_budget(body.budget_usd_max)
    # Resolved on the REQUEST thread, before the background job is queued: an
    # unknown project must be a 404 the caller sees, not a job that fails later.
    project_id = _validate_project_id(store, body.project_id)

    if sync != 1:
        job_id = f"swarmjob_{uuid.uuid4().hex[:20]}"
        with _SWARM_JOBS_LOCK:
            _SWARM_JOBS[job_id] = {"status": "running"}
        background_tasks.add_task(
            _run_swarm_job,
            job_id,
            brief,
            str(resolved_working_dir),
            requested_budget,
            body.params,
            project_id,
            store,
            swarm_dal.db_path,
            planner_llm,
            clarify_llm,
            recall_fn,
        )
        return {"job_id": job_id, "status": "running"}

    planner_fns = _wp4_planner_functions()
    if planner_fns is None:
        fail(503, "unavailable", "swarm planner is not available")
    plan_bundles_fn, provision_run_fn = planner_fns

    plans = list(
        plan_bundles_fn(
            brief,
            str(resolved_working_dir),
            planner_llm=planner_llm,
            clarify_llm=clarify_llm,
            dal=swarm_dal,
            recall_fn=recall_fn,
        )
    )
    plan = _select_safe_plan(plans, working_dir=str(resolved_working_dir))
    try:
        assert_plan_safe_for_provision(plan, workspace_dir=str(resolved_working_dir))
        budget = swarm_run_budget(len(plan.tasks), requested_budget)
        provisioned = provision_run_fn(
            plan,
            dal=swarm_dal,
            working_dir=str(resolved_working_dir),
            project_id=project_id,
            budget_usd_max=budget,
            params=body.params,
        )
    except PlanSafetyError as safety_exc:
        _fail_plan_decision(safety_exc.decision)
    run_row = provisioned["run"]
    StoreSwarmEmitter(store).emit(
        str(run_row["id"]),
        ACTION_PLAN_CREATED,
        {"task_count": len(plan.tasks), "parallelism_ratio": plan.parallelism_ratio},
    )

    if plan.formation is not None:
        try:
            from omniagentos.formation.telemetry import record_selection

            conn = swarm_dal._connection
            form = plan.formation
            fingerprint = getattr(plan, "plan_hash", None) or getattr(plan, "fingerprint", None)
            record_selection(
                conn,
                task_id=str(run_row.get("id") or provisioned.get("root_card_id")),
                goal=brief,
                arm="formation",
                formation_id=form.id,
                confidence=form.confidence or 1.0,
                low_confidence=bool(getattr(form, "low_confidence", False)),
                topology=form.topology or "monitor_and_repair",
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
                task_fingerprint=fingerprint,
            )
        except Exception:
            LOG.warning(
                "formation_selections production write failed during synchronous swarm job",
                exc_info=True,
            )

    try:
        _activate_run_safely(str(run_row["id"]), plan, str(resolved_working_dir))
    except PlanSafetyError as safety_exc:
        _fail_plan_decision(safety_exc.decision)
    except Exception:
        LOG.exception("swarm run activation hook failed for %s", run_row["id"])
    return {"swarm_run_id": run_row["id"], "board_task_id": provisioned["root_card_id"]}


@router.get("/overview")
def get_swarm_overview(swarm_dal: SwarmDalDep, store: StoreDep) -> dict[str, Any]:
    """Fleet-level metric rollup for the Command Center's header + throughput tiles.

    Aggregates across every ACTIVE run (planning/running/merging) — active count,
    done/total progress, an est-completion, the throughput block (speedup vs the
    frozen manual estimate, terminal utilization, tasks/hour, rate-limit delays
    avoided, a coarse health string), the in-flight tasks that feed the kanban's
    phase overlay (``{task_id, phase, session_id, assignment_reason}``), and the
    summed budget. Per-run detail stays on ``GET /api/swarm/{id}``.

    Registered BEFORE ``GET /{run_id}`` so the literal ``/overview`` path is never
    captured as a run id. Empty fleet returns the same envelope with zeroed
    numbers, an empty ``tasks`` list, ``est_completion_at``/``budget.cap_usd``
    null, and ``health="idle"``.
    """
    return _build_overview(swarm_dal, store)


@router.get("/team")
def get_swarm_team(swarm_dal: SwarmDalDep, store: StoreDep) -> dict[str, Any]:
    """Live team board: who is running, who was spawned, leader updates.

    Cheap observability (attempt rows + recent swarm.event), not a token stream.
    Registered BEFORE ``GET /{run_id}`` so ``team`` is never captured as an id.
    """
    return _build_team(swarm_dal, store)


@router.get("/providers")
def get_swarm_providers(swarm_dal: SwarmDalDep) -> list[dict[str, Any]]:
    """Per provider/account durable rate-limit, cooldown and inflight health.

    The ``ProviderHealthPanel``'s primary source (C4): a flat list with one row
    per registered account plus one implicit row per account-less known provider
    (``_KNOWN_PROVIDERS``). Each row carries ``{provider, account_id,
    display_name, status, cooldown_until, reset_in_seconds, active_sessions,
    max_inflight, status_detail}`` — ``active_sessions`` and ``reset_in_seconds``
    come from WP2's :mod:`omniagentos.routing.limit_state` (durable inflight +
    remaining cooldown), the single cross-lane authority, rather than a naive
    session scan. Reads the SAME database this route's ``SwarmDal`` is bound to
    (``swarm_dal.db_path``) so tests inject one tmp DB for all of it.

    Registered BEFORE ``GET /{run_id}`` so the literal ``/providers`` path is
    never captured as a run id. The dashboard falls back to ``GET /api/accounts``
    only on a 404 (route absent); once this route exists the panel prefers it.
    """
    return _build_providers(swarm_dal.db_path)


@router.get("/{run_id}")
def get_swarm_run(run_id: str, swarm_dal: SwarmDalDep) -> dict[str, Any]:
    """Run detail: the run row + its member tasks, DAG edges, and attempts.

    ``tasks`` excludes the run's own root/container card — see
    :func:`_member_tasks`.
    """
    run = swarm_dal.get_run(run_id)
    if run is None:
        fail(404, "not_found", "swarm run not found", {"id": run_id})

    status = run.get("status")
    if status in TERMINAL_RUN_STATUSES:
        try:
            conn = swarm_dal._connection
            row = conn.execute(
                "SELECT id, outcome FROM formation_selections WHERE task_id = ? AND outcome = 'predicted'",
                (run_id,),
            ).fetchone()
            if row:
                from omniagentos.contracts import utc_now_iso

                outcome = (
                    "accepted"
                    if status == "completed"
                    else ("cancelled" if status == "cancelled" else "rejected")
                )
                conn.execute(
                    "UPDATE formation_selections SET outcome = ?, finished_at = ? WHERE id = ?",
                    (outcome, utc_now_iso(), row["id"]),
                )

            board_task_id = run.get("board_task_id")
            if board_task_id and board_task_id != run_id:
                board_row = conn.execute(
                    "SELECT id FROM formation_selections WHERE task_id = ? AND outcome = 'predicted'",
                    (board_task_id,),
                ).fetchone()
                if board_row:
                    from omniagentos.contracts import utc_now_iso

                    outcome = (
                        "accepted"
                        if status == "completed"
                        else ("cancelled" if status == "cancelled" else "rejected")
                    )
                    conn.execute(
                        "UPDATE formation_selections SET outcome = ?, finished_at = ? WHERE id = ?",
                        (outcome, utc_now_iso(), board_row["id"]),
                    )
        except Exception:
            LOG.warning(
                "Could not update formation_selection outcome on get_swarm_run", exc_info=True
            )

    tasks = _member_tasks(swarm_dal, run)
    attempts = {str(task["id"]): swarm_dal.list_attempts(str(task["id"])) for task in tasks}
    return {
        "run": dict(run),
        "tasks": tasks,
        "deps": swarm_dal.deps_for_run(run_id),
        "attempts": attempts,
        "progress": _task_progress(tasks),
        "metrics": _parse_json_field(run.get("metrics_json"), default={}),
    }


@router.get("/{run_id}/activity")
def get_swarm_activity(
    run_id: str,
    swarm_dal: SwarmDalDep,
    store: StoreDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[dict[str, Any]]:
    """Chronological ``swarm.event`` rows for one run, paginated by event id.

    ``after`` is an event-id cursor (not a timestamp): pass the highest ``id``
    already seen to fetch only newer rows — the same replay-by-id idiom the
    frozen SSE contract uses for ``GET /api/events?after_id=`` (``contracts/
    events.md``), just under this route's own ``after`` query param name.
    """
    run = swarm_dal.get_run(run_id)
    if run is None:
        fail(404, "not_found", "swarm run not found", {"id": run_id})
    event_store = cast(_SwarmEventStore, store)
    rows = event_store.get_events_for_target("swarm_run", run_id, after_id=after, limit=limit)
    return [_serialize_event(row) for row in rows]


# ---------------------------------------------------------------------------
# Cancel's session fanout.
#
# The scheduler already kills a cancelled run's sessions when it notices the
# status flip (``_handle_external_terminalization``), and it remains the SOLE
# owner of worker lifecycle — respawn, attempt finalization and worktree
# cleanup all stay there. What it cannot cover is the case where NOBODY is
# polling: a run whose coordinator died, was displaced, or never started still
# has live sessions bound to open attempts, and nothing will ever look at them
# again. That is the seam this closes, and it is deliberately the narrow one —
# signal + verify, no attempt closing, no respawn decisions.
# ---------------------------------------------------------------------------

# "This process is already gone." Derived from the sessions DAL's own
# vocabulary instead of re-listed here, so a terminal state added there can
# never silently read as "still live" on the kill path.
_TERMINAL_SESSION_STATES: frozenset[str] = frozenset(
    str(state.value) for state in TERMINAL_SESSION_STATES
)

# The attribution an OPERATOR cancel must carry. This is not cosmetic: the
# longhaul engine's D7 discrimination is a blocklist — only ``operator`` and
# ``cancel_requested`` mean "superseded", and ANY other killer (including the
# scheduler's own ``swarm-cancelled``) is treated as a crash worth respawning.
# Cancelling through ``request_cancel`` is what makes the stop button stick.
_CANCEL_ATTRIBUTION = "cancel_requested"


class _SessionKiller(Protocol):
    """The three sessions-DAL methods cancel needs, and nothing else.

    ``request_cancel`` is preferred over ``request_kill``: it records the
    operator attribution (see :data:`_CANCEL_ATTRIBUTION`), upgrades a kill a
    reaper already requested, and is idempotent for a session that is already
    cancel-requested. ``orchestrator_session_run`` is the durable ownership
    binding written at spawn time; the fanout refuses to signal any session it
    cannot prove belongs to the run being cancelled (fail closed — a killer
    that cannot answer the ownership question gets no kills at all).
    """

    def get_session(self, session_id: str) -> dict[str, Any] | None: ...

    def request_cancel(self, session_id: str) -> bool: ...

    def orchestrator_session_run(self, session_id: str) -> str | None: ...


def get_swarm_session_killer() -> _SessionKiller:
    """The sessions DAL cancel signals through (overridable in tests).

    Imported lazily from ``routes.sessions`` rather than constructing a second
    ``SessionsDal``: that module owns the process-lifetime instance (one
    connection, one activity-write coalesce map — T-OPS-006/DR-008), and a
    module-level import here would close an import cycle, since it reaches
    back into this module for ``get_swarm_dal``.
    """
    from omniagentos.api.routes.sessions import get_sessions_dal

    return cast("_SessionKiller", get_sessions_dal())


SessionKillerDep = Annotated[_SessionKiller, Depends(get_swarm_session_killer)]


def _live_run_sessions(swarm_dal: SwarmDal, run_id: str) -> tuple[list[str], list[str]]:
    """``(session ids, unbound attempt ids)`` for this run's still-open attempts.

    Run-scoped by construction: ``attempts_with_usage`` filters on
    ``swarm_attempts.swarm_run_id``, so another run's session cannot be
    reached from here even if it shares a board task, a worktree or a
    provider account.

    An attempt with no ``session_id`` is reported separately rather than
    dropped — it is a spawn caught mid-flight, the one live thing cancel can
    neither name nor signal, and silence about it is exactly the failure this
    route is being fixed for.
    """
    session_ids: list[str] = []
    unbound: list[str] = []
    seen: set[str] = set()
    for attempt in swarm_dal.attempts_with_usage(run_id):
        if attempt.get("ended_at") is not None:
            continue
        session_id = attempt.get("session_id")
        if not session_id:
            unbound.append(str(attempt.get("id")))
            continue
        if str(session_id) not in seen:
            seen.add(str(session_id))
            session_ids.append(str(session_id))
    return session_ids, unbound


def _verify_dead(killer: _SessionKiller, session_id: str) -> tuple[str, str]:
    """Read back what actually happened to ``session_id``: ``(bucket, reason)``.

    Deliberately ignores ``request_cancel``'s return value. A boolean from the
    write path is a claim about the write; only the row says what the session
    is doing. And the row's ``kill_requested`` flag is itself only a REQUEST:
    the one fact that means "this process is dead" is a terminal ``state``,
    which only the supervisor's kill/terminalize path writes. Anything short
    of that is at best ``kill_pending`` — durably signalled, death not yet
    confirmed — and ``kill_pending`` can never count as a completed kill
    (fail closed: unknown is not dead).
    """
    try:
        row = killer.get_session(session_id)
    except Exception as exc:  # noqa: BLE001 -- an unverifiable kill is a failed kill
        LOG.warning("cancel: could not re-read session %s", session_id, exc_info=True)
        return "failed", f"could not verify: {type(exc).__name__}"
    if row is None:
        return "failed", "session row missing — cancellation unverifiable"
    state = str(row.get("state") or "")
    if state in _TERMINAL_SESSION_STATES:
        # The supervisor set a terminal session state after our request:
        # confirmed process death, the only fact "cancelled" may report.
        return "cancelled", ""
    if row.get("kill_requested") and str(row.get("killed_by") or "") == _CANCEL_ATTRIBUTION:
        return (
            "kill_pending",
            "cancel durably recorded; supervisor has not yet confirmed process death",
        )
    return (
        "failed",
        f"cancel not durably recorded (state={state!r}, "
        f"kill_requested={row.get('kill_requested')!r}, killed_by={row.get('killed_by')!r})",
    )


def _refuse_unowned(killer: _SessionKiller, run_id: str, session_id: str) -> str | None:
    """The F002 ownership gate: a refusal reason, or None when owned by ``run_id``.

    ``swarm_attempts.session_id`` has no FK and no ownership constraint — a
    stale, corrupt or mis-bound attempt row can name ANY session. The durable
    owner is ``orchestrator_sessions.run_id``, written at spawn time, and it
    must equal the run being cancelled BEFORE any kill request goes out.
    Unknown is treated exactly like a mismatch: a killer without the binding
    method, a lookup that raises, a missing row or a NULL run_id all refuse —
    never kill a session you cannot prove is yours.
    """
    owner_of = getattr(killer, "orchestrator_session_run", None)
    if not callable(owner_of):
        return "ownership unverifiable: killer exposes no orchestrator_session_run binding"
    try:
        owner = owner_of(session_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable owner is not a proven owner
        LOG.warning("cancel: ownership lookup failed for %s", session_id, exc_info=True)
        return f"ownership lookup raised {type(exc).__name__}"
    if owner is None:
        return "no durable owner recorded (orchestrator_sessions has no run binding)"
    if str(owner) != run_id:
        return f"durable owner is run {owner!r}, not this run — attempt row is mis-bound"
    return None


def _cancel_run_sessions(
    swarm_dal: SwarmDal, killer: _SessionKiller, run_id: str
) -> dict[str, Any]:
    """Signal every live session of ``run_id`` and verify each one is DEAD."""
    outcome: dict[str, Any] = {
        "cancelled": [],
        "already_terminal": [],
        "kill_pending": [],
        "failed": [],
        "not_owned": [],
        "unbound_attempts": [],
    }
    try:
        session_ids, unbound = _live_run_sessions(swarm_dal, run_id)
    except Exception as exc:  # noqa: BLE001 -- a scan we could not finish is not an empty scan
        LOG.warning("cancel: live-session scan failed for run %s", run_id, exc_info=True)
        outcome["scan_error"] = f"{type(exc).__name__}: {exc}"
        return {"sessions": outcome, "kill_complete": False}

    outcome["unbound_attempts"] = unbound
    for session_id in session_ids:
        refusal = _refuse_unowned(killer, run_id, session_id)
        if refusal is not None:
            LOG.warning(
                "cancel: refusing to signal session %s for run %s: %s",
                session_id,
                run_id,
                refusal,
            )
            outcome["not_owned"].append({"session_id": session_id, "reason": refusal})
            continue
        try:
            row = killer.get_session(session_id)
        except Exception as exc:  # noqa: BLE001 -- unreadable pre-state is unverifiable
            LOG.warning("cancel: could not read session %s", session_id, exc_info=True)
            outcome["failed"].append(
                {"session_id": session_id, "reason": f"could not verify: {type(exc).__name__}"}
            )
            continue
        if row is None:
            outcome["failed"].append(
                {
                    "session_id": session_id,
                    "reason": "session row missing — cancellation unverifiable",
                }
            )
            continue
        if str(row.get("state") or "") in _TERMINAL_SESSION_STATES:
            outcome["already_terminal"].append(session_id)
            continue
        try:
            killer.request_cancel(session_id)
        except Exception as exc:  # noqa: BLE001 -- one bad session must not spare the rest
            LOG.warning("cancel: request_cancel failed for %s", session_id, exc_info=True)
            outcome["failed"].append(
                {"session_id": session_id, "reason": f"request_cancel raised {type(exc).__name__}"}
            )
            continue
        bucket, reason = _verify_dead(killer, session_id)
        if bucket == "failed":
            outcome["failed"].append({"session_id": session_id, "reason": reason})
        else:
            outcome[bucket].append(session_id)

    kill_complete = not (
        outcome["failed"]
        or outcome["not_owned"]
        or outcome["kill_pending"]
        or outcome["unbound_attempts"]
    )
    return {"sessions": outcome, "kill_complete": kill_complete}


def _unstopped_warning(outcome: dict[str, Any]) -> str:
    """The human-readable half of a partial cancel — names names, never counts alone."""
    parts: list[str] = []
    if outcome.get("scan_error"):
        parts.append(f"could not enumerate live sessions ({outcome['scan_error']})")
    if outcome["failed"]:
        named = ", ".join(str(item["session_id"]) for item in outcome["failed"])
        parts.append(f"could not stop session(s): {named}")
    if outcome["not_owned"]:
        named = ", ".join(str(item["session_id"]) for item in outcome["not_owned"])
        parts.append(f"refused to signal session(s) not provably owned by this run: {named}")
    if outcome["kill_pending"]:
        named = ", ".join(str(item) for item in outcome["kill_pending"])
        parts.append(f"process death not yet confirmed for session(s): {named}")
    if outcome["unbound_attempts"]:
        named = ", ".join(str(item) for item in outcome["unbound_attempts"])
        parts.append(f"attempt(s) mid-spawn with no session to signal: {named}")
    return "run marked cancelled but " + "; ".join(parts) + " — verify manually"


def _cancel_event_payload(outcome: dict[str, Any]) -> dict[str, Any]:
    """``run_failed`` payload for a cancel.

    Stays byte-identical to the historical ``{"reason": "cancelled"}`` when the
    run had nothing running — the common case keeps its frozen shape — and
    grows the fanout facts only when there is something to report.
    """
    payload: dict[str, Any] = {"reason": "cancelled"}
    touched = (
        outcome["cancelled"]
        or outcome["already_terminal"]
        or outcome["kill_pending"]
        or outcome["failed"]
        or outcome["not_owned"]
        or outcome["unbound_attempts"]
        or outcome.get("scan_error")
    )
    if not touched:
        return payload
    payload["sessions_cancelled"] = len(outcome["cancelled"])
    payload["sessions_already_terminal"] = len(outcome["already_terminal"])
    if outcome["kill_pending"]:
        payload["sessions_kill_pending"] = [str(item) for item in outcome["kill_pending"]]
    if outcome["failed"]:
        payload["sessions_unstopped"] = [str(item["session_id"]) for item in outcome["failed"]]
    if outcome["not_owned"]:
        payload["sessions_not_owned"] = [str(item["session_id"]) for item in outcome["not_owned"]]
    if outcome["unbound_attempts"]:
        payload["attempts_unbound"] = list(outcome["unbound_attempts"])
    if outcome.get("scan_error"):
        payload["scan_error"] = outcome["scan_error"]
    return payload


class SwarmCancelFailure(BaseModel):
    """One session the fanout could not (or refused to) stop, and why."""

    session_id: str
    reason: str


class SwarmCancelSessions(BaseModel):
    """Per-session outcome buckets for a cancel's kill fanout.

    ``cancelled`` means CONFIRMED DEAD — a terminal session state was read
    back off the row after the request, i.e. the supervisor finished the
    process. ``kill_pending`` means the cancel is durably recorded but death
    is not yet confirmed; it always makes ``kill_complete`` false.
    ``not_owned`` are sessions the fanout REFUSED to signal because the
    durable ownership binding (``orchestrator_sessions.run_id``) did not
    prove they belong to this run. ``scan_error`` is set only when the
    attempt scan itself failed — the live set is then unknown, which is
    never reported as empty.
    """

    cancelled: list[str] = []
    already_terminal: list[str] = []
    kill_pending: list[str] = []
    failed: list[SwarmCancelFailure] = []
    not_owned: list[SwarmCancelFailure] = []
    unbound_attempts: list[str] = []
    scan_error: str | None = None


class SwarmCancelResponse(BaseModel):
    """The cancel route's declared contract — ``kill_complete`` is mandatory."""

    id: str
    status: str
    kill_complete: bool
    sessions: SwarmCancelSessions
    warning: str | None = None


@router.post("/{run_id}/cancel", response_model_exclude_none=True)
def cancel_swarm_run(
    run_id: str,
    swarm_dal: SwarmDalDep,
    store: StoreDep,
    killer: SessionKillerDep,
) -> SwarmCancelResponse:
    """Cancel a run: flip its status AND stop the sessions it still has running.

    Idempotent: cancelling an already-terminal run is not a 409 — it returns
    the current status (mirrors ``intake.routes.cancel_board_task``). It is
    NOT, however, a no-op: the session fanout below runs on both paths, so a
    second press of Stop re-checks (and re-signals) anything still alive
    instead of reporting a success it did not re-verify. A run stamped
    terminal with live sessions still bound to open attempts is a leak, and
    the only way to learn about it is to look.

    Order is load-bearing. The status flip happens FIRST so nothing new can be
    scheduled or spawned (slot workers re-check run status after claim and
    before spawn), and only then is the live set read and signalled — an
    attempt opened in between is caught by this read; one opened after it is
    caught by the status the flip already wrote.

    The flip itself is a CAS (``SwarmDal.cancel_run_if_active``): predicate
    and write are one UPDATE, so of N concurrent cancels exactly one wins the
    terminal transition and only the winner emits the ``run_failed`` receipt.
    Losers still run the fanout and report truthfully — they just never
    duplicate the irreversible side effects.

    Scope is by ``swarm_attempts.swarm_run_id`` AND the durable ownership
    binding: every candidate session must have ``orchestrator_sessions.run_id``
    equal to this run before any kill request goes out. A mis-bound attempt
    row is refused and reported (``sessions.not_owned``), never signalled.

    The response is :class:`SwarmCancelResponse` (machine-declared in the
    OpenAPI contract). ``kill_complete`` is false whenever anything might
    still be running that this call did not verifiably stop: a session whose
    process death the supervisor has not yet confirmed (``kill_pending`` — a
    durable kill REQUEST is not a dead process), a session it could not
    signal or verify (``failed``), one it refused as unowned (``not_owned``),
    a live attempt with no session id to name (``unbound_attempts``), or an
    attempt scan that failed (``scan_error``). ``sessions.cancelled`` means a
    terminal state was read back off the session row — confirmed process
    death, never merely that the request was recorded. ``warning`` is present
    only when ``kill_complete`` is false and names every stray.
    """
    run = swarm_dal.get_run(run_id)
    if run is None:
        fail(404, "not_found", "swarm run not found", {"id": run_id})
    was_terminal = str(run["status"]) in {str(status) for status in TERMINAL_RUN_STATUSES}
    won_transition = False
    if not was_terminal:
        won_transition = swarm_dal.cancel_run_if_active(run_id, error="cancelled by operator")

    # Status is terminal now, so this read sees a set that can only shrink.
    sessions = _cancel_run_sessions(swarm_dal, killer, run_id)
    kill_complete = bool(sessions["kill_complete"])
    outcome = cast(dict[str, Any], sessions["sessions"])
    if not kill_complete:
        LOG.error(
            "swarm run %s cancelled but %d session(s) unconfirmed-dead, %d failed, "
            "%d refused as unowned and %d unbound attempt(s) remain: %s",
            run_id,
            len(outcome["kill_pending"]),
            len(outcome["failed"]),
            len(outcome["not_owned"]),
            len(outcome["unbound_attempts"]),
            outcome,
        )

    if won_transition:
        StoreSwarmEmitter(store).emit(run_id, ACTION_RUN_FAILED, _cancel_event_payload(outcome))
        try:
            conn = swarm_dal._connection
            row = conn.execute(
                "SELECT id FROM formation_selections WHERE task_id = ? AND outcome = 'predicted'",
                (run_id,),
            ).fetchone()
            if row:
                from omniagentos.contracts import utc_now_iso

                conn.execute(
                    "UPDATE formation_selections SET outcome = 'cancelled', finished_at = ? WHERE id = ?",
                    (utc_now_iso(), row["id"]),
                )

            board_task_id = run.get("board_task_id")
            if board_task_id and board_task_id != run_id:
                board_row = conn.execute(
                    "SELECT id FROM formation_selections WHERE task_id = ? AND outcome = 'predicted'",
                    (board_task_id,),
                ).fetchone()
                if board_row:
                    from omniagentos.contracts import utc_now_iso

                    conn.execute(
                        "UPDATE formation_selections SET outcome = 'cancelled', finished_at = ? WHERE id = ?",
                        (utc_now_iso(), board_row["id"]),
                    )
        except Exception:
            pass
    if not was_terminal:
        # Losers of the CAS race re-read too: the winner already flipped the
        # row, and reporting the pre-race status would claim the run is still
        # active moments after a 200 said it was cancelled.
        run = swarm_dal.get_run(run_id) or run
    return SwarmCancelResponse(
        id=run_id,
        status=str(run["status"]),
        kill_complete=kill_complete,
        sessions=SwarmCancelSessions.model_validate(outcome),
        warning=None if kill_complete else _unstopped_warning(outcome),
    )


__all__ = [
    "SessionKillerDep",
    "SwarmDalDep",
    "get_swarm_dal",
    "get_swarm_session_killer",
    "router",
]
