"""Intake loop services: clarify a rough request, dispatch it, reconcile the board.

Three connected pieces:

* ``clarify_intake`` — runs a cheap LLM (the Claude CLI adapter by default) to either
  return 1-3 targeted clarifying questions or, once it has enough (or the round budget
  is spent), a refined task spec. Never loops: bounded by ``MAX_CLARIFY_ROUNDS`` and
  degrades to a deterministic heuristic when the LLM is unavailable.
* ``dispatch_spec`` — turns a confirmed spec into BOTH a live board card
  (``board_tasks``, status open) AND a control-plane task+run (so a runner's agent
  actually executes it), linked via ``board_tasks.run_id`` (migration 016).
* ``reconcile_board`` — the live-kanban projection: maps each linked run's state onto
  its board card's column (To-Do / In-Progress / Done) and enriches each row with the
  agent working it, so a dispatched task MOVES as the runner claims and completes it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import (
    TERMINAL_RUN_STATES,
    ActionClass,
    Events,
    HarnessType,
    RunState,
    Store,
    default_db_path,
    new_id,
    utc_now_iso,
)
from omniagentos.intake.contracts import (
    MAX_CLARIFY_ROUNDS,
    ClarifyResult,
    ClarifyTurn,
    RefinedSpec,
)
from omniagentos.intake.fable import FABLE_MODEL, run_fable_json
from omniagentos.intake.fastlane import classify_task_lane, target_and_todo_prompt
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.notifications.service import record_notification
from omniagentos.path_containment import inode_paths_equal
from omniagentos.policy import PolicyConfig
from omniagentos.provision import ProvisionLLM, ProvisionResult, provision_project
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.simgate import SimContext

if TYPE_CHECKING:
    from omniagentos.orchestrator.contracts import OrchestrationCheckpoint, ResumeState

LOG = logging.getLogger(__name__)

_ACTIVE_CONDUCTORS: dict[str, tuple[threading.Thread, threading.Lock]] = {}
_ACTIVE_CONDUCTORS_LOCK = threading.RLock()
_RESUME_START_LOCK = threading.Lock()
_MAX_RESUME_CONDUCTORS = 4

_DELIVERABLES_INSTRUCTION = (
    "Deliverables: write final output files into the `outputs/` folder inside your working "
    "directory (it already exists). Only write elsewhere if the user explicitly names a "
    "destination. Files a user gave you are in `uploads/`."
)
_ORCHESTRATION_DIED_MARKER = " [auto-blocked: orchestrator died]"

# The default harness a dispatched intake task runs on. cli-claude is the sensible
# default (the Anthropic subscription CLI); override per-request or via env for a
# mock dry-run or a different lane.
DEFAULT_INTAKE_HARNESS = os.environ.get("OMNIAGENTOS_INTAKE_HARNESS", HarnessType.CLI_CLAUDE.value)

# REAL-HARNESS ARMING SWITCH -- DEFAULT OFF, ON PURPOSE.
#
# The default queued dispatch is ``readonly``: a text-only generation with no
# tools and no scoped working dir. A goal filed today therefore plans, gets a
# board card and a run, and then nothing real happens. This env var is the
# operator's explicit opt-in to closing that last mile: when it is armed, a
# dispatch that WOULD have queued a text-only run instead queues the existing
# ``tools`` posture (scoped working dir + _INTAKE_TOOLS), whose single agent
# step is declared ``consequential`` -- so the runner's EXISTING policy gate
# still decides whether a human approves before any tool runs. Arming widens
# what a run may do; it never weakens that gate.
#
# Unset (or 0/false/no/off/empty) -> every code path below behaves exactly as it
# did before this flag existed. Autonomous execution costs money, so only an
# explicit affirmative arms it, and the runner re-checks the flag in its OWN
# environment before executing an armed run (see runner/core.py) -- disarming
# stops queued work too.
REAL_HARNESS_ENV = "OMNIAGENTOS_REAL_HARNESS"
_REAL_HARNESS_TRUTHY = frozenset({"1", "true", "yes", "on"})


def real_harness_enabled() -> bool:
    """True only when the operator has explicitly armed real-harness execution.

    Read at CALL time (never cached at import) so arming/disarming takes effect
    for the next dispatch without restarting the API.
    """
    return os.environ.get(REAL_HARNESS_ENV, "").strip().lower() in _REAL_HARNESS_TRUTHY


# Synthetic health checks must still receive a card because the dispatch pipeline
# updates it by id later, but they must not accumulate on the operator's board.
_PREARCHIVE_GOALS = frozenset({"probe", "__proxytest__ do nothing"})

# Phase 1: registry-backed project routing for quick intake (A1). Tri-state,
# DEFAULT off -> today's quick-dispatch behavior byte-identically (the registry
# is never consulted, no clarify gate, no build_preflight). "shadow" computes and
# LOGS the project-registry routing decision without applying it. "enforce"
# applies it: a registry hit dispatches through the matched project's
# root_dirs[0]; a genuine miss keeps today's scratch-project creation.
QUICK_PROJECT_ROUTING_ENV = "OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE"
_QUICK_ROUTING_MODES = ("off", "shadow", "enforce")


def quick_project_routing_mode() -> str:
    """The tri-state ``OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE`` gate (off default)."""
    mode = os.environ.get(QUICK_PROJECT_ROUTING_ENV, "off").strip().lower()
    return mode if mode in _QUICK_ROUTING_MODES else "off"


# A clarify LLM turns a prompt + JSON schema into the model's parsed JSON object,
# or None if the model was unavailable / produced nothing usable.
ClarifyLLM = Callable[[str, dict[str, Any]], dict[str, Any] | None]

_CLARIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["mode"],
    "properties": {
        "mode": {"type": "string", "enum": ["questions", "spec"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "spec": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "suggested_discipline": {"type": "string"},
                "suggested_priority": {"type": "string"},
                "required_expertise": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


def _clarify_prompt(draft: str, history: Sequence[ClarifyTurn], *, final: bool) -> str:
    lines = [
        "You are the CLARIFYING INTAKE AGENT for OmniAgentOS, a multi-agent operations",
        "system. An operator has given a rough request. Your job is to refine it into a",
        "precise, executable task spec for another agent to work.",
        "",
        f"OPERATOR'S ROUGH REQUEST:\n{draft.strip() or '(empty)'}",
    ]
    if history:
        lines.append("\nPRIOR CLARIFYING Q&A:")
        for turn in history:
            lines.append(f"- Q: {turn.q}\n  A: {turn.a}")
    lines += [
        "",
        "Decide ONE of:",
        '(A) You still need specifics. Return mode="questions" with 1-3 TARGETED',
        "    questions. Ask only about what genuinely blocks writing the spec:",
        "    the concrete outcome, the acceptance criteria, the project/vertical,",
        "    and which agent/lane should handle it. Do not ask what you can infer.",
        '(B) You have enough. Return mode="spec" with a refined spec: a crisp title,',
        "    a description of the outcome, 2-4 acceptance_criteria, a",
        "    suggested_discipline (short tag) and suggested_priority",
        "    (low|normal|high|urgent), and required_expertise tags.",
    ]
    if final:
        lines += [
            "",
            'IMPORTANT: This is the FINAL round. You MUST return mode="spec" now.',
            "Do not ask any more questions; synthesize the best spec from what you have.",
        ]
    return "\n".join(lines)


def default_clarify_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Run the clarify prompt through Fable at effort MEDIUM (via the Claude CLI).

    The clarifying/intake agent is Fable at effort medium — light reasoning to turn
    a rough request into questions or a spec. Both the model and the effort stay
    overridable via ``OMNIAGENTOS_INTAKE_MODEL`` / ``OMNIAGENTOS_INTAKE_EFFORT`` so an
    operator can retune without a code change. Never raises: an unavailable CLI or an
    unusable response returns None so ``clarify_intake`` falls back to its heuristic.
    No API key is hardcoded — the subscription CLI carries its own auth.
    """
    return run_fable_json(
        prompt,
        schema,
        model=os.environ.get("OMNIAGENTOS_INTAKE_MODEL", FABLE_MODEL),
        effort=os.environ.get("OMNIAGENTOS_INTAKE_EFFORT", "medium"),
        max_turns=2,
        wall_ms=120_000,
    )


def _heuristic_questions() -> list[str]:
    return [
        "What is the concrete outcome you want when this is done?",
        "How will we know it is complete — what are the acceptance criteria?",
        "Which project/vertical is this for, and which agent or lane should handle it?",
    ]


def _heuristic_spec(draft: str, history: Sequence[ClarifyTurn]) -> RefinedSpec:
    first_line = next((ln.strip() for ln in draft.splitlines() if ln.strip()), draft.strip())
    title = (first_line or "Intake task")[:120]
    parts = [draft.strip()]
    criteria: list[str] = []
    for turn in history:
        if turn.a.strip():
            parts.append(f"{turn.q.strip()} {turn.a.strip()}".strip())
            criteria.append(turn.a.strip())
    description = "\n\n".join(p for p in parts if p)
    if not criteria:
        criteria = [f"Delivers on: {title}"]
    return RefinedSpec(
        title=title,
        description=description,
        acceptance_criteria=criteria[:4],
        suggested_discipline=None,
        suggested_priority="normal",
    )


def _parse_llm_clarify(
    raw: dict[str, Any], draft: str, history: Sequence[ClarifyTurn]
) -> ClarifyResult:
    mode = str(raw.get("mode", "")).strip().lower()
    if mode == "spec":
        spec_raw = raw.get("spec")
        if not isinstance(spec_raw, dict) or not str(spec_raw.get("title", "")).strip():
            spec = _heuristic_spec(draft, history)
        else:
            spec = RefinedSpec.model_validate(spec_raw).normalized()
        return ClarifyResult(mode="spec", spec=spec)
    questions_raw = raw.get("questions")
    questions = (
        [str(q).strip() for q in questions_raw if str(q).strip()]
        if isinstance(questions_raw, list)
        else []
    )
    if not questions:
        questions = _heuristic_questions()
    return ClarifyResult(mode="questions", questions=questions[:3])


def clarify_intake(
    draft: str,
    history: Sequence[ClarifyTurn] | Sequence[dict[str, Any]] | None = None,
    *,
    llm: ClarifyLLM | None = None,
) -> ClarifyResult:
    """One clarify turn: questions, or (once ready / round budget spent) a spec.

    ``history`` is the prior answered Q&A. The number of answered rounds bounds the
    loop: at ``MAX_CLARIFY_ROUNDS`` the agent is forced to emit a spec regardless of
    what the model would prefer, so intake can never spin.
    """
    turns: list[ClarifyTurn] = [
        item if isinstance(item, ClarifyTurn) else ClarifyTurn.model_validate(item)
        for item in (history or [])
    ]
    rounds = len(turns)
    # Phase 1 (OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE=enforce): quick intake's
    # registry routing gets exactly ONE clarify round on low confidence, not the
    # full MAX_CLARIFY_ROUNDS budget — after one answered round the agent must
    # synthesize a spec on its best judgment. "off" keeps today's behavior and
    # "shadow" only observes/logs routing, so neither applies the shorter loop.
    final = rounds >= MAX_CLARIFY_ROUNDS or (
        quick_project_routing_mode() == "enforce" and rounds >= 1
    )
    runner = llm or default_clarify_llm

    raw = runner(_clarify_prompt(draft, turns, final=final), _CLARIFY_SCHEMA)
    if raw is None:
        # LLM unavailable — deterministic fallback keeps the loop working offline.
        result = (
            ClarifyResult(mode="spec", spec=_heuristic_spec(draft, turns))
            if final or rounds >= 1
            else ClarifyResult(mode="questions", questions=_heuristic_questions())
        )
    else:
        result = _parse_llm_clarify(raw, draft, turns)

    if final and result.mode != "spec":
        # Round budget spent: force a spec no matter what the model returned.
        result = ClarifyResult(
            mode="spec",
            spec=result.spec or _heuristic_spec(draft, turns),
            forced=True,
        )
    if result.mode == "spec" and result.spec is not None:
        result.spec = result.spec.normalized()
    result.round = rounds
    return result


def _resolve_discipline(store: Store, suggested: str | None) -> str | None:
    """Map a free-form suggested discipline onto a real discipline id, else None.

    ``board_tasks.discipline`` is free text (kept for display/filtering), but the
    control-plane task's ``discipline_id`` must reference a real discipline or be
    NULL — so an invented tag is dropped rather than tripping a 404.
    """
    if not suggested:
        return None
    needle = suggested.strip().lower()
    for row in store.list_disciplines():
        if str(row.get("id", "")).lower() == needle or str(row.get("name", "")).lower() == needle:
            return str(row["id"])
    return None


def _compose_description(spec: RefinedSpec) -> str:
    body = spec.description.strip()
    if spec.acceptance_criteria:
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria)
        body = f"{body}\n\nAcceptance criteria:\n{criteria}".strip()
    return body


def _compose_prompt(spec: RefinedSpec) -> str:
    lines = [spec.title.strip()]
    if spec.description.strip():
        lines.append(spec.description.strip())
    if spec.acceptance_criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {c}" for c in spec.acceptance_criteria)
    lines.append(_DELIVERABLES_INSTRUCTION)
    return "\n\n".join(lines)


_SESSION_MODEL_TIERS: dict[str, tuple[str, str]] = {
    "simple": ("OMNIAGENTOS_SESSION_MODEL_SIMPLE", "haiku"),
    "standard": ("OMNIAGENTOS_SESSION_MODEL_STANDARD", "sonnet"),
    "complex": ("OMNIAGENTOS_SESSION_MODEL_COMPLEX", FABLE_MODEL),
}

# FAST DISPATCH (PKG-FAST-DISPATCH): each solo_* gate decision maps to the
# session-model band it fast-lanes onto (via _SESSION_MODEL_TIERS above).
FAST_DISPATCH_ENV = "OMNIAGENTOS_FAST_DISPATCH"
_FAST_DISPATCH_BAND: dict[str, str] = {
    "solo_fast": "simple",
    "solo_standard": "standard",
    "solo_complex": "complex",
}


def fast_dispatch_enabled() -> bool:
    """True when the three-gate FAST DISPATCH classifier is switched on.

    Default OFF -> byte-identical dispatch behavior (the gate never runs). Copies
    the swarm scheduler's env-flag idiom (``swarm_execute_enabled``)."""
    return os.environ.get(FAST_DISPATCH_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _fast_dispatch_brief(spec: RefinedSpec) -> str:
    """The FULL spec text the gate classifies: title + description + acceptance.

    A risk term in ANY field (a benign-looking title with 'delete production
    database' buried in the acceptance criteria) must reach the risk gate, so the
    classifier never sees the description alone."""
    parts: list[str] = [spec.title or "", spec.description or ""]
    parts.extend(spec.acceptance_criteria or [])
    return "\n".join(part for part in parts if part).strip()


def _estimate_session_complexity(goal: str) -> str:
    # Lazy to avoid the service <-> planner import cycle during API initialisation.
    from omniagentos.intake.planner import estimate_complexity

    return estimate_complexity(goal)


def _resolve_session_model(goal: str, explicit_model: str | None) -> tuple[str, str]:
    """Return the complexity band and model for one session dispatch."""
    try:
        estimated_band = _estimate_session_complexity(goal)
    except Exception:  # noqa: BLE001 -- an uncertain estimate must bias up to sonnet.
        estimated_band = "unknown"
    band = str(estimated_band).strip().lower()
    tier = _SESSION_MODEL_TIERS.get(band)

    if explicit_model is not None:
        return band, explicit_model.strip()
    if tier is None:
        return band or "unknown", "sonnet"

    env_name, default_model = tier
    return band, os.environ.get(env_name, default_model).strip() or default_model


def _prepare_working_dir(working_dir: str) -> None:
    root = Path(working_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)


def _sqlite_db_path(store: Any) -> str:
    connection = getattr(store, "_connection", None)
    if connection is None:
        return default_db_path()
    try:
        for row in connection.execute("PRAGMA database_list").fetchall():
            if str(row[1]) == "main":
                return str(row[2]) or ":memory:"
    except Exception:  # noqa: BLE001 -- non-SQLite test seams use the configured DB.
        LOG.debug("intake could not resolve store database path", exc_info=True)
    return default_db_path()


def _planned_coding_task(spec: RefinedSpec) -> bool:
    text = " ".join(
        (
            str(spec.suggested_discipline or ""),
            spec.title,
            spec.description,
        )
    ).lower()
    return any(
        token in text
        for token in (
            "coding",
            "codebase",
            "software",
            "developer",
            "engineering",
            "implement",
            "repository",
            "repo ",
        )
    )


def _resolve_dispatch_lane(requested: str | None, mode: str, spec: RefinedSpec) -> str | None:
    lane = str(requested or "auto").strip().lower()
    if lane in {"fast", "longhaul"}:
        return lane
    if lane != "auto":
        raise ValueError("lane must be auto, fast, or longhaul")
    if mode == "orchestrate" and _planned_coding_task(spec):
        resolved = classify_task_lane(
            f"{spec.title}\n{spec.description}",
            requested="auto",
            discipline=spec.suggested_discipline,
        )
        if resolved == "longhaul":
            return resolved
    return None


def _set_board_routing(
    db_path: str,
    board_task_id: str,
    *,
    lane: str | None,
    category: str | None,
) -> str | None:
    """Persist additive lane/category fields after the frozen BoardTask insert."""

    from omniagentos.longhaul.store import LonghaulStore

    longhaul = LonghaulStore(db_path)
    try:
        category_id: str | None = None
        if category and category.strip():
            found = longhaul.get_category(category.strip())
            if found is None:
                found = longhaul.create_category(category.strip())
            category_id = str(found["id"])
            longhaul.set_task_category(board_task_id, category_id)
        if lane is not None:
            longhaul.set_lane(board_task_id, lane)
        if lane == "longhaul":
            longhaul._connection.execute(
                "UPDATE board_tasks SET status = 'pending', updated_at = ? WHERE id = ?",
                (utc_now_iso(), board_task_id),
            )
            longhaul._connection.commit()
        return category_id
    finally:
        longhaul.close()


def refined_spec_from_plan(plan: Any, goal: str) -> RefinedSpec:
    """Collapse a planner result into the single spec used by quick orchestration.

    The front door deliberately does not ask an operator to inspect or confirm the
    plan.  It still uses the plan's title, description, and task-level acceptance
    criteria to give the Orchestrator a concrete, bounded starting point.
    """
    planned_tasks = list(getattr(plan, "tasks", []) or [])
    for sub_project in list(getattr(plan, "sub_projects", []) or []):
        planned_tasks.extend(list(getattr(sub_project, "tasks", []) or []))

    criteria: list[str] = []
    for task in planned_tasks:
        for criterion in list(getattr(task, "acceptance_criteria", []) or []):
            text = str(criterion).strip()
            if text and text not in criteria:
                criteria.append(text)

    first_task = planned_tasks[0] if planned_tasks else None
    title = str(getattr(plan, "project_name", "") or "").strip()
    description = str(getattr(plan, "description", "") or "").strip()
    if not title and first_task is not None:
        title = str(getattr(first_task, "title", "") or "").strip()
    if not description and first_task is not None:
        description = str(getattr(first_task, "description", "") or "").strip()

    return RefinedSpec(
        title=title or goal.strip() or "Intake task",
        description=description or goal.strip(),
        acceptance_criteria=criteria or [f"Delivers on: {title or goal.strip()}"],
        suggested_discipline=(
            str(getattr(first_task, "suggested_discipline", "") or "").strip() or None
        ),
        suggested_priority=(
            str(getattr(first_task, "suggested_priority", "normal") or "normal").strip()
        ),
    ).normalized()


def create_pending_plan_card(collab_store: CollabStore, spec: RefinedSpec) -> dict[str, Any]:
    """Surface a planned-but-not-dispatched goal on the board.

    ``pending`` is intentionally not claimable (only ``open`` is), so creating a
    plan never accidentally starts work before the operator promotes it.
    """
    spec = spec.normalized()
    board = BoardTask(
        title=spec.title,
        description=_compose_description(spec),
        required_expertise=list(spec.required_expertise),
        discipline=spec.suggested_discipline,
        priority=spec.suggested_priority,
        status=BoardTaskStatus.PENDING,
    )
    collab_store.create_board_task(board)
    _prearchive_suppressed_card(collab_store, board.id, spec.title)
    _emit_board_event(collab_store, board.id)
    return collab_store.get_board_task(board.id) or {"id": board.id, "status": "pending"}


def persist_card_intake_directives(
    store: Any,
    board_task_id: str,
    *,
    execute: str | None = None,
    speed: str | None = None,
) -> None:
    """Record the Mode dial's execute/speed on a board card (D10, F1).

    Persisted ADDITIVELY under ``board_tasks.longhaul_json["intake"]`` — an
    existing per-card JSON blob, deliberately NO new columns — so the dial
    survives the gap between a Plan-first pending card and its later approval.
    The forward contract: ANY approval-time dispatch of a pending plan card
    MUST read these directives and thread them into :func:`dispatch_spec`
    (``execute="single"`` HARD-suppresses the auto solo-vs-swarm upgrade;
    speed maps at the API edge). Cards without the key keep today's auto
    behavior exactly; the longhaul engine's own keys are untouched (its
    dispatch ``update()``s the dict, preserving this one).
    """
    if execute is None and speed is None:
        return
    from omniagentos.longhaul.store import LonghaulStore

    longhaul = LonghaulStore(_sqlite_db_path(store))
    try:
        state = longhaul.get_longhaul_json(board_task_id) or {}
        intake = dict(state.get("intake") or {})
        if execute is not None:
            intake["execute"] = execute
        if speed is not None:
            intake["speed"] = speed
        state["intake"] = intake
        longhaul.set_longhaul_json(board_task_id, state)
    finally:
        longhaul.close()


def create_queued_goal_card(
    collab_store: CollabStore, goal: str, orchestration_run_id: str
) -> dict[str, Any]:
    """Create the immediately visible placeholder for a background quick dispatch."""
    title = next((line.strip() for line in goal.splitlines() if line.strip()), goal.strip())[:120]
    board = BoardTask(
        title=title or "Intake task",
        description="Planning the work…",
        status=BoardTaskStatus.OPEN,
        result_ref=orchestration_run_id,
    )
    collab_store.create_board_task(board)
    _prearchive_suppressed_card(collab_store, board.id, goal)
    _emit_board_event(collab_store, board.id, run_id=orchestration_run_id)
    return collab_store.get_board_task(board.id) or {"id": board.id, "status": "open"}


def _is_prearchived_goal(goal: str) -> bool:
    return goal.strip().casefold() in _PREARCHIVE_GOALS


def _prearchive_suppressed_card(collab_store: CollabStore, task_id: str, goal: str) -> None:
    """Pre-archive health-probe cards without skipping their required creation."""
    if _is_prearchived_goal(goal):
        collab_store.update_board_task(task_id, {"archived_at": utc_now_iso()})


# Per-dispatch execution posture. ``readonly`` (the default) keeps the historical
# safe behaviour: the run has NO tool grants, so its agent can only generate text
# (read_only sandbox, auto-approved). ``tools`` makes the run able to do REAL work
# -- read/write files and run shell inside a scoped working dir -- but ONLY after
# the runner's existing policy gate parks it for human approval (see below).
# ``session`` skips the runner-queue lane entirely: the task is launched as a live,
# monitored Claude Code session via the Session Bridge (``SessionSupervisor.spawn``)
# instead of a queued run -- see the ``mode == "session"`` branch in dispatch_spec.
# ``orchestrate`` (default OFF) hands the spec to the OmniAgentOS Orchestrator
# (``omniagentos.orchestrator.run_orchestration``): the "worker-as-planner" loop that
# plans -> writes a spec -> spawns tiered executors with injected context ->
# approve-safe/escalate-hard -> quality-gates + iterates -> learns. It is a thin
# passthrough here (a library call); the existing readonly/tools/session modes are
# untouched -- see the ``mode == "orchestrate"`` branch in dispatch_spec.
# ``swarm`` (WP10) plans the goal with the Swarm Mode planner (bundles included)
# and provisions swarm run(s) -- an EXECUTE MODE, never a lane value (the frozen
# 043 lane CHECK is fast/longhaul only), so its branch runs BEFORE
# ``_resolve_dispatch_lane``. ``auto`` is the solo-vs-swarm auto-decision: plan
# first, and when the plan is a single solo bundle fall through to the EXISTING
# orchestrate path with zero behavior change (parallelism must pay before swarm
# is chosen). Explicit lanes (fast/longhaul) keep absolute priority over both.
# ``auto`` is also the DEFAULT decision path: an orchestrate dispatch with no
# explicit lane upgrades to it when OMNIAGENTOS_SWARM_EXECUTE is on and the
# working dir is a git checkout (see ``_swarm_auto_default_applies``).
# ``single`` (D10 Mode dial) is orchestrate with that auto-default upgrade
# HARD-suppressed — the swarm planner never runs, whatever the flags say.
ExecuteMode = Literal["readonly", "tools", "session", "orchestrate", "swarm", "auto", "single"]


class OrchestrateRunner(Protocol):
    """The slice of ``orchestrator.run_orchestration`` the dispatch path needs.

    Matches the real entry point structurally so it satisfies this Protocol with no
    adapter, while tests inject a lightweight stub and never run a real orchestration.
    """

    def __call__(
        self,
        goal: str,
        *,
        priority: str = ...,
        pins: dict[str, Any] | None = ...,
        working_dir: str | None = ...,
        project_id: str | None = ...,
        run_id: str | None = ...,
        granted_roots: list[str] | None = ...,
        checkpoint: OrchestrationCheckpoint | None = ...,
        resume_state: ResumeState | None = ...,
    ) -> Any: ...


class SessionSpawner(Protocol):
    """The slice of ``SessionSupervisor`` a session-mode dispatch needs.

    Matches ``omniagentos.sessions.supervisor.SessionSupervisor.spawn`` exactly so
    the real class satisfies this Protocol structurally with no adapter, while
    tests inject a lightweight stub/mock and never touch the real Session Bridge
    (no real ``claude`` process, no real sessions DB) -- see
    ``dispatch_spec(..., session_spawner=...)``.
    """

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> str: ...


# Lazily-constructed, process-lifetime SessionSupervisor (T-OPS-006 / DR-008, the
# same reasoning as ``omniagentos.api.routes.sessions.get_sessions_dal``): a fresh
# SessionSupervisor per dispatch would open a fresh SessionsDal connection each
# time, defeating its activity-write coalesce and multiplying SQLite writer
# contention -- which matters here specifically because a multi-task Fable plan
# dispatches N sessions back-to-back (see ``intake.planner.provision_plan``, which
# calls ``dispatch_spec`` once per task). Constructing the real SessionSupervisor
# is deferred to first use so importing this module never pulls in the Session
# Bridge's subprocess/policy machinery for callers who never dispatch in "session"
# mode.
_SESSION_SPAWNER: SessionSpawner | None = None
_SESSION_SPAWNER_LOCK = threading.Lock()


def _default_session_spawner() -> SessionSpawner:
    global _SESSION_SPAWNER
    if _SESSION_SPAWNER is None:
        with _SESSION_SPAWNER_LOCK:
            if _SESSION_SPAWNER is None:
                from omniagentos.sessions.supervisor import SessionSupervisor

                _SESSION_SPAWNER = cast(SessionSpawner, SessionSupervisor())
    return cast(SessionSpawner, _SESSION_SPAWNER)


def _record_failed_session(
    dal: SessionsDal,
    *,
    task_id: str,
    project_dir: str,
    model: str,
    error: str,
) -> str:
    """Create a visible terminal session when failure preceded spawner insertion."""
    session_id = new_id("ses")
    now = utc_now_iso()
    detail = error or "unknown session spawn failure"
    dal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": project_dir,
            "provider": "claude",
            "session_ref": "",
            "state": SessionState.FAILED.value,
            "model": model,
            "title": f"[FAILED] task {task_id}: {detail}",
            "budget_usd_max": None,
            "cost_usd": 0.0,
            "kill_requested": 0,
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    dal.record_session_error(session_id, detail)
    return session_id


# The tool grant a tools-mode dispatch carries. file_write/shell drive the
# adapter's workspace_write sandbox (configs/policy.yaml tools->sandbox table); the
# runner narrows nothing further because the agent step declares no step-level
# allowlist. These are workspace PRIMITIVES (no dotted connector capabilities), so
# they never reach the outside world -- only the run's own scoped working dir.
_INTAKE_TOOLS: list[str] = ["file_read", "file_write", "shell"]


def _intake_workspace_base() -> Path:
    """The parent dir for per-task scratch workspaces: ``<var>/intake-workspace``.

    Anchored to ``OMNIAGENTOS_VAR_DIR`` when set (same knob the adapters use for
    their log root), else the repo's ``var/`` dir -- never the process cwd or an
    unscoped home dir (council INT-003)."""
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if not base:
        import omniagentos

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
        base = os.path.join(repo_root, "var")
    return Path(base) / "intake-workspace"


def _resolve_project_dir(store: Store, project_id: str) -> str | None:
    """First declared root dir of a project, or None if it has none / is unknown."""
    try:
        from omniagentos.projects import ProjectStore

        project = ProjectStore(cast(Any, store)).get_project(project_id)
    except Exception:  # noqa: BLE001 -- a projects backend fault must fall back to scratch.
        LOG.debug("intake could not resolve project dir for %s", project_id, exc_info=True)
        return None
    if not project:
        return None
    roots = project.get("root_dirs") or []
    if roots and str(roots[0]).strip():
        return str(roots[0])
    return None


def _swarm_auto_default_applies(store: Store, project_id: str | None) -> bool:
    """True when a DEFAULT orchestrate dispatch (no explicit lane) may upgrade
    to the WP10 auto solo-vs-swarm decision.

    Two guards, BOTH required — otherwise today's path runs byte-identically
    (the swarm planner never executes):

    * ``OMNIAGENTOS_SWARM_EXECUTE`` is on (``swarm_execute_enabled``, the same
      flag that gates run activation — auto-as-default is pointless while
      activation is provision-only);
    * the dispatch's workspace can satisfy the scheduler's git-checkout
      requirement (Phase 1 same-directory model snapshots/reverts via git).
      A project with a declared root qualifies only when that root has a
      ``.git``. A PROJECTLESS dispatch (the cockpit quick path) qualifies by
      construction: the swarm provision path creates its own managed workspace
      and git-initializes it (``ensure_git`` in
      ``_resolve_or_create_orchestration_project``). Declared roots are only
      ever checked, never git-mutated.

    Side-effect free on purpose: unlike the orchestrate path's
    ``_resolve_or_create_orchestration_project`` this never creates a project
    or a workspace — a failed guard leaves zero trace.
    """
    try:
        from omniagentos.swarm.scheduler import swarm_execute_enabled

        if not swarm_execute_enabled():
            return False
    except Exception:  # noqa: BLE001 -- a guard fault means legacy behavior, never a crash.
        LOG.debug("swarm auto-default flag check failed", exc_info=True)
        return False
    if not project_id:
        # Projectless quick/cockpit dispatch: the swarm provision path creates
        # its own managed workspace with ensure_git, so the git requirement is
        # met by construction. (Rootless projects below still fail the guard —
        # their managed workspace is only git-initialized on FORCED swarm.)
        return True
    project_dir = _resolve_project_dir(store, project_id)
    if not project_dir:
        return False
    try:
        # ``.git`` is a dir in a plain checkout and a FILE in a linked worktree;
        # exists() covers both.
        return (Path(project_dir) / ".git").exists()
    except OSError:
        return False


def _resolve_project_budget(store: Store, project_id: str) -> float | None:
    """A project's declared ``budget_usd`` cap, or None when unset/unknown.

    Used to scope a session-mode dispatch's ``budget_usd_max`` to the project it
    runs under -- never spawn a session with a wider budget than its own project
    declares.
    """
    try:
        from omniagentos.projects import ProjectStore

        project = ProjectStore(cast(Any, store)).get_project(project_id)
    except Exception:  # noqa: BLE001 -- a projects backend fault must not block dispatch.
        LOG.debug("intake could not resolve project budget for %s", project_id, exc_info=True)
        return None
    if not project:
        return None
    budget = project.get("budget_usd")
    if isinstance(budget, int | float) and not isinstance(budget, bool) and budget >= 0:
        return float(budget)
    return None


def _session_grant_base_dirs(sim_ctx: SimContext | None = None) -> list[str]:
    """Server-managed base dirs a re-resolved session grant may legitimately live under.

    Beyond the create-time-approved grant roots (``within_allowed_roots``), the
    dispatcher itself MANAGES ephemeral workspaces that are valid session write scope
    yet are NOT user-approved grant roots: per-project + intake workspaces under
    ``<var>`` (``var/projects/<id>/workspace``, ``var/intake-workspace/<task>``) and the
    orchestrator's ``mkdtemp(prefix="orch-")`` dirs under the system temp base. A grant
    that re-resolves under one of these is kept; one that re-resolves outside BOTH these
    bases and every approved grant root (a post-validation symlink retarget) is dropped.
    """
    from omniagentos.policy.dir_grants import resolve_grant_dir

    if sim_ctx is not None and sim_ctx.sim_mode:
        campaign_root = sim_ctx.campaign_root
        return [os.path.realpath(str(campaign_root))] if campaign_root is not None else []

    var_base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if not var_base:
        import omniagentos

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(omniagentos.__file__)))
        var_base = os.path.join(repo_root, "var")
    bases: list[str] = []
    for base in (var_base, tempfile.gettempdir()):
        resolved = resolve_grant_dir(base)
        if resolved and resolved not in bases:
            bases.append(resolved)
    return bases


def _resolve_session_granted_roots(
    store: Store, project_id: str | None, working_dir: str
) -> list[str]:
    """The project's granted write roots a session may honor, BEYOND ``working_dir``.

    P3: a session must honor its project's FULL granted scope -- every ``root_dir``
    + ``allowed_dir`` -- not just the single working dir. These are looked up
    SERVER-SIDE from the project store (each validated at project creation by
    ``validate_grant_dir``, or a server-computed managed workspace), then frozen
    onto the session row; the hook never trusts a path from its payload. Returns
    canonical realpaths, excluding ``working_dir`` (already the session's cwd
    scope) and, defensively, anything that is/engulfs a secret dir. No project (or
    no extra roots) -> ``[]`` == pre-P3 (project-dir-only) behavior.
    """
    if not project_id:
        return []
    try:
        from omniagentos.projects import ProjectStore

        project = ProjectStore(cast(Any, store)).get_project(project_id)
    except Exception:  # noqa: BLE001 -- a projects backend fault must narrow, never crash dispatch.
        LOG.debug("intake could not resolve granted roots for %s", project_id, exc_info=True)
        return []
    if not project:
        return []
    from omniagentos.simgate import SimGateError, resolve_sim_context

    try:
        sim_ctx = resolve_sim_context()
    except SimGateError as exc:
        LOG.warning("intake/session grants: sim gate refused: %s", exc)
        return []

    from omniagentos.policy.dir_grants import (
        allowed_grant_roots,
        references_secret_dir,
        resolve_grant_dir,
        within_allowed_roots,
    )

    working_real = resolve_grant_dir(working_dir)
    approved_roots = allowed_grant_roots(sim_ctx=sim_ctx)
    managed_bases = _session_grant_base_dirs(sim_ctx)
    roots: list[str] = []
    for raw in (*project.get("root_dirs", []), *project.get("allowed_dirs", [])):
        candidate = str(raw).strip()
        if not candidate:
            continue
        # Defense in depth: never hand a session a root that IS or ENGULFS a secret
        # dir, even though create-time validate_grant_dir already rejects such grants.
        if references_secret_dir(candidate):
            LOG.debug("intake dropped a secret-adjacent session grant root")
            continue
        resolved = resolve_grant_dir(candidate)
        # Safety (`is not False`): unknown identity cannot add another session grant root.
        if (
            not resolved
            or inode_paths_equal(resolved, working_real) is not False
            or resolved in roots
        ):
            continue
        # HARDEN (P3 dispatch symlink-retarget): resolve_grant_dir re-runs realpath, so
        # a grant recorded as an approved path at create-time but SYMLINK-RETARGETED
        # afterwards resolves to its NEW destination here. references_secret_dir (above)
        # already catches a retarget INTO a credential store; this additionally requires
        # the RE-RESOLVED destination to still sit within an approved grant root OR under
        # a server-managed base (per-project/intake workspaces under <var>, mkdtemp orch
        # dirs under the system temp base). A retarget that now points anywhere else
        # (e.g. /etc, another user's home) sits within none of these -> dropped. Roots
        # are still looked up SERVER-SIDE from the project store, never a hook payload.
        if not (
            within_allowed_roots(resolved, allow_roots=approved_roots)
            or within_allowed_roots(resolved, allow_roots=managed_bases)
        ):
            LOG.debug("intake dropped a session grant root that re-resolved out of scope")
            continue
        roots.append(resolved)
    # AUTO-APPROVE Phase 1: merge standing roots (Desktop, var/, …) so ordinary
    # work outside the project workspace does not park for approval.
    try:
        from omniagentos.policy.roots import merge_standing_roots

        roots = merge_standing_roots(roots, working_dir=working_dir)
    except Exception:  # noqa: BLE001 -- roots config must never crash dispatch
        LOG.debug("intake could not merge standing roots", exc_info=True)
    return roots


def _resolve_working_dir(store: Store, task_id: str, project_id: str | None) -> str:
    """The scoped dir a tools-mode run may write in.

    The task's project root when a project is given (real work lands in its repo),
    otherwise a freshly-created per-task scratch dir under ``<var>/intake-workspace``.
    NEVER an unscoped/home dir -- the adapter would otherwise fall back to the
    process cwd for ``--add-dir`` (adapters.common._working_dir)."""
    if project_id:
        project_dir = _resolve_project_dir(store, project_id)
        if project_dir:
            # Bind only the pack discovered from this project's registry root.
            # A process-wide brand-pack environment variable could leak one
            # project's constraints into another project and is intentionally
            # not consulted.
            try:
                from omniagentos.intake.brand_context import bind_project_brand

                bind_project_brand(
                    store,
                    project_id=project_id,
                    working_dir=project_dir,
                    scope=str(project_id),
                )
            except Exception:  # noqa: BLE001 -- optional brand context must not block dispatch.
                LOG.debug("project brand binding failed for %s", project_id, exc_info=True)
            return project_dir
    scratch = _intake_workspace_base() / task_id
    scratch.mkdir(parents=True, exist_ok=True)
    return str(scratch)


def _ensure_git_workspace(workspace_dir: Path) -> None:
    """Best-effort ``git init`` + one empty root commit for a MANAGED workspace.

    The swarm scheduler refuses non-git working dirs, and its snapshot /
    ownership-diff machinery needs a resolvable ``HEAD``, so a freshly created
    managed workspace gets a repo with a root commit up front. Only ever
    applied to workspaces THIS module creates — never to a project's declared
    root dirs. Failure degrades to today's non-git workspace; the scheduler's
    git refusal remains the backstop.
    """
    import subprocess

    try:
        if (workspace_dir / ".git").exists():
            return
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(workspace_dir),
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=OmniAgentOS",
                "-c",
                "user.email=omniagentos@localhost",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "workspace init",
            ],
            cwd=str(workspace_dir),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 -- degrade to a non-git workspace, never block dispatch.
        LOG.debug("git init failed for managed workspace %s", workspace_dir, exc_info=True)


def _resolve_or_create_orchestration_project(
    store: Store, spec: RefinedSpec, project_id: str | None, *, ensure_git: bool = False
) -> tuple[str, str]:
    """Resolve or create a project for orchestration, return (project_id, working_dir).

    When project_id is provided, resolve its root directory or use its managed workspace.
    When not provided, create an ephemeral project so files land in a project workspace
    (visible via the Files API) instead of a temp directory.

    ``ensure_git`` (the swarm provision path) git-initializes any MANAGED
    workspace this call touches — the swarm scheduler requires a git checkout.
    A project's declared root dir is returned untouched either way.

    Always returns (project_id, working_dir) where working_dir is guaranteed to be set.
    """
    from omniagentos.projects import ProjectStore as _ProjectStore
    from omniagentos.projects.activity import project_log_dir as _project_log_dir

    # If project_id is provided, try to use its declared root directory first
    if project_id:
        project_dir = _resolve_project_dir(store, project_id)
        if project_dir:
            return project_id, project_dir

        # Project exists but has no root_dir; use its managed workspace
        try:
            workspace = str(_project_log_dir(project_id).parent)
            workspace_dir = Path(workspace) / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            if ensure_git:
                _ensure_git_workspace(workspace_dir)
            return project_id, str(workspace_dir)
        except Exception as exc:  # noqa: BLE001
            LOG.debug(
                "could not resolve managed workspace for project %s: %s",
                project_id,
                exc,
                exc_info=True,
            )

    # No project given: create an orchestration project in the managed workspace
    # so files are discoverable via the Files API.
    try:
        proj_store = _ProjectStore(cast(Any, store))

        # Generate the id first so the project's persisted grant can name its
        # dedicated workspace rather than the broader managed-project parent.
        # The latter also holds logs and orchestration artifacts, which are not
        # deliverables for the Files tab.
        created_id = new_id("proj")
        managed_workspace_root = _project_log_dir(created_id).parent
        workspace_dir = managed_workspace_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        if ensure_git:
            _ensure_git_workspace(workspace_dir)

        # Create a scoped project for this orchestration.  ``workspace_dir`` is
        # deliberately the task output directory, not ``workspace`` (which is
        # the managed parent containing logs and uploads).
        new_project = proj_store.create_project(
            {
                "id": created_id,
                "name": f"Orchestration: {spec.title[:80]}",
                "root_dirs": [str(workspace_dir)],
                "kind": "scratch",  # portfolio redesign Phase A — not a durable project
            }
        )
        return str(new_project.get("id")), str(workspace_dir)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("orchestration project creation failed: %s", exc, exc_info=True)

    # Ultimate fallback: let orchestrator.core use temp directory
    # (should not normally reach here)
    return project_id or "", ""


def dispatch_spec(
    store: Store,
    collab_store: CollabStore,
    policy_cfg: PolicyConfig,
    spec: RefinedSpec,
    *,
    harness: str | HarnessType | None = None,
    project_id: str | None = None,
    execute: ExecuteMode | None = "readonly",
    provision: bool = False,
    provision_llm: ProvisionLLM | None = None,
    model: str | None = None,
    budget_usd_max: float | None = None,
    session_spawner: SessionSpawner | None = None,
    priority: str | None = None,
    pins: dict[str, Any] | None = None,
    orchestrate_runner: OrchestrateRunner | None = None,
    async_orchestrate: bool = False,
    board_task_id: str | None = None,
    orchestration_run_id: str | None = None,
    fast: bool = False,
    target_write_root: str | None = None,
    lane: Literal["auto", "fast", "longhaul"] | None = None,
    category: str | None = None,
    swarm_planner: Callable[..., Any] | None = None,
    speed: str | None = None,
) -> dict[str, Any]:
    """Create a live board card + control-plane task from a spec, then execute it.

    ``fast`` + ``target_write_root`` are the quick front door's superfast lane (only
    meaningful with ``execute="session"``): the session is spawned straight from the
    goal with NO planning pass, marked orchestrator-owned so its safe writes
    auto-approve (money/delete still escalate), and granted ``target_write_root`` as
    an extra sandbox write root so a "make a folder on my desktop" task actually lands
    in ``~/Desktop``. The prompt tells the agent the absolute target + to use
    TodoWrite for live progress.

    Returns ``{"board_task", "task_id", "run_id", "session_id", "execute",
    "working_dir", "project_id", "provisioned", "allowed_connectors"}``. The board
    card lands in the To-Do column (status open).

    ``execute`` selects how the task actually runs. ``None`` means unspecified and
    normalizes to ``"readonly"`` (same as the default string).

    * ``"readonly"`` (default) -- queued as a run with no tools; the agent only
      generates text. Safe and auto-approved, exactly as before.
    * ``"tools"`` -- queued as a run that carries file_read/file_write/shell and a
      scoped working dir, and its single agent step is declared ``consequential``
      so the runner's EXISTING policy gate parks it in AWAITING_APPROVAL
      (configs/policy.yaml: consequential.requires_approval + always_human) before
      any tool runs. Nothing here bypasses that gate -- opting into tools opts into
      a human approval. Both of the above link the board card to its run via
      ``board_tasks.run_id`` (migration 016).
    * ``"session"`` -- skips the runner queue entirely and launches a live,
      MONITORED Claude Code session via the Session Bridge
      (``SessionSupervisor.spawn``, injectable as ``session_spawner`` for tests),
      in the task's scoped project dir, with ``model`` (default tiered by estimated
      task complexity: Haiku/Sonnet/Fable for simple/standard/complex) and
      ``budget_usd_max`` (default the project's own declared ``budget_usd`` when
      dispatched under a project). The session's id
      is returned as ``"session_id"`` and recorded on the board card
      (``result_ref``) so it is trackable from the board and already shows up in
      the dashboard's Sessions area (the Session Bridge owns spawn/monitor/kill
      and its existing PreToolUse approval interception; this function only
      requests the spawn, never bypasses that gate). A multi-task Fable plan
      dispatched with ``execute="session"`` (via ``intake.planner.provision_plan``,
      which calls this function once per task) therefore spawns ONE monitored
      session PER task.
    * ``"swarm"`` (WP10) -- plans the goal with the Swarm Mode planner
      (``swarm.planner.plan_swarm_bundles``; injectable as ``swarm_planner`` for
      tests) and dispatches what parallelism pays for: a swarm-worthy plan is
      provisioned as a swarm run (root card + child DAG cards, ``lane`` NULL,
      membership via ``swarm_run_id``) and activated ONLY behind
      ``OMNIAGENTOS_SWARM_EXECUTE`` (flag off = provision-only); a brief bundling
      N unrelated asks is split -- each bundle dispatched independently as its
      own board card(s), solo bundles down the existing orchestrate path, swarm
      bundles as their own runs. Fleet admission applies: over
      ``max_concurrent_swarms`` the run provisions as ``queued`` (never blocks
      intake). A single solo plan falls through to the EXISTING orchestrate path
      with zero behavior change.
    * ``"auto"`` (WP10) -- the solo-vs-swarm auto-decision: identical to
      ``"swarm"`` (plan first; route to swarm only when the planner's own solo
      rule says parallelism pays, otherwise the plain orchestrate path). The
      solo rule is rate-limit-aware: LOW fleet headroom (few free swarm slots
      or most providers cooling -- ``swarm.planner.swarm_headroom``) raises
      the swarm bar per configs/swarm.yaml ``auto.low_headroom_*``, and the
      decision + inputs are recorded in ``plan_json.assumptions``.

    AUTO IS THE DEFAULT DECISION PATH for machine dispatches: an
    ``"orchestrate"`` dispatch that names NO explicit lane (the quick front
    door's planned lane, and any caller that left ``lane`` unset/"auto") is
    upgraded to ``"auto"`` so the orchestrator chooses swarm itself whenever
    parallelism pays -- but ONLY when ``OMNIAGENTOS_SWARM_EXECUTE`` is on AND
    the dispatch resolves to a git-checkout working dir (the swarm scheduler's
    requirement). Otherwise the orchestrate path runs byte-identically to
    before (the swarm planner never executes).

    Explicit lanes keep ABSOLUTE priority: ``lane="fast"``/``"longhaul"`` is
    never intercepted by swarm/auto (nor by the auto-default upgrade) -- those
    dispatches take the existing orchestrate path exactly as
    ``execute="orchestrate"`` would.

    ``execute="single"`` (D10 Mode dial: Single Task) is orchestrate with the
    auto-default upgrade HARD-suppressed -- however parallelizable the goal
    looks and whatever ``OMNIAGENTOS_SWARM_EXECUTE`` says, the swarm planner
    never runs. An omitted execute keeps today's auto-default behavior exactly
    (automation backcompat).

    ``speed`` (D10 Mode dial: fast|auto|ultra) is additive execution metadata:
    the speed->priority mapping happens at the API edge (the ``priority`` arg
    here already reflects it); this function only THREADS speed — into the
    swarm path so a provisioned run's ``plan_json`` records it and the swarm
    router can apply its tier floor, and onto the created task's input
    metadata for the readonly/tools/session lanes (the fastlane session
    records it durably, F2). ``None`` changes nothing.

    ``provision`` (implies ``tools``) runs the provisioning step first: it
    determines the working dir + connector APIs the goal needs, creates/attaches a
    scoped project with exactly that reach, and scopes the dispatched run's working
    dir + connectors to it -- the run can then reach ONLY the provisioned dirs and
    connector scopes (a run bound to nothing else narrows no further).
    """
    # Lazy on purpose — this is the edge of the intake import cycle
    # (intake.service -> api package -> api.main -> api.routes.intake ->
    # intake.service). At module level it crashes any process that imports
    # intake before the API package (the sessions daemon's stale-run sweep
    # died every pass with "cannot import name 'ClarifyLLM' from partially
    # initialized module"); at call time both modules are fully initialized.
    from omniagentos.api.services import (
        board_priority_to_run_priority,
        create_run_service,
        create_task_service,
    )

    spec = spec.normalized()
    harness_value = (
        harness.value if isinstance(harness, HarnessType) else (harness or DEFAULT_INTAKE_HARNESS)
    )
    _execute_normalized = str(execute or "readonly").strip().lower()
    # D10: "single" is the Mode dial's Single Task -- orchestrate, with the
    # auto solo-vs-swarm upgrade HARD-suppressed below. Omitting execute (or
    # naming "orchestrate") keeps today's auto-default behavior exactly.
    single_task = _execute_normalized == "single"
    mode: ExecuteMode
    if _execute_normalized == "tools":
        mode = "tools"
    elif _execute_normalized == "session":
        mode = "session"
    elif _execute_normalized == "orchestrate" or single_task:
        mode = "orchestrate"
    elif _execute_normalized in ("swarm", "auto"):
        mode = cast(ExecuteMode, _execute_normalized)
    else:
        mode = "readonly"

    # REAL-HARNESS UPGRADE (OMNIAGENTOS_REAL_HARNESS, default OFF). The queued
    # default posture is a text-only generation -- it plans and produces prose,
    # and nothing in the world changes. When the operator has ARMED real-harness
    # execution, that same dispatch queues the existing ``tools`` posture
    # instead: a scoped working dir + _INTAKE_TOOLS, with its single agent step
    # DECLARED consequential, so the runner's own policy gate is what decides
    # whether a human approves before the adapter runs. This adds reach, never
    # permission. Only the default/readonly posture is upgraded -- an explicit
    # tools/session/orchestrate/swarm/auto/single dispatch already stated its
    # intent and is never intercepted. Flag off -> this is one env read and the
    # dispatch continues byte-identically.
    real_harness = mode == "readonly" and real_harness_enabled()
    # NEVER CLAIM REAL EXECUTION ON THE MOCK HARNESS. The defect this lane
    # exists for is a fleet of runs recorded as harness='mock' -- runs that
    # planned, queued, and executed nothing. ``OMNIAGENTOS_INTAKE_HARNESS`` (or
    # an explicit ``harness="mock"``) can resolve this dispatch to the mock
    # adapter, and upgrading THAT to the tools posture would queue yet another
    # mock run while telling the operator real execution was armed. So the
    # upgrade is withheld, and the response says so explicitly rather than
    # silently doing nothing: arming is reported as blocked, with the reason.
    # The operator's harness choice is never overridden here -- that would be a
    # silent config override, which is worse than a loud no-op.
    real_harness_blocked: str | None = None
    if real_harness and harness_value == HarnessType.MOCK.value:
        real_harness = False
        real_harness_blocked = f"harness={HarnessType.MOCK.value}"
        LOG.warning(
            "%s is armed but this dispatch resolves to the %r harness; "
            "refusing to queue a mock run as real execution "
            "(set OMNIAGENTOS_INTAKE_HARNESS or pass an explicit harness)",
            REAL_HARNESS_ENV,
            HarnessType.MOCK.value,
        )
    if real_harness:
        mode = "tools"

    # Swarm auto-default: on the DEFAULT dispatch path the orchestrator makes
    # the solo-vs-swarm decision ITSELF. A plain orchestrate dispatch with NO
    # explicit lane (the machine default: the quick front door's planned lane
    # and any orchestrate dispatch that named no lane) upgrades to the WP10
    # "auto" branch below -- plan first; a solo plan falls through to this
    # very orchestrate path with zero behavior change. Guarded twice
    # (_swarm_auto_default_applies): OMNIAGENTOS_SWARM_EXECUTE must be on AND
    # the dispatch must resolve to a git-checkout working dir (the swarm
    # scheduler refuses non-git workspaces). Explicit fast/longhaul lanes keep
    # absolute priority (the branch below routes them straight to orchestrate);
    # every other explicit execute value (readonly/tools/session) never reaches
    # this upgrade. Flag off, no project, or a non-git root -> today's path,
    # byte-identically -- the swarm planner never runs.
    # execute="single" (D10) is the ONE hard suppress of this upgrade: the
    # Mode dial's Single Task must stay a single orchestration however
    # parallelizable the goal looks and whatever the env flag says.
    if (
        mode == "orchestrate"
        and not single_task
        and str(lane or "auto").strip().lower() == "auto"
        and _swarm_auto_default_applies(store, project_id)
    ):
        mode = "auto"

    # FAST DISPATCH (PKG-FAST-DISPATCH): flag-gated three-gate classifier at the
    # intake seam. Only auto/swarm-bound dispatches on the auto lane are eligible
    # (explicit fast/longhaul keep absolute priority and are never intercepted).
    # A solo_* verdict skips the expensive inline-Fable swarm planning entirely
    # and re-routes to the EXISTING session-spawn path at the matching model band;
    # swarm threads speed_hint into the existing speed seam; fallthrough leaves
    # today's path untouched. Every branch records one telemetry row. Flag OFF ->
    # this block never runs and behavior is byte-identical.
    #
    # A solo_* re-route DEFERS its telemetry + orchestration close to AFTER the
    # session spawns (below): the decision row then carries the real session id as
    # ref_id, and the pre-created queued orchestration row is terminal-closed
    # exactly like the swarm_result branch supersedes it.
    _fd_session_pending: dict[str, Any] | None = None
    if (
        fast_dispatch_enabled()
        and mode in ("swarm", "auto")
        and str(lane or "auto").strip().lower() not in ("fast", "longhaul")
    ):
        from omniagentos.dispatch import decide as _fd_decide
        from omniagentos.dispatch import record_decision as _fd_record

        # F1: classify the FULL spec text -- title + description + acceptance
        # criteria -- so a risk term in ANY field is caught (not description only).
        _fd_brief = _fast_dispatch_brief(spec)
        _fd = _fd_decide(_fd_brief)
        _fd_conn = getattr(store, "_connection", None)
        if _fd.decision in _FAST_DISPATCH_BAND:
            _fd_band = _FAST_DISPATCH_BAND[_fd.decision]
            _fd_env, _fd_default = _SESSION_MODEL_TIERS[_fd_band]
            model = os.environ.get(_fd_env, _fd_default).strip() or _fd_default
            mode = "session"
            LOG.info(
                "fast dispatch: %s -> session (band=%s model=%s, gate=%s conf=%.2f %.1fms)",
                _fd.decision,
                _fd_band,
                model,
                _fd.gate,
                _fd.confidence,
                _fd.latency_ms,
            )
            # Deferred: recorded AFTER spawn with ref_id=session_id (see below).
            _fd_session_pending = {"decision": _fd, "brief": _fd_brief}
        elif _fd.decision == "swarm":
            _fd_applied = False
            if _fd.speed_hint and not speed:
                speed = _fd.speed_hint
                _fd_applied = True
            _fd_record(_fd_conn, _fd, brief=_fd_brief, applied=_fd_applied, dispatch_kind="swarm")
        else:  # fallthrough (incl. risk_flagged) -> today's path, unchanged.
            _fd_record(_fd_conn, _fd, brief=_fd_brief, applied=False, dispatch_kind="swarm")

    # WP10: swarm is an EXECUTE MODE, not a lane value -- this branch runs
    # BEFORE _resolve_dispatch_lane (which raises on any lane outside
    # auto/fast/longhaul; the 043 lane CHECK is frozen and swarm membership is
    # swarm_run_id only). Explicit lanes keep absolute priority: a fast or
    # longhaul request is NEVER intercepted by swarm planning -- it takes the
    # existing orchestrate path exactly as execute="orchestrate" would.
    if mode in ("swarm", "auto"):
        if str(lane or "auto").strip().lower() in ("fast", "longhaul"):
            mode = "orchestrate"
        else:
            swarm_result = _dispatch_swarm(
                store,
                collab_store,
                spec,
                project_id=project_id,
                priority=priority,
                pins=pins,
                orchestrate_runner=orchestrate_runner,
                async_orchestrate=async_orchestrate,
                board_task_id=board_task_id,
                budget_usd_max=budget_usd_max,
                swarm_planner=swarm_planner,
                speed=speed,
            )
            if swarm_result is not None:
                # A pre-created orchestrations row (the quick front door's
                # planned lane creates one BEFORE dispatch) must not linger
                # queued once swarm run(s) superseded it -- the resume sweep
                # would conduct it as a phantom duplicate.
                if orchestration_run_id:
                    _close_superseded_orchestration(store, orchestration_run_id, swarm_result)
                return swarm_result
            # Single solo plan: fall through to the EXISTING orchestrate path
            # with zero behavior change (the planner's own solo rule already
            # decided sequential execution wins).
            mode = "orchestrate"

    effective_lane = _resolve_dispatch_lane(lane, mode, spec)

    if mode == "orchestrate" and effective_lane != "longhaul":
        # Thin passthrough to the Orchestrator library -- the "worker-as-planner" loop
        # owns planning, tiered executor spawns (its own monitored sessions), the
        # approve-safe/escalate gate, the quality gate and the learners. Nothing in the
        # readonly/tools/session lanes below is touched. A board card is created for
        # dashboard visibility and linked to the orchestration run.
        return _dispatch_orchestrate(
            store,
            collab_store,
            spec,
            project_id=project_id,
            priority=priority,
            pins=pins,
            orchestrate_runner=orchestrate_runner,
            async_orchestrate=async_orchestrate,
            board_task_id=board_task_id,
            orchestration_run_id=orchestration_run_id,
        )

    provisioned: ProvisionResult | None = None
    if provision:
        # Provisioning does REAL work in a scoped repo -> force tools mode so the
        # run is tool-capable AND parked behind the runner's approval gate.
        mode = "tools"
        provisioned = provision_project(store, spec, project_id=project_id, llm=provision_llm)
        project_id = provisioned.project_id

    # Reuse the caller's pre-created card when one was supplied -- the quick front
    # door creates an INSTANT placeholder card and returns its id, then dispatches in
    # the background, so a fast/session dispatch must update THAT card instead of
    # creating a duplicate. With no board_task_id (the direct API), create a fresh
    # card exactly as before. (The orchestrate mode consumes board_task_id earlier and
    # already returned, so this only affects readonly/tools/session.)
    existing_card = collab_store.get_board_task(board_task_id) if board_task_id else None
    if existing_card is not None:
        board_id = str(board_task_id)
        collab_store.update_board_task(
            board_id,
            {
                "title": spec.title,
                "description": _compose_description(spec),
                "discipline": spec.suggested_discipline,
                "priority": spec.suggested_priority,
                "status": BoardTaskStatus.OPEN.value,
            },
        )
    else:
        board = BoardTask(
            title=spec.title,
            description=_compose_description(spec),
            required_expertise=list(spec.required_expertise),
            discipline=spec.suggested_discipline,
            priority=spec.suggested_priority,
            status=BoardTaskStatus.OPEN,
            # M1: at CREATE, not by the follow-up UPDATE below. That update
            # swallows its own failures, so it could leave a card unscoped
            # without saying so; the INSERT cannot.
            project_id=project_id,
        )
        collab_store.create_board_task(board)
        board_id = board.id
        _prearchive_suppressed_card(collab_store, board_id, spec.title)

    # Chat v2 (P0-7): the card carries its project from creation — read-time
    # joins can't cover session-mode cards (no run links them to a project).
    # Still called: the reuse branch above PATCHes an existing card, which the
    # create-time stamp cannot reach.
    _persist_board_project_id(collab_store, board_id, project_id)

    database_path = _sqlite_db_path(store)
    category_id = _set_board_routing(
        database_path,
        board_id,
        lane=effective_lane,
        category=category,
    )

    # A provisioned run is scoped to EXACTLY the provisioned connectors: the task
    # grant is workspace primitives + those connector capabilities, and nothing
    # else. A plain tools dispatch carries only the workspace primitives.
    task_tools: list[str] | None = None
    if mode == "tools":
        task_tools = list(_INTAKE_TOOLS)
        if provisioned is not None:
            task_tools += list(provisioned.allowed_connectors)

    # D10 (F2): a caller-supplied dial speed is recorded on the task row
    # (additive input metadata, tasks.input_json) so the readonly/tools/session
    # lanes — the fastlane's spawned session in particular — carry it durably.
    # None (legacy callers) keeps the payload byte-identical.
    task_input: dict[str, Any] = {
        "text": spec.description,
        "source": "intake",
        "execute": mode,
    }
    if speed is not None:
        task_input["speed"] = speed
    task = create_task_service(
        store,
        policy_cfg,
        title=spec.title,
        discipline_id=_resolve_discipline(store, spec.suggested_discipline),
        project_id=project_id,
        input=task_input,
        acceptance={"criteria": spec.acceptance_criteria},
        tools_allowed=task_tools,
    )

    if effective_lane == "longhaul":
        from omniagentos.longhaul.config import load_config
        from omniagentos.longhaul.engine import LonghaulEngine
        from omniagentos.longhaul.store import LonghaulStore

        longhaul_working_dir = _resolve_working_dir(store, str(task["id"]), project_id)
        _prepare_working_dir(longhaul_working_dir)
        longhaul_store = LonghaulStore(database_path)
        state = longhaul_store.get_longhaul_json(board_id) or {}
        state.update(
            {
                "acceptance": "\n".join(spec.acceptance_criteria).strip()
                or f"Complete: {spec.title}",
                "max_sessions": int(load_config().get("max_sessions", 8)),
                "working_dir": longhaul_working_dir,
                "control_task_id": str(task["id"]),
            }
        )
        longhaul_store.set_longhaul_json(board_id, state)
        engine = LonghaulEngine(longhaul_store, load_config(), database_path)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(engine.dispatch(board_id))
        else:
            running_loop.create_task(engine.dispatch(board_id))
        board_row = collab_store.get_board_task(board_id) or {}
        return {
            "board_task": _enrich_board_row(store, board_row),
            "task_id": str(task["id"]),
            "run_id": None,
            "session_id": board_row.get("result_ref"),
            "execute": "longhaul",
            "working_dir": longhaul_working_dir,
            "project_id": project_id,
            "provisioned": False,
            "allowed_connectors": [],
            "lane": "longhaul",
            "category_id": category_id,
        }

    working_dir: str | None = None
    plan: list[dict[str, Any]] | None = None
    if mode == "tools":
        working_dir = (
            provisioned.working_dir
            if provisioned is not None
            else _resolve_working_dir(store, str(task["id"]), project_id)
        )
        # A single agent step, DECLARED consequential on purpose: an agent that can
        # write files / run shell is a consequential action, so the runner's policy
        # gate must stop and ask a human before it runs. working_dir scopes the
        # adapter's workspace_write sandbox to the resolved dir.
        step_params: dict[str, Any] = {"working_dir": working_dir}
        if real_harness:
            # Durable arming marker. The run may be executed minutes or days
            # later, in a different process: the runner re-checks the flag in
            # ITS environment before touching the adapter and fails the step
            # closed if the operator has since disarmed (runner/core.py).
            step_params["real_harness"] = True
        plan = [
            {
                "name": "agent",
                "kind": "agent",
                "action_class": ActionClass.CONSEQUENTIAL.value,
                "params": step_params,
            }
        ]
    elif mode == "session":
        # A session needs the same scoped project dir a tools-mode run gets (its
        # own working dir, never an unscoped/home dir) -- just launched as a live
        # Claude Code process instead of a queued run (see below).
        working_dir = (
            provisioned.working_dir
            if provisioned is not None
            else _resolve_working_dir(store, str(task["id"]), project_id)
        )

    if working_dir is not None:
        _prepare_working_dir(working_dir)

    run: dict[str, Any] | None = None
    session_id: str | None = None
    if mode == "session":
        # NEVER create a queued run for session mode -- the live Session Bridge
        # process IS the execution vehicle; the runner-queue lane stays untouched.
        model_band, resolved_model = _resolve_session_model(spec.description or spec.title, model)
        LOG.info("session model tier selected: band=%s model=%s", model_band, resolved_model)
        resolved_budget = budget_usd_max
        if resolved_budget is None and project_id:
            resolved_budget = _resolve_project_budget(store, project_id)
        spawner = session_spawner or _default_session_spawner()
        owns_failure_dal = False
        failure_dal: SessionsDal | None = None
        try:
            # P3: the project's FULL granted scope (root_dirs + allowed_dirs beyond
            # working_dir), resolved server-side. Frozen onto the session row by
            # spawn() so BOTH the OS sandbox and the hook classifier honor the same
            # roots -- a write inside any of them is in-scope, not a hard stop.
            session_granted_roots = _resolve_session_granted_roots(
                store, project_id, cast(str, working_dir)
            )
            session_prompt = (
                target_and_todo_prompt(
                    spec.description or spec.title,
                    target_write_root,
                    granted_roots=session_granted_roots,
                )
                if fast
                else _compose_prompt(spec)
            )
            # A1.2: ALL machine-spawned intake dispatch sessions run hands-off --
            # spawn marks them orchestrator-owned BEFORE the process launches (so
            # hook-eval auto-approves safe work and escalates ONLY money/delete/
            # secret -- the operator's policy -- with no race that would park the session's
            # first write for a human). Fast and planned lanes alike: an intake
            # dispatch spawn is machine-initiated by definition.
            session_id = spawner.spawn(
                project_dir=cast(str, working_dir),
                model=resolved_model,
                prompt=session_prompt,
                budget_usd_max=resolved_budget,
                title=spec.title,
                extra_write_roots=[target_write_root] if target_write_root else None,
                orchestrator_owned=True,
                orchestrator_run_id=orchestration_run_id,
                granted_roots=session_granted_roots or None,
            )
            # Record the session on the board card so it is trackable from the board;
            # the dashboard's Sessions area already lists/monitors it (the Session
            # Bridge, unchanged) once spawn() has created its durable session row.
            # `result_ref` is the existing generic "run/experiment/note id when done"
            # field -- reused here (set immediately, not only on completion) rather
            # than `run_id`, because a session is never a `runs` table row and
            # `reconcile_board`/`_enrich_board_row` resolve `run_id` via
            # `store.get_run`, which a session id would never match.
            collab_store.update_board_task(board_id, {"result_ref": session_id})
            _emit_board_event(collab_store, board_id, session_id=session_id)
            # FAST DISPATCH (F4): a solo re-route now has its session id. Close the
            # pre-created queued orchestration it superseded (so the resume sweep
            # never conducts it as a phantom duplicate) and record the decision
            # with ref_id=session_id -- AFTER spawn, not before, so telemetry
            # carries the real target.
            if _fd_session_pending is not None:
                from omniagentos.dispatch import record_decision as _fd_record

                if orchestration_run_id:
                    _close_superseded_by_session(store, orchestration_run_id, session_id)
                _fd_record(
                    getattr(store, "_connection", None),
                    _fd_session_pending["decision"],
                    brief=_fd_session_pending["brief"],
                    applied=True,
                    dispatch_kind="session",
                    ref_id=session_id,
                )
        except Exception as exc:
            LOG.exception("session spawn failed for task %s", task["id"])
            failure_dal = getattr(spawner, "dal", None)
            if not isinstance(failure_dal, SessionsDal):
                failure_dal = SessionsDal(default_db_path())
                owns_failure_dal = True
            failed_session_id = getattr(exc, "session_id", None)
            if not isinstance(failed_session_id, str) or not failed_session_id:
                failed_session_id = _record_failed_session(
                    failure_dal,
                    task_id=str(task["id"]),
                    project_dir=cast(str, working_dir),
                    model=resolved_model,
                    error=str(exc),
                )
            collab_store.update_board_task(
                board_id,
                {
                    "result_ref": failed_session_id,
                    # BoardTaskStatus has no FAILED value; BLOCKED is its existing
                    # failure column (the linked session itself is state=FAILED).
                    "status": BoardTaskStatus.BLOCKED.value,
                },
            )
            _emit_board_event(collab_store, board_id, session_id=failed_session_id)
            raise
        finally:
            if owns_failure_dal and failure_dal is not None:
                failure_dal.close()
    else:
        board_priority = board_priority_to_run_priority(
            (collab_store.get_board_task(board_id) or {}).get("priority")
        )
        run = create_run_service(
            store,
            policy_cfg,
            task_id=str(task["id"]),
            harness=harness_value,
            prompt=_compose_prompt(spec),
            plan=plan,
            priority=board_priority,
            origin="board_task",
            provenance_id=board_id,
        )
        # Bridge the card to its executor (migration 016). Best-effort event so the
        # collab SSE consumers refresh the board immediately.
        collab_store.update_board_task(board_id, {"run_id": str(run["id"])})
        _emit_board_event(collab_store, board_id, run_id=str(run["id"]))

    board_row = collab_store.get_board_task(board_id) or {}
    result: dict[str, Any] = {
        "board_task": _enrich_board_row(store, board_row),
        "task_id": str(task["id"]),
        "run_id": str(run["id"]) if run is not None else None,
        "session_id": session_id,
        "execute": mode,
        "working_dir": working_dir,
        "project_id": project_id,
        "provisioned": provisioned is not None,
        "allowed_connectors": (
            list(provisioned.allowed_connectors) if provisioned is not None else []
        ),
    }
    if real_harness:
        # Present ONLY on an armed upgrade, so an unarmed dispatch's response is
        # indistinguishable from the one this endpoint returned before the flag
        # existed -- and the operator can see, per dispatch, that this goal was
        # promoted to real execution.
        result["real_harness"] = True
    elif real_harness_blocked is not None:
        # Armed, but withheld: say so out loud. A silent no-op here is exactly
        # how a goal ends up looking dispatched while nothing executes.
        result["real_harness"] = False
        result["real_harness_blocked"] = real_harness_blocked
    return result


def _default_orchestrate_runner(
    goal: str,
    *,
    priority: str = "balanced",
    pins: dict[str, Any] | None = None,
    working_dir: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    granted_roots: list[str] | None = None,
    checkpoint: OrchestrationCheckpoint | None = None,
    resume_state: ResumeState | None = None,
) -> Any:
    """Lazily bind the real Orchestrator entry point (avoids an import cycle).

    ``run_orchestration`` pulls in the orchestrator package, which imports the planner;
    binding it lazily keeps ``intake.service`` importable without dragging the whole
    orchestrator/planner graph into every dispatch caller. ``granted_roots`` (P3 / FIX
    6) is the project's server-resolved scope, threaded into every executor spawn.
    """
    from omniagentos.orchestrator import run_orchestration

    return run_orchestration(
        goal,
        priority=priority,  # type: ignore[arg-type]
        pins=pins,
        working_dir=working_dir,
        project_id=project_id,
        run_id=run_id,
        granted_roots=granted_roots,
        checkpoint=checkpoint,
        resume_state=resume_state,
    )


def _call_orchestrate_runner(
    runner: OrchestrateRunner,
    goal: str,
    *,
    priority: str,
    pins: dict[str, Any] | None,
    working_dir: str | None,
    project_id: str | None,
    run_id: str,
    granted_roots: list[str] | None,
    checkpoint: OrchestrationCheckpoint | None,
    resume_state: ResumeState | None,
) -> Any:
    """Pass durable kwargs when supported, preserving pre-checkpoint test seams."""
    pre_checkpoint_kwargs: dict[str, Any] = {
        "priority": priority,
        "pins": pins,
        "working_dir": working_dir,
        "project_id": project_id,
        "run_id": run_id,
        "granted_roots": granted_roots,
    }
    kwargs = {
        **pre_checkpoint_kwargs,
        "checkpoint": checkpoint,
        "resume_state": resume_state,
    }
    try:
        parameters = tuple(inspect.signature(runner).parameters.values())
    except (TypeError, ValueError):
        kwargs = pre_checkpoint_kwargs
        parameters = ()
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    accepted_names = {parameter.name for parameter in parameters}
    if parameters and not accepts_kwargs:
        kwargs = {name: value for name, value in kwargs.items() if name in accepted_names}
    return runner(goal, **kwargs)


def _orchestration_params_json(
    *,
    priority: str,
    pins: dict[str, Any] | None,
    project_id: str | None,
    granted_roots: list[str] | None,
) -> str:
    return json.dumps(
        {
            "priority": priority,
            "pins": pins,
            "project_id": project_id,
            "granted_roots": granted_roots,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _register_active_conductor(run_id: str, thread: threading.Thread) -> bool:
    with _ACTIVE_CONDUCTORS_LOCK:
        existing = _ACTIVE_CONDUCTORS.get(run_id)
        if existing is not None:
            existing_thread, entry_lock = existing
            with entry_lock:
                active = existing_thread.is_alive() or existing_thread.ident is None
            if active and existing_thread is not thread:
                LOG.info("orchestration %s conductor live", run_id)
                return False
            if existing_thread is thread:
                return True
        _ACTIVE_CONDUCTORS[run_id] = (thread, threading.Lock())
        return True


def _reserve_active_conductor_slot(run_id: str) -> threading.Thread | None:
    """Reserve a local conductor slot before claiming its persisted lease."""
    placeholder = threading.Thread(name=f"intake-resume-claim-{run_id}")
    with _ACTIVE_CONDUCTORS_LOCK:
        existing = _ACTIVE_CONDUCTORS.get(run_id)
        if existing is not None:
            existing_thread, entry_lock = existing
            if _thread_active(existing_thread, entry_lock):
                LOG.info("orchestration %s conductor live", run_id)
                return None
            del _ACTIVE_CONDUCTORS[run_id]
        _ACTIVE_CONDUCTORS[run_id] = (placeholder, threading.Lock())
    return placeholder


def _replace_active_conductor_slot(
    run_id: str,
    placeholder: threading.Thread,
    worker: threading.Thread,
) -> bool:
    """Atomically replace a claimed resume placeholder with its real worker."""
    with _ACTIVE_CONDUCTORS_LOCK:
        existing = _ACTIVE_CONDUCTORS.get(run_id)
        if existing is None or existing[0] is not placeholder:
            return False
        _ACTIVE_CONDUCTORS[run_id] = (worker, existing[1])
        return True


def _unregister_active_conductor(run_id: str, thread: threading.Thread) -> None:
    with _ACTIVE_CONDUCTORS_LOCK:
        existing = _ACTIVE_CONDUCTORS.get(run_id)
        if existing is not None and existing[0] is thread:
            del _ACTIVE_CONDUCTORS[run_id]


def _local_conductor_live(run_id: str) -> bool:
    with _ACTIVE_CONDUCTORS_LOCK:
        existing = _ACTIVE_CONDUCTORS.get(run_id)
        if existing is None:
            return False
        thread, entry_lock = existing
        with entry_lock:
            active = thread.is_alive() or thread.ident is None
        if not active:
            del _ACTIVE_CONDUCTORS[run_id]
            return False
    LOG.info("orchestration %s conductor live", run_id)
    return True


def _live_resume_conductor_count() -> int:
    with _ACTIVE_CONDUCTORS_LOCK:
        return sum(
            1
            for thread, entry_lock in _ACTIVE_CONDUCTORS.values()
            if thread.name.startswith("intake-resume-") and _thread_active(thread, entry_lock)
        )


def _thread_active(thread: threading.Thread, entry_lock: threading.Lock) -> bool:
    with entry_lock:
        return thread.is_alive() or thread.ident is None


def _swarm_bundle_spec(plan: Any) -> RefinedSpec:
    """A RefinedSpec for ONE planner bundle (its goal + task acceptance)."""
    goal = str(getattr(plan, "goal", "") or "").strip()
    title = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal)[:120]
    criteria = [
        str(getattr(task, "acceptance", "") or "").strip()
        for task in (getattr(plan, "tasks", None) or [])
        if str(getattr(task, "acceptance", "") or "").strip()
    ]
    return RefinedSpec(
        title=title or "Intake task",
        description=goal or title or "Intake task",
        acceptance_criteria=criteria or [f"Delivers on: {title or 'the request'}"],
    ).normalized()


def _provision_swarm_bundle(
    store: Store,
    collab_store: CollabStore,
    dal: Any,
    plan: Any,
    spec: RefinedSpec,
    *,
    project_id: str | None,
    budget_usd_max: float | None,
    priority: str | None = None,
    pins: dict[str, Any] | None = None,
    speed: str | None = None,
) -> dict[str, Any]:
    """Provision ONE swarm-worthy plan as a swarm run; activate behind the flag.

    Fleet admission applies here (WP10): at/over ``max_concurrent_swarms`` the
    run provisions with status ``queued`` -- parked with no coordinator, started
    oldest-first as capacity frees -- so intake NEVER blocks on fleet capacity
    (small/fast/longhaul tasks keep their reserved headroom via the scheduler's
    limits port; nothing here consumes a session slot). Activation goes through
    ``activate_run_if_enabled``: with ``OMNIAGENTOS_SWARM_EXECUTE`` unset/false
    this is provision-only, exactly like ``POST /api/swarm``.

    ``priority``/``pins``/``speed`` (D10 Mode dial) are recorded ADDITIVELY on
    the run's ``plan_json["params"]`` (old rows lack the key and keep current
    behavior everywhere); a valid ``speed`` is also stamped into every card's
    ``swarm_json`` so the router's tier floor can read it per task.
    """
    from omniagentos.budget.policy import swarm_run_budget
    from omniagentos.swarm.plan_safety import assert_plan_safe_for_provision
    from omniagentos.swarm.planner import provision_run as _swarm_provision_run

    # Reject unsafe planner output before even resolving/creating an
    # orchestration project. A second workspace-aware check below binds the
    # same decision to the resolved directory.
    assert_plan_safe_for_provision(plan)

    exec_params: dict[str, Any] = {}
    if priority in ("fast", "balanced", "quality"):
        exec_params["priority"] = priority
    if pins:
        exec_params["pins"] = dict(pins)
    speed_normalized = str(speed or "").strip().lower()
    if speed_normalized in ("fast", "auto", "ultra"):
        exec_params["speed"] = speed_normalized

    # Swarm workspaces must be git checkouts (scheduler refusal + snapshot HEAD).
    resolved_project_id, working_dir = _resolve_or_create_orchestration_project(
        store, spec, project_id, ensure_git=True
    )
    if working_dir:
        _prepare_working_dir(working_dir)

    # Fail-closed before any run/card creation (mutation: refusal-creates-run).
    assert_plan_safe_for_provision(plan, workspace_dir=working_dir or "")

    try:
        from omniagentos.routing.limit_state import load_swarm_config

        max_swarms = int(load_swarm_config().get("max_concurrent_swarms") or 10)
    except Exception:  # noqa: BLE001 -- a config fault must not block dispatch.
        LOG.debug("could not load max_concurrent_swarms; defaulting to 10", exc_info=True)
        max_swarms = 10
    queued = dal.active_run_count() >= max(1, max_swarms)

    provisioned = _swarm_provision_run(
        plan,
        dal=dal,
        working_dir=working_dir or "",
        project_id=resolved_project_id or project_id,
        # Task-dependent, same rule the /swarm route uses: an explicit request
        # wins, otherwise the plan's size sets the figure (budget.policy).
        budget_usd_max=swarm_run_budget(len(plan.tasks), budget_usd_max),
        priority=spec.suggested_priority or "normal",
        status="queued" if queued else "planning",
        params=exec_params or None,
    )
    run_row = provisioned["run"]
    run_id = str(run_row["id"])
    root_card_id = str(provisioned["root_card_id"])

    try:
        from omniagentos.swarm.activity import StoreSwarmEmitter
        from omniagentos.swarm.contracts import ACTION_PLAN_CREATED

        formation = getattr(plan, "formation", None)
        formation_payload = (
            formation.model_dump(mode="json")
            if formation is not None and hasattr(formation, "model_dump")
            else None
        )
        StoreSwarmEmitter(store).emit(
            run_id,
            ACTION_PLAN_CREATED,
            {
                "task_count": len(getattr(plan, "tasks", None) or []),
                "parallelism_ratio": getattr(plan, "parallelism_ratio", None),
                "source": "intake",
                "queued": queued,
                "formation": formation_payload,
            },
        )
    except Exception:  # noqa: BLE001 -- emission is observability, never control flow.
        LOG.debug("swarm plan_created emit failed for %s", run_id, exc_info=True)

    activated = False
    if not queued:
        try:
            # Independent activation recheck (mutation: activation-skips-recheck).
            assert_plan_safe_for_provision(plan, workspace_dir=working_dir or "")
            from omniagentos.swarm.scheduler import activate_run_if_enabled

            activated = activate_run_if_enabled(run_id)
        except Exception:  # noqa: BLE001 -- activation must never fail the dispatch.
            LOG.exception("swarm run activation hook failed for %s", run_id)

    _emit_board_event(collab_store, root_card_id)
    board_row = collab_store.get_board_task(root_card_id) or {}
    current = dal.get_run(run_id) or run_row
    return {
        "board_task": _enrich_board_row(store, board_row),
        "task_id": None,
        "run_id": None,
        "session_id": None,
        "execute": "swarm",
        "working_dir": working_dir or None,
        "project_id": resolved_project_id or project_id,
        "provisioned": False,
        "allowed_connectors": [],
        "swarm_run_id": run_id,
        "swarm": {
            "run_id": run_id,
            "root_card_id": root_card_id,
            "status": str(current.get("status")),
            "queued": queued,
            "activated": activated,
            "task_count": len(getattr(plan, "tasks", None) or []),
            "parallelism_ratio": getattr(plan, "parallelism_ratio", None),
            "target_n": getattr(plan, "target_n", None),
        },
    }


def _close_superseded_orchestration(
    store: Store, run_id: str, swarm_result: dict[str, Any]
) -> None:
    """Terminal-close a pre-created orchestrations row superseded by swarm dispatch.

    Best-effort board/lifecycle hygiene, never control flow: the swarm run(s)
    already exist; a row that cannot be closed is only ever a stale queued
    entry. A ``run_id`` with no orchestrations row (the quick swarm
    passthrough's correlation-only id) updates zero rows and is a no-op."""
    try:
        swarm_run_id = str(swarm_result.get("swarm_run_id") or "").strip()
        detail = (
            f"superseded by swarm dispatch (swarm run {swarm_run_id})"
            if swarm_run_id
            else "superseded by swarm dispatch"
        )
        lifecycle = OrchestrationsDal(_sqlite_db_path(store))
        try:
            lifecycle.set_status(run_id, "cancelled", error=detail)
        finally:
            lifecycle.close()
    except Exception:  # noqa: BLE001 -- hygiene only; the swarm dispatch already succeeded.
        LOG.debug("could not close superseded orchestration %s", run_id, exc_info=True)


def _close_superseded_by_session(store: Store, run_id: str, session_id: str) -> None:
    """Terminal-close a pre-created orchestration superseded by a FAST DISPATCH
    solo re-route (the live session IS the execution vehicle now).

    Best-effort board/lifecycle hygiene, never control flow -- the session already
    spawned; a row that cannot be closed is only a stale queued entry that the
    resume sweep would otherwise conduct as a phantom duplicate. A ``run_id`` with
    no orchestrations row updates zero rows and is a harmless no-op."""
    try:
        lifecycle = OrchestrationsDal(_sqlite_db_path(store))
        try:
            lifecycle.set_status(
                run_id,
                "cancelled",
                error=f"superseded by fast-dispatch session {session_id}",
            )
        finally:
            lifecycle.close()
    except Exception:  # noqa: BLE001 -- hygiene only; the session already spawned.
        LOG.debug("could not close superseded orchestration %s", run_id, exc_info=True)


def _dispatch_swarm(
    store: Store,
    collab_store: CollabStore,
    spec: RefinedSpec,
    *,
    project_id: str | None,
    priority: str | None,
    pins: dict[str, Any] | None,
    orchestrate_runner: OrchestrateRunner | None,
    async_orchestrate: bool,
    board_task_id: str | None,
    budget_usd_max: float | None,
    swarm_planner: Callable[..., Any] | None,
    speed: str | None = None,
) -> dict[str, Any] | None:
    """WP10: plan ``spec`` with the swarm planner; dispatch what parallelism pays for.

    ``swarm_planner`` (default ``swarm.planner.plan_swarm_bundles``) returns one
    :class:`~omniagentos.swarm.contracts.SwarmPlan` per detected bundle -- a
    brief with one coherent ask yields one plan; N unrelated asks yield N plans,
    each dispatched INDEPENDENTLY as its own board card(s):

    * a solo bundle goes down the existing orchestrate path (its own card, its
      own orchestration run -- the sequential Orchestrator owns it end to end);
    * a swarm-worthy bundle is provisioned as a swarm run (root card + child DAG
      cards) with fleet admission + flag-gated activation
      (:func:`_provision_swarm_bundle`).

    Returns ``None`` for a SINGLE solo plan: the caller falls through to the
    EXISTING orchestrate path with zero behavior change (dispatch_spec then
    behaves exactly as ``execute="orchestrate"`` would have). A quick-front-door
    placeholder card (``board_task_id``) is archived once real bundle cards
    exist -- except on that solo fall-through, where the orchestrate path keeps
    reusing it as today.
    """
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import fast_speed_headroom, plan_swarm_bundles

    db_path = _sqlite_db_path(store)
    dal = SwarmDal(db_path)
    try:
        planner = swarm_planner or plan_swarm_bundles
        # Rate-limit-aware mode decision: measure fleet headroom (global swarm
        # slots + mean provider pressure, read from THIS control plane's DB)
        # and hand it to the planner's solo-vs-swarm rule -- LOW headroom
        # raises the swarm bar (configs/swarm.yaml auto.low_headroom_*), and
        # the decision + inputs land in plan_json.assumptions. Best-effort: a
        # headroom fault degrades to the standard 1.5 rule, never blocks
        # dispatch.
        headroom = None
        try:
            from omniagentos.swarm.planner import swarm_headroom

            headroom = swarm_headroom(db_path=db_path)
        except Exception:  # noqa: BLE001 -- fleet telemetry is advisory to planning.
            LOG.debug("swarm headroom unavailable; standard solo rule applies", exc_info=True)
        if str(speed or "").strip().lower() == "fast":
            # Fastest-dial topology bias: swarm must pay off harder (the
            # low-headroom ratio) before the fastest lane spends wall-clock
            # coordinating a DAG.
            headroom = fast_speed_headroom(headroom)
        # Planning is workspace-independent today (plan_swarm_bundles reserves
        # the dir argument); each bundle resolves its OWN project/working dir at
        # provision/orchestrate time, so a solo fall-through never leaves an
        # orphan project behind.
        planner_kwargs: dict[str, Any] = {"dal": dal, "headroom": headroom}
        # The production planner resolves this project through projects.org_company_id
        # before ambient recall. Keep injected legacy test/operator seams unchanged.
        if swarm_planner is None:
            planner_kwargs["project_id"] = project_id
        plans = list(planner(spec, "", **planner_kwargs))
        if not plans:
            return None

        # Fail-closed multi-bundle / unsafe plans before any side effect.
        # Interim multi-bundle containment refuses the whole set (P1-SAFETY /
        # R4); atomic group creation lands later in P5-GROUPS.
        from omniagentos.swarm.plan_safety import PlanSafetyError, decide_from_plans

        safety = decide_from_plans(plans, allow_multi_bundle=False)
        if not safety.is_ready:
            raise PlanSafetyError(safety)

        if len(plans) == 1 and str(getattr(plans[0], "mode", "swarm")) == "solo":
            decision_notes = [
                str(note)
                for note in (getattr(plans[0], "assumptions", None) or [])
                if "auto headroom" in str(note)
            ]
            LOG.info(
                "swarm auto-decision: solo fall-through for %r (%d planned task(s))%s",
                spec.title[:80],
                len(getattr(plans[0], "tasks", None) or []),
                f" — {decision_notes[-1]}" if decision_notes else "",
            )
            return None

        results: list[dict[str, Any]] = []
        for plan in plans:
            bundle_spec = spec if len(plans) == 1 else _swarm_bundle_spec(plan)
            if str(getattr(plan, "mode", "swarm")) == "solo":
                results.append(
                    _dispatch_orchestrate(
                        store,
                        collab_store,
                        bundle_spec,
                        project_id=project_id,
                        priority=priority,
                        pins=pins,
                        orchestrate_runner=orchestrate_runner,
                        async_orchestrate=async_orchestrate,
                        board_task_id=None,
                        orchestration_run_id=None,
                    )
                )
            else:
                results.append(
                    _provision_swarm_bundle(
                        store,
                        collab_store,
                        dal,
                        plan,
                        bundle_spec,
                        project_id=project_id,
                        budget_usd_max=budget_usd_max,
                        priority=priority,
                        pins=pins,
                        speed=speed,
                    )
                )

        if board_task_id:
            # The quick front door's instant placeholder is superseded by the
            # real bundle cards created above; archive it so the board never
            # shows a phantom duplicate.
            try:
                collab_store.update_board_task(board_task_id, {"archived_at": utc_now_iso()})
                _emit_board_event(collab_store, board_task_id)
            except Exception:  # noqa: BLE001 -- board hygiene, never control flow.
                LOG.debug(
                    "could not archive superseded placeholder card %s",
                    board_task_id,
                    exc_info=True,
                )

        out = dict(results[0])
        out["bundles"] = [
            {
                "execute": entry.get("execute"),
                "board_task_id": (entry.get("board_task") or {}).get("id"),
                "run_id": entry.get("run_id"),
                "swarm_run_id": entry.get("swarm_run_id"),
            }
            for entry in results
        ]
        return out
    finally:
        dal.close()


def _dispatch_orchestrate(
    store: Store,
    collab_store: CollabStore,
    spec: RefinedSpec,
    *,
    project_id: str | None,
    priority: str | None,
    pins: dict[str, Any] | None,
    orchestrate_runner: OrchestrateRunner | None,
    async_orchestrate: bool,
    board_task_id: str | None,
    orchestration_run_id: str | None,
) -> dict[str, Any]:
    """Run one orchestration for ``spec`` and surface a board card for it.

    The composed spec prompt is the orchestration goal; the project's scoped root dir
    (when any) is the working dir. The Orchestrator itself writes the spec, spawns the
    tiered executors and resolves approvals -- this only creates the visible board card
    and returns a compact summary alongside it.
    """
    runner = orchestrate_runner or _default_orchestrate_runner
    # Resolve or create a project for this orchestration
    resolved_project_id, working_dir = _resolve_or_create_orchestration_project(
        store, spec, project_id
    )
    if working_dir:
        _prepare_working_dir(working_dir)
    project_id = resolved_project_id or project_id

    # P3 (FIX 6): resolve the project's FULL granted scope (root_dirs + allowed_dirs
    # beyond working_dir) SERVER-SIDE here -- the intake path owns the project store --
    # and thread it into the orchestration so every executor session it spawns is frozen
    # with the same scope an intake session gets. Reuses the exact session-dispatch
    # helper (same secret-drop + retarget-revalidation). A working_dir-less fallback
    # resolves to no extra scope (pre-P3 working-dir-only confinement).
    orchestration_granted_roots = (
        _resolve_session_granted_roots(store, project_id, working_dir) if working_dir else []
    )

    # A RefinedSpec is already the product of clarify+planning, so its composed prompt
    # is the orchestration goal verbatim. Natural-language intent parsing (priority /
    # executor / pins from raw user text) happens at the FRONT DOOR before planning and
    # arrives here as the explicit ``priority``/``pins`` args -- it is never re-parsed
    # off the spec here (that would rewrite the goal and re-hit the model).
    final_priority = priority if priority in {"fast", "balanced", "quality"} else "balanced"

    if async_orchestrate:
        return _queue_orchestration(
            collab_store,
            spec,
            runner=runner,
            working_dir=working_dir,
            project_id=project_id,
            priority=final_priority,
            pins=pins,
            board_task_id=board_task_id,
            run_id=orchestration_run_id,
            granted_roots=orchestration_granted_roots,
            db_path=_sqlite_db_path(store),
        )

    run_id = orchestration_run_id or new_id("orch")
    board = BoardTask(
        title=spec.title,
        description=_compose_description(spec),
        required_expertise=list(spec.required_expertise),
        discipline=spec.suggested_discipline,
        priority=spec.suggested_priority,
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref=run_id,
        project_id=project_id,  # M1: scoped by the INSERT, not by a later PATCH
    )
    collab_store.create_board_task(board)
    _prearchive_suppressed_card(collab_store, board.id, spec.title)
    _persist_board_project_id(collab_store, board.id, project_id)
    lifecycle_db_path = _sqlite_db_path(store)
    lifecycle = OrchestrationsDal(lifecycle_db_path)
    lifecycle.create(
        run_id,
        board_task_id=board.id,
        working_dir=working_dir or "",
        goal=_compose_prompt(spec),
        params_json=_orchestration_params_json(
            priority=final_priority,
            pins=pins,
            project_id=project_id,
            granted_roots=orchestration_granted_roots,
        ),
    )
    _emit_board_event(collab_store, board.id, run_id=run_id)
    result = _run_orchestration_with_lifecycle(
        lifecycle,
        collab_store,
        board_id=board.id,
        run_id=run_id,
        db_path=lifecycle_db_path,
        runner=runner,
        goal=_compose_prompt(spec),
        priority=final_priority,
        pins=pins,
        working_dir=working_dir,
        project_id=project_id,
        granted_roots=orchestration_granted_roots,
    )

    status = getattr(result, "status", None)

    board_row = collab_store.get_board_task(board.id) or {}
    escalations = getattr(result, "escalations", []) or []
    return {
        "board_task": _enrich_board_row(store, board_row),
        "task_id": None,
        "run_id": run_id,
        "session_id": None,
        "execute": "orchestrate",
        "working_dir": working_dir,
        "project_id": project_id,
        "provisioned": False,
        "allowed_connectors": [],
        "orchestration": {
            "run_id": run_id,
            "status": status,
            "priority": final_priority,
            "task_count": len(getattr(result, "tasks", []) or []),
            "escalation_count": len(escalations),
            "spec_note_path": getattr(result, "spec_note_path", None),
        },
    }


def _heartbeat_orchestration(
    stop: threading.Event,
    *,
    run_id: str,
    db_path: str,
    conductor_pid: int | None = None,
    conductor_claimed_at: str | None = None,
) -> None:
    """F-011: Fence heartbeat by conductor claim. Stops if conductor is superseded."""
    heartbeat_dal: OrchestrationsDal | None = None
    try:
        heartbeat_dal = OrchestrationsDal(db_path)
    except Exception:  # noqa: BLE001 -- heartbeat telemetry must never kill the worker.
        LOG.exception("could not create heartbeat DAL for orchestration %s", run_id)
        return
    try:
        while not stop.wait(30):
            try:
                # F-011: Check conductor claim matches before beating.
                if conductor_pid is not None and conductor_claimed_at is not None:
                    row = heartbeat_dal.get(run_id)
                    if (
                        row is None
                        or row.get("conductor_pid") != conductor_pid
                        or row.get("conductor_claimed_at") != conductor_claimed_at
                    ):
                        LOG.info("orchestration %s heartbeat stopped: conductor superseded", run_id)
                        break
                    updated = heartbeat_dal.heartbeat(
                        run_id,
                        conductor_pid=conductor_pid,
                        conductor_claimed_at=conductor_claimed_at,
                    )
                    if updated is False:
                        LOG.info("orchestration %s heartbeat stopped: conductor superseded", run_id)
                        break
                else:
                    heartbeat_dal.heartbeat(run_id)
            except Exception:  # noqa: BLE001 -- a transient busy DB must not stop heartbeats.
                LOG.debug("orchestration %s heartbeat failed; retrying", run_id, exc_info=True)
    finally:
        heartbeat_dal.close()


def _renotify_orchestration_approvals(db_path: str, run_id: str) -> None:
    """Best-effort re-surface of pending approvals attached to resumed steps.

    Deduped. This runs on EVERY resume tick, so ``dedupe=False`` (as it was until
    2026-07-24) meant one still-pending approval accumulated a fresh notification
    every few minutes for as long as nobody clicked it -- 6 rows and 6 desktop
    banners for a single approval inside 75 minutes, live. Deduping still
    re-surfaces what the operator has already dismissed: the guard only skips
    while an UNREAD notification for that approval exists.
    """
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        session_rows = connection.execute(
            "SELECT DISTINCT session_id FROM orchestration_steps "
            "WHERE run_id = ? AND session_id IS NOT NULL",
            (run_id,),
        ).fetchall()
        for session_row in session_rows:
            session_id = str(session_row["session_id"])
            approvals = connection.execute(
                "SELECT id, action_class, proposed_action, risk FROM approvals "
                "WHERE session_id = ? AND state = 'pending'",
                (session_id,),
            ).fetchall()
            for approval in approvals:
                approval_id = str(approval["id"])
                action_class = str(approval["action_class"])
                proposed_action = str(approval["proposed_action"])
                try:
                    record_notification(
                        kind="approval",
                        title="Approval required",
                        body=f"{action_class}: {proposed_action}".strip(),
                        severity="warning",
                        ref_type="approval",
                        ref_id=approval_id,
                        payload={
                            "approval_id": approval_id,
                            "action_class": action_class,
                            "proposed_action": proposed_action,
                            "risk": str(approval["risk"] or ""),
                            "source": "orchestrator-resume",
                            "session_id": session_id,
                            "run_id": run_id,
                        },
                        db_path=db_path,
                        dedupe=True,
                    )
                except Exception:  # noqa: BLE001 -- continue re-surfacing other approvals.
                    LOG.debug(
                        "orchestration %s approval %s re-notification failed",
                        run_id,
                        approval_id,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001 -- approval reminders must never block a resume.
        LOG.debug("orchestration %s approval re-notification failed", run_id, exc_info=True)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 -- cleanup is best-effort.
                LOG.debug(
                    "orchestration %s approval re-notification cleanup failed",
                    run_id,
                    exc_info=True,
                )


def _run_orchestration_with_lifecycle(
    lifecycle: OrchestrationsDal,
    collab_store: CollabStore,
    *,
    board_id: str,
    run_id: str,
    db_path: str,
    runner: OrchestrateRunner,
    goal: str,
    priority: str,
    pins: dict[str, Any] | None,
    working_dir: str | None,
    project_id: str | None,
    granted_roots: list[str] | None,
    resume_state: ResumeState | None = None,
    resuming: bool = False,
    conductor_claimed_at: str | None = None,
) -> Any:
    """Run one orchestration with the same persisted lifecycle in every dispatch mode."""
    conductor_thread = threading.current_thread()
    registered = _register_active_conductor(run_id, conductor_thread)
    if not registered:
        raise RuntimeError("conductor live")
    conductor_pid = os.getpid()
    claim_stamp = conductor_claimed_at
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        if not resuming:
            claim_stamp = lifecycle.conductor_started(run_id, pid=conductor_pid)
        lifecycle.set_status(
            run_id,
            "running" if resuming else "planning",
            stage="resuming" if resuming else "planning",
        )
        board_fields: dict[str, Any] = {
            "status": BoardTaskStatus.IN_PROGRESS.value,
            "result_ref": run_id,
        }
        if resuming:
            board = collab_store.get_board_task(board_id) or {}
            board_fields["description"] = str(board.get("description") or "").replace(
                _ORCHESTRATION_DIED_MARKER, ""
            )
        collab_store.update_board_task(
            board_id,
            board_fields,
        )
        _emit_board_event(collab_store, board_id, run_id=run_id)
        if resuming:
            _renotify_orchestration_approvals(db_path, run_id)
        if claim_stamp is not None:
            heartbeat_thread = threading.Thread(
                target=_heartbeat_orchestration,
                kwargs={
                    "stop": heartbeat_stop,
                    "run_id": run_id,
                    "db_path": db_path,
                    "conductor_pid": conductor_pid,
                    "conductor_claimed_at": claim_stamp,
                },
                name=f"intake-heartbeat-{run_id}",
                daemon=True,
            )
            heartbeat_thread.start()
        lifecycle.set_status(run_id, "running", stage="running")
        _emit_board_event(collab_store, board_id, run_id=run_id)
        result = _call_orchestrate_runner(
            runner,
            goal,
            priority=priority,
            pins=pins,
            working_dir=working_dir,
            project_id=project_id,
            run_id=run_id,
            granted_roots=granted_roots,
            checkpoint=lifecycle,
            resume_state=resume_state,
        )
        completed = getattr(result, "status", None) in {"done", "completed"}
        terminal_written = lifecycle.set_status(
            run_id,
            "completed" if completed else "failed",
            error=None if completed else str(getattr(result, "status", None) or "unknown error"),
            conductor_pid=conductor_pid if claim_stamp is not None else None,
            conductor_claimed_at=claim_stamp,
        )
        if terminal_written is False:
            LOG.info("orchestration %s terminal write skipped: conductor superseded", run_id)
            return result
        terminal_fields: dict[str, Any] = {
            "status": (BoardTaskStatus.DONE.value if completed else BoardTaskStatus.BLOCKED.value),
            "result_ref": run_id,
        }
        if resuming and not completed:
            board = collab_store.get_board_task(board_id) or {}
            description = str(board.get("description") or "")
            if _ORCHESTRATION_DIED_MARKER not in description:
                terminal_fields["description"] = description + _ORCHESTRATION_DIED_MARKER
        collab_store.update_board_task(board_id, terminal_fields)
        _emit_board_event(collab_store, board_id, run_id=run_id)
        if completed:
            # C0: the "finished files" bell -- deep-links the owner to this
            # card's files drawer. Best-effort; deduped against reconcile_board.
            _notify_task_done_safe(
                collab_store,
                board_id,
                workspace=working_dir,
                run_id=run_id,
                session_id=None,
                db_path=db_path,
            )
        return result
    except Exception as exc:
        terminal_written = lifecycle.set_status(
            run_id,
            "failed",
            error=str(exc) or "unknown error",
            conductor_pid=conductor_pid if claim_stamp is not None else None,
            conductor_claimed_at=claim_stamp,
        )
        if terminal_written is False:
            LOG.info("orchestration %s terminal write skipped: conductor superseded", run_id)
            raise
        failed_fields: dict[str, Any] = {
            "status": BoardTaskStatus.BLOCKED.value,
            "result_ref": run_id,
        }
        if resuming:
            board = collab_store.get_board_task(board_id) or {}
            description = str(board.get("description") or "")
            if _ORCHESTRATION_DIED_MARKER not in description:
                failed_fields["description"] = description + _ORCHESTRATION_DIED_MARKER
        collab_store.update_board_task(
            board_id,
            failed_fields,
        )
        _emit_board_event(collab_store, board_id, run_id=run_id)
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None and heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=1)
        lifecycle.close()
        _unregister_active_conductor(run_id, conductor_thread)


def resume_orchestration(
    board_task_id: str,
    *,
    store: Store,
    collab_store: CollabStore,
    manual: bool = False,
) -> dict[str, Any]:
    """Claim and asynchronously resume one checkpointed orchestration."""
    try:
        board = collab_store.get_board_task(board_task_id)
    except Exception as exc:  # noqa: BLE001 -- normalize lookup failures to the contract.
        LOG.debug("could not load board task %s for resume", board_task_id, exc_info=True)
        raise LookupError(board_task_id) from exc
    if board is None:
        raise LookupError(board_task_id)
    run_id = str(board.get("result_ref") or "")
    if board.get("archived_at") is not None or str(board.get("status") or "") in {
        BoardTaskStatus.CANCELLED.value,
        BoardTaskStatus.DONE.value,
    }:
        if manual:
            raise ValueError("board task closed")
        return {"run_id": run_id, "resumed": False, "reason": "board task closed"}
    if not run_id.startswith("orch_"):
        raise ValueError("result_ref is not an orchestration")
    if board.get("lane") == "longhaul":
        raise ValueError("longhaul orchestration cannot be resumed by intake")

    db_path = _sqlite_db_path(store)
    try:
        lifecycle = OrchestrationsDal(db_path)
    except Exception:  # noqa: BLE001 -- unavailable telemetry must not break reconciliation.
        LOG.debug("could not open orchestration %s for resume", run_id, exc_info=True)
        return {"run_id": run_id, "resumed": False, "reason": "resume state unavailable"}
    try:
        orchestration = lifecycle.get(run_id)
        resume_state = lifecycle.load_resume_state(run_id)
    except Exception as exc:  # noqa: BLE001 -- normalize resume-state failures.
        LOG.debug("could not load orchestration %s resume state", run_id, exc_info=True)
        lifecycle.close()
        raise ValueError("not resumable") from exc
    if orchestration is None or orchestration.get("plan_json") is None or resume_state is None:
        lifecycle.close()
        raise ValueError("not resumable")

    placeholder = _reserve_active_conductor_slot(run_id)
    if placeholder is None:
        lifecycle.close()
        if manual:
            raise ValueError("conductor live")
        return {"run_id": run_id, "resumed": False, "reason": "conductor live"}

    try:
        claimed = lifecycle.claim_conductor(
            run_id,
            pid=os.getpid(),
            stale_minutes=_orchestration_stale_minutes(),
            allow_failed_retry=manual,
            max_resumes=10,
            max_retries=2,
        )
    except Exception:  # noqa: BLE001 -- treat claim failures exactly like a lost race.
        LOG.debug("orchestration %s conductor claim failed", run_id, exc_info=True)
        claimed = None
    if claimed is None:
        _unregister_active_conductor(run_id, placeholder)
        lifecycle.close()
        if manual:
            raise ValueError("conductor live")
        return {"run_id": run_id, "resumed": False, "reason": "not claimed"}

    if manual:
        lifecycle.reset_retry_steps(run_id)
        refreshed_state = lifecycle.load_resume_state(run_id)
        if refreshed_state is not None:
            resume_state = refreshed_state

    try:
        raw_params = json.loads(str(orchestration.get("params_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        raw_params = {}
    params = raw_params if isinstance(raw_params, dict) else {}
    priority_value = str(params.get("priority") or "balanced")
    priority = priority_value if priority_value in {"fast", "balanced", "quality"} else "balanced"
    pins_value = params.get("pins")
    pins = pins_value if isinstance(pins_value, dict) else None
    project_value = params.get("project_id")
    project_id = str(project_value) if project_value is not None else None
    roots_value = params.get("granted_roots")
    granted_roots = [str(root) for root in roots_value] if isinstance(roots_value, list) else None

    def run_in_background() -> None:
        try:
            _run_orchestration_with_lifecycle(
                lifecycle,
                collab_store,
                board_id=board_task_id,
                run_id=run_id,
                db_path=db_path,
                runner=_default_orchestrate_runner,
                goal=str(orchestration.get("goal") or ""),
                priority=priority,
                pins=pins,
                working_dir=str(orchestration.get("working_dir") or "") or None,
                project_id=project_id,
                granted_roots=granted_roots,
                resume_state=resume_state,
                resuming=True,
                conductor_claimed_at=str(claimed["conductor_claimed_at"]),
            )
        except Exception:  # noqa: BLE001 -- lifecycle already records terminal failure.
            LOG.debug("resumed orchestration %s failed", run_id, exc_info=True)

    try:
        worker = threading.Thread(
            target=run_in_background,
            name=f"intake-resume-{run_id}",
            daemon=True,
        )
    except Exception:  # noqa: BLE001 -- a failed construction must release the claim slot.
        _unregister_active_conductor(run_id, placeholder)
        lifecycle.close()
        raise
    if not _replace_active_conductor_slot(run_id, placeholder, worker):
        lifecycle.close()
        if manual:
            raise ValueError("conductor live")
        return {"run_id": run_id, "resumed": False, "reason": "conductor live"}
    if not _register_active_conductor(run_id, worker):
        _unregister_active_conductor(run_id, worker)
        lifecycle.close()
        if manual:
            raise ValueError("conductor live")
        return {"run_id": run_id, "resumed": False, "reason": "conductor live"}
    try:
        worker.start()
    except Exception:  # noqa: BLE001 -- a failed start must not leak the claimed DAL.
        _unregister_active_conductor(run_id, worker)
        LOG.debug("could not start resumed orchestration %s", run_id, exc_info=True)
        terminal_written = lifecycle.set_status(
            run_id,
            "failed",
            error="resume worker failed to start",
            conductor_pid=os.getpid(),
            conductor_claimed_at=str(claimed["conductor_claimed_at"]),
        )
        if terminal_written is False:
            LOG.info("orchestration %s terminal write skipped: conductor superseded", run_id)
            lifecycle.close()
            return {"run_id": run_id, "resumed": False, "reason": "conductor superseded"}
        try:
            description = str(board.get("description") or "")
            if _ORCHESTRATION_DIED_MARKER not in description:
                description += _ORCHESTRATION_DIED_MARKER
            collab_store.update_board_task(
                board_task_id,
                {
                    "status": BoardTaskStatus.BLOCKED.value,
                    "result_ref": run_id,
                    "description": description,
                },
            )
            _emit_board_event(collab_store, board_task_id, run_id=run_id)
        except Exception:  # noqa: BLE001 -- terminal persistence is best-effort.
            LOG.debug(
                "could not map failed resume %s onto board task %s",
                run_id,
                board_task_id,
                exc_info=True,
            )
        lifecycle.close()
        return {"run_id": run_id, "resumed": False, "reason": "thread start failed"}
    return {"run_id": run_id, "resumed": True, "reason": "manual" if manual else "auto"}


def resume_orphaned_orchestrations(
    *,
    store: Store,
    collab_store: CollabStore,
    limit: int = 8,
) -> list[str]:
    """Best-effort claim of stale or retryable checkpointed orchestrations."""
    lifecycle: OrchestrationsDal | None = None
    claimed: list[str] = []
    try:
        lifecycle = OrchestrationsDal(_sqlite_db_path(store))
        rows = lifecycle.find_resumable(
            stale_minutes=_orchestration_stale_minutes(),
            include_failed_retry=True,
            now=None,
        )
    except Exception:  # noqa: BLE001 -- reconciliation must survive resume scan failure.
        LOG.debug("orchestration resume scan failed", exc_info=True)
        return claimed
    finally:
        if lifecycle is not None:
            lifecycle.close()

    claim_limit = max(0, limit)
    if claim_limit == 0:
        return claimed
    for orchestration in rows:
        run_id = str(orchestration.get("id") or "")
        if _local_conductor_live(run_id):
            continue
        try:
            board_task_id = str(orchestration.get("board_task_id") or "")
            board = collab_store.get_board_task(board_task_id)
            if (
                board is None
                or board.get("lane") == "longhaul"
                or board.get("archived_at") is not None
                or str(board.get("status") or "")
                in {BoardTaskStatus.CANCELLED.value, BoardTaskStatus.DONE.value}
            ):
                continue
            with _RESUME_START_LOCK:
                if _live_resume_conductor_count() >= _MAX_RESUME_CONDUCTORS:
                    LOG.info(
                        "orchestration resume deferred: %s resume conductors live",
                        _MAX_RESUME_CONDUCTORS,
                    )
                    break
                if _local_conductor_live(run_id):
                    continue
                result = resume_orchestration(
                    board_task_id,
                    store=store,
                    collab_store=collab_store,
                    manual=False,
                )
            if result.get("resumed") is True:
                claimed.append(run_id)
                if len(claimed) >= claim_limit:
                    break
        except Exception:  # noqa: BLE001 -- one bad row must not stop later resumes.
            LOG.debug(
                "orchestration %s auto-resume failed",
                orchestration.get("id"),
                exc_info=True,
            )
    return claimed


def _queue_orchestration(
    collab_store: CollabStore,
    spec: RefinedSpec,
    *,
    runner: OrchestrateRunner,
    working_dir: str | None,
    project_id: str | None,
    priority: str,
    pins: dict[str, Any] | None,
    board_task_id: str | None,
    run_id: str | None,
    granted_roots: list[str] | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Create a visible queued card, then run orchestration away from the request.

    The direct ``dispatch_spec(..., execute="orchestrate")`` API remains
    synchronous for its existing callers.  The cockpit explicitly opts into this
    queueing mode so its HTTP request only creates trackable work; Fable planning,
    executor sessions, and quality review never occupy the browser connection.
    """
    queued_run_id = run_id or new_id("orch")
    existing = collab_store.get_board_task(board_task_id) if board_task_id else None
    if existing is None:
        board = BoardTask(
            title=spec.title,
            description=_compose_description(spec),
            required_expertise=list(spec.required_expertise),
            discipline=spec.suggested_discipline,
            priority=spec.suggested_priority,
            status=BoardTaskStatus.OPEN,
            result_ref=queued_run_id,
            project_id=project_id,  # M1: scoped by the INSERT, not by a later PATCH
        )
        collab_store.create_board_task(board)
        _prearchive_suppressed_card(collab_store, board.id, spec.title)
    else:
        board = BoardTask.model_validate(existing)
        collab_store.update_board_task(
            board.id,
            {
                "title": spec.title,
                "description": _compose_description(spec),
                "discipline": spec.suggested_discipline,
                "priority": spec.suggested_priority,
                "status": BoardTaskStatus.OPEN.value,
                "result_ref": queued_run_id,
            },
        )
    _persist_board_project_id(collab_store, board.id, project_id)
    resolved_db_path = db_path or _sqlite_db_path(collab_store)
    lifecycle = OrchestrationsDal(resolved_db_path)
    lifecycle.create(
        queued_run_id,
        board_task_id=board.id,
        working_dir=working_dir or "",
        goal=_compose_prompt(spec),
        params_json=_orchestration_params_json(
            priority=priority,
            pins=pins,
            project_id=project_id,
            granted_roots=granted_roots,
        ),
    )
    _emit_board_event(collab_store, board.id, run_id=queued_run_id)

    def run_in_background() -> None:
        try:
            _run_orchestration_with_lifecycle(
                lifecycle,
                collab_store,
                board_id=board.id,
                run_id=queued_run_id,
                db_path=resolved_db_path,
                runner=runner,
                goal=_compose_prompt(spec),
                priority=priority,
                pins=pins,
                working_dir=working_dir,
                project_id=project_id,
                granted_roots=granted_roots,
            )
        except Exception:  # noqa: BLE001 -- failures must be visible, not crash a worker.
            LOG.exception("queued orchestration %s failed", queued_run_id)

    worker = threading.Thread(target=run_in_background, name=f"intake-{queued_run_id}", daemon=True)
    if not _register_active_conductor(queued_run_id, worker):
        lifecycle.close()
        raise RuntimeError("conductor live")
    try:
        worker.start()
    except Exception as exc:
        _unregister_active_conductor(queued_run_id, worker)
        lifecycle.set_status(queued_run_id, "failed", error=str(exc) or "unknown error")
        collab_store.update_board_task(
            board.id,
            {"status": BoardTaskStatus.BLOCKED.value, "result_ref": queued_run_id},
        )
        _emit_board_event(collab_store, board.id, run_id=queued_run_id)
        lifecycle.close()
        raise
    board_row = collab_store.get_board_task(board.id) or {}
    return {
        "board_task": board_row,
        "task_id": None,
        "run_id": queued_run_id,
        "session_id": None,
        "execute": "orchestrate",
        "working_dir": working_dir,
        "project_id": project_id,
        "provisioned": False,
        "allowed_connectors": [],
        "orchestration": {
            "run_id": queued_run_id,
            "status": "queued",
            "priority": priority,
            "task_count": 0,
            "escalation_count": 0,
            "spec_note_path": None,
        },
    }


def _emit_board_event(
    collab_store: CollabStore,
    task_id: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> None:
    h1 = getattr(collab_store, "_store", None)
    if h1 is None or not hasattr(h1, "insert_event"):
        return
    try:
        payload: dict[str, Any] = {"task_id": task_id}
        if run_id is not None:
            payload["run_id"] = run_id
        if session_id is not None:
            payload["session_id"] = session_id
        h1.insert_event(
            "board.updated",
            "intake",
            "board.dispatched",
            target_type="board_task",
            target_id=task_id,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 -- event emission is best-effort telemetry.
        LOG.debug("intake board event emit failed", exc_info=True)


def _count_deliverable_files(workspace: str | None) -> int:
    """Best-effort count of a workspace's ``uploads/`` + ``outputs/`` files.

    Deliberately lightweight -- this is a display hint on the completion
    notification, NOT the file-serving board-files surface, so it does no
    containment/deny parity -- and hard-bounded so a runaway scratch dir can
    never make a task's completion hang.
    """
    if not workspace:
        return 0
    count = 0
    try:
        base = Path(workspace)
        for sub in ("uploads", "outputs"):
            directory = base / sub
            if not directory.is_dir():
                continue
            for entry in directory.rglob("*"):
                try:
                    if entry.is_file():
                        count += 1
                except OSError:
                    continue
                if count >= 5000:
                    return count
    except (OSError, RuntimeError, ValueError):
        return count
    return count


def _notify_task_done_safe(
    collab_store: CollabStore,
    board_task_id: str,
    *,
    workspace: str | None,
    run_id: str | None,
    session_id: str | None,
    db_path: str | None,
) -> None:
    """Emit the single 'task complete' notification for a finished card.

    Best-effort by contract: a card that just moved to DONE must never fail
    because its completion bell could not be recorded. The KIND-AWARE dedupe
    lives in :func:`omniagentos.notifications.service.notify_task_done`, so the
    orchestration-lifecycle path and the ``reconcile_board`` transition path can
    both call this for one completion without ever double-belling.
    """
    try:
        from omniagentos.notifications.service import notify_task_done

        title = ""
        try:
            card = collab_store.get_board_task(board_task_id)
            if card is not None:
                title = str(card.get("title") or "")
        except Exception:  # noqa: BLE001 -- the title is a nicety, not required.
            title = ""
        notify_task_done(
            board_task_id=board_task_id,
            task_title=title,
            files_count=_count_deliverable_files(workspace),
            workspace=workspace,
            run_id=run_id,
            session_id=session_id,
            db_path=db_path or default_db_path(),
        )
    except Exception:  # noqa: BLE001 -- the completion bell is best-effort telemetry.
        LOG.debug("task-done notification emit failed", exc_info=True)


def _resolve_done_workspace(store: Store, collab_store: CollabStore, task_id: str) -> str | None:
    """The card's resolved workspace path (str), or None -- for the done bell.

    Reuses the board-files resolver (lazy import to avoid an api<->intake import
    cycle at module load) so the notification's ``workspace``/``files_count``
    match exactly what the files drawer will show. Best-effort: any failure
    degrades to ``None`` (a completion bell with no file hint), never an error.
    """
    try:
        from omniagentos.api.routes.board_files import resolve_board_workspace

        _board, workspace = resolve_board_workspace(store, collab_store, task_id)
        return str(workspace) if workspace is not None else None
    except Exception:  # noqa: BLE001 -- workspace resolution is best-effort here.
        return None


def pause_board_task_work(
    store: Store,
    task: dict[str, Any],
    *,
    sessions_dal: Any = None,
) -> tuple[str | None, str | None]:
    """Best-effort pause of live work linked from a board card.

    The board must remain archivable when an unrelated worker database call fails,
    so each link is deliberately isolated.  Repeated calls are harmless: run
    cancellation is a durable flag and ``SessionsDal.request_cancel`` is idempotent.
    """
    paused_run: str | None = None
    paused_session: str | None = None

    run_id = task.get("run_id")
    if isinstance(run_id, str) and run_id:
        try:
            run = store.get_run(run_id)
            if run is not None and str(run.get("state")) not in {
                state.value for state in TERMINAL_RUN_STATES
            }:
                if store.request_cancel(run_id):
                    store.insert_event(
                        Events.AUDIT,
                        "api",
                        "run.cancel_requested",
                        target_type="run",
                        target_id=run_id,
                        payload={"run_id": run_id},
                        trace_id=str(run.get("trace_id", "")),
                    )
                    paused_run = run_id
        except Exception:  # noqa: BLE001 -- archiving is allowed despite pause failure.
            LOG.warning("board archive could not pause run %s", run_id, exc_info=True)

    session_id = task.get("result_ref")
    if _is_session_ref(session_id) and sessions_dal is not None:
        try:
            session = sessions_dal.get_session(session_id)
            if session is not None and str(session.get("state")) not in {
                SessionState.COMPLETED.value,
                SessionState.FAILED.value,
                SessionState.CANCELLED.value,
                SessionState.KILLED.value,
            }:
                if sessions_dal.request_cancel(session_id):
                    paused_session = session_id
        except Exception:  # noqa: BLE001 -- archiving is allowed despite pause failure.
            LOG.warning("board archive could not pause session %s", session_id, exc_info=True)

    # Orchestrate-lane cards link their work through result_ref = orch_… (the
    # run_id/ses_ branches above never fire for them), so pause/archive used
    # to leave the conductor and its live step session running — the exact
    # "cannot pause from the board" gap. Cancel the live step sessions, then
    # mark the orchestration cancelled (resumable later via the existing
    # retry/resume endpoint, which passes manual=True).
    orch_ref = task.get("result_ref")
    if isinstance(orch_ref, str) and orch_ref.startswith("orch_") and sessions_dal is not None:
        try:
            from omniagentos.intake.orchestrations import OrchestrationsDal

            lifecycle = OrchestrationsDal(_sqlite_db_path(store))
            try:
                orch = lifecycle.get(orch_ref)
                if orch is not None and str(orch.get("status")) not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    resume_state = lifecycle.load_resume_state(orch_ref)
                    for step in resume_state.steps if resume_state else []:
                        step_session = getattr(step, "session_id", None)
                        if not (isinstance(step_session, str) and step_session):
                            continue
                        live = sessions_dal.get_session(step_session)
                        if live is not None and str(live.get("state")) not in {
                            SessionState.COMPLETED.value,
                            SessionState.FAILED.value,
                            SessionState.CANCELLED.value,
                            SessionState.KILLED.value,
                        }:
                            if sessions_dal.request_cancel(step_session):
                                paused_session = paused_session or step_session
                    lifecycle.set_status(orch_ref, "cancelled", error="paused from board")
                    paused_run = paused_run or orch_ref
            finally:
                lifecycle.close()
        except Exception:  # noqa: BLE001 -- pause stays best-effort per link.
            LOG.warning("could not pause orchestration %s", orch_ref, exc_info=True)

    return paused_run, paused_session


# Run state -> board column. Terminal-completed lands in Done; a failed run is
# surfaced as blocked rather than vanishing; queued mirrors the open card.
_RUN_TO_BOARD: dict[RunState, BoardTaskStatus] = {
    RunState.QUEUED: BoardTaskStatus.OPEN,
    RunState.RUNNING: BoardTaskStatus.IN_PROGRESS,
    RunState.VALIDATING: BoardTaskStatus.IN_PROGRESS,
    # Same rule as sessions: parked on a human decision is not "in progress".
    # Distinct from BLOCKED, which means FAILED.
    RunState.AWAITING_APPROVAL: BoardTaskStatus.AWAITING_APPROVAL,
    RunState.PAUSED: BoardTaskStatus.IN_PROGRESS,
    RunState.COMPLETED: BoardTaskStatus.DONE,
    RunState.FAILED: BoardTaskStatus.BLOCKED,
    RunState.CANCELLED: BoardTaskStatus.CANCELLED,
}

# Fast-lane / execute="session" cards link a live session via ``result_ref`` (a
# ``ses_`` id), NOT a run -- so the board must project SESSION state onto the card,
# else a completed fast task is stuck forever in To-Do (its session finished but the
# run-only reconciler never looked at it).
_SESSION_TO_BOARD: dict[SessionState, BoardTaskStatus] = {
    SessionState.STARTING: BoardTaskStatus.IN_PROGRESS,
    SessionState.RUNNING: BoardTaskStatus.IN_PROGRESS,
    SessionState.RESUMING: BoardTaskStatus.IN_PROGRESS,
    # NOT in_progress: a session parked on a human decision is live but cannot
    # advance, and collapsing it into in_progress is what made "waiting for you"
    # invisible on the board. Distinct from BLOCKED, which means FAILED below.
    SessionState.AWAITING_APPROVAL: BoardTaskStatus.AWAITING_APPROVAL,
    SessionState.COMPLETED: BoardTaskStatus.DONE,
    SessionState.FAILED: BoardTaskStatus.BLOCKED,
    SessionState.CANCELLED: BoardTaskStatus.CANCELLED,
    SessionState.KILLED: BoardTaskStatus.CANCELLED,
}

_ORCH_TO_BOARD: dict[str, BoardTaskStatus] = {
    "queued": BoardTaskStatus.OPEN,
    "planning": BoardTaskStatus.IN_PROGRESS,
    "running": BoardTaskStatus.IN_PROGRESS,
    "completed": BoardTaskStatus.DONE,
    "failed": BoardTaskStatus.BLOCKED,
    "cancelled": BoardTaskStatus.CANCELLED,
}


def _is_session_ref(result_ref: Any) -> bool:
    return isinstance(result_ref, str) and result_ref.startswith("ses_")


def _is_orchestration_ref(result_ref: Any) -> bool:
    return isinstance(result_ref, str) and result_ref.startswith("orch_")


def _session_todos(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw = session.get("todos_json")
    if isinstance(raw, str) and raw.strip():
        try:
            todos = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(todos, list):
            return [todo for todo in todos if isinstance(todo, dict)]
    return []


def _session_progress(session: dict[str, Any]) -> dict[str, int]:
    """Board-card progress from the session's captured TodoWrite checklist."""
    todos = _session_todos(session)
    done = sum(1 for todo in todos if todo.get("status") == "completed")
    total = len(todos)
    return {"steps_done": done, "steps_total": total}


def _session_files_count(session: dict[str, Any]) -> int:
    raw = session.get("files_json")
    if isinstance(raw, list):
        return len(raw)
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        files = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return 0
    return len(files) if isinstance(files, list) else 0


def _cost(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _run_map(store: Store, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not run_ids:
        return {}
    # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
    try:
        return cast(Any, store).get_runs_by_ids(run_ids)
    except Exception:  # noqa: BLE001 -- a bad link must not break the whole board read.
        LOG.debug("intake could not batch-read linked runs", exc_info=True)
        return {}


def _step_count_map(store: Store, run_ids: list[str]) -> dict[str, tuple[int, int]]:
    if not run_ids:
        return {}
    # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
    try:
        return cast(Any, store).get_step_counts(run_ids)
    except Exception:  # noqa: BLE001 -- progress is optional board context.
        LOG.debug("intake could not batch-read linked run progress", exc_info=True)
        return {}


def _pending_approval_map(store: Store) -> dict[str, dict[str, Any]]:
    """Pending approvals keyed by the session or run they park.

    ONE query for the whole board -- the board is a hot path and this must never
    become a per-card read. Keys are ``ses_…`` for session-parked approvals and
    ``run:run_…`` for runner-parked approvals so both card kinds can bind one
    blocker without a second query. Cards with no pending approval are absent.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = cast(Any, store).list_approvals(state="pending", limit=200)
    except Exception:  # noqa: BLE001 -- approval context is optional board decoration.
        LOG.debug("intake could not batch-read pending approvals", exc_info=True)
        return out
    for row in rows:
        session_id = str(row.get("session_id") or "")
        if session_id and session_id not in out:
            out[session_id] = row
        run_id = str(row.get("run_id") or "")
        if run_id:
            run_key = f"run:{run_id}"
            if run_key not in out:
                out[run_key] = row
    return out


def _approval_command(approval: dict[str, Any]) -> str:
    """The command a human is actually being asked to allow.

    ``proposed_action`` is the tool NAME (literally ``"Bash"`` on every one of the
    98 expired approvals in production), which is undecidable on its own. The real
    command lives in ``params_json``.
    """
    raw = approval.get("params_json")
    params: dict[str, Any] = {}
    if isinstance(raw, dict):
        params = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            params = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            params = {}
    for key in ("command", "cmd", "input"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(part) for part in value)
    description = params.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return str(approval.get("proposed_action") or "")


def _persist_board_project_id(
    collab_store: CollabStore, board_id: str, project_id: str | None
) -> None:
    """Stamp ``project_id`` onto a board card (Chat v2 §3.8 write path).

    Read-time joins can't cover session-mode cards (no run links them to a
    project), so dispatch persists it at creation. No-op when the migration
    087 column is missing — the I7 merge-order contract boots either way.
    """
    if not project_id:
        return
    try:
        collab_store.update_board_task(board_id, {"project_id": project_id})
    except Exception:  # noqa: BLE001
        LOG.debug("board project_id persist skipped for %s", board_id, exc_info=True)


def _chat_origin_map(store: Store) -> dict[str, dict[str, Any]]:
    """ONE query for the whole board: board_task_id → {chat_id, title}.

    Feeds both the companion-exclusion (P0-9: only cards pointed at by a chat
    are hidden) and the ``chat_origin`` card field.
    """
    try:
        rows = (
            cast(Any, store)
            ._connection.execute(
                "SELECT board_task_id, id, title FROM chats "
                "WHERE status != 'deleted' AND board_task_id IS NOT NULL"
            )
            .fetchall()
        )
    except Exception:  # noqa: BLE001 -- chats table absent in minimal stores
        return {}
    return {
        str(row["board_task_id"]): {"chat_id": str(row["id"]), "title": row["title"]}
        for row in rows
    }


def _is_run_ref(result_ref: Any) -> bool:
    return isinstance(result_ref, str) and result_ref.startswith("run_")


# The two per-card attempt ledgers. Both carry (board_task_id, seq, ended_at,
# end_reason, detail) with the same meaning, so one shape reads both: swarm
# cards write ``swarm_attempts`` (migration 045), longhaul cards write
# ``task_sessions`` (043).
_ATTEMPT_LEDGERS: tuple[tuple[str, str], ...] = (
    ("swarm_attempts", "swarm_attempt"),
    ("task_sessions", "task_session"),
)
# End reasons that are NOT a failure mode, and therefore never an answer to
# "why is this card stuck". ``completed`` is the ledgers' one success token
# (``swarm.costgreen.GREEN_REASON``, ``router._LEARN_WIN_REASONS``); the other
# three record an attempt that ENDED because the work moved elsewhere — split
# into sub-tasks, rerouted to another provider, or superseded by a newer
# attempt — not because anything broke. A blocked card whose latest attempt
# ends this way has no stated reason, and saying so is the honest answer: the
# projection falls through to the linked run's own error text, or to null.
_NON_FAILURE_END_REASONS = frozenset({"completed", "split", "rerouted", "superseded"})
# One line of "why", not a transcript: attempt ``detail`` is free text and has
# been observed holding whole stack traces.
_BLOCKED_DETAIL_MAX = 240
# Well below SQLite's 32766 variable ceiling, and small enough that a 1200-card
# board is a handful of queries rather than one giant IN list.
_ATTEMPT_CHUNK = 400


def _short_detail(value: Any) -> str | None:
    """First non-empty line of an attempt/run detail, truncated. None when empty."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return None
    return first if len(first) <= _BLOCKED_DETAIL_MAX else first[: _BLOCKED_DETAIL_MAX - 1] + "…"


def _blocked_reason_map(store: Store, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """board_task_id → the last ENDED attempt that recorded an ``end_reason``.

    Why this exists: 617 of 1208 live cards sat in ``status='blocked'`` and only
    ~8 of them carried a ``run_error``, so the board could not say why any of
    the rest were stuck — the answer was already written down one table over, in
    the attempt ledger the lane used. This is a READ-ONLY projection of that
    ledger (no schema change, no backfill); cards whose lane never recorded an
    end reason stay honestly absent from the map.

    ONLY FAILURE MODES. The latest attempt is chosen first and filtered second,
    never the other way round: a card whose latest attempt ended
    ``completed`` (or ``split``/``rerouted``/``superseded`` — see
    :data:`_NON_FAILURE_END_REASONS`) is dropped from the map entirely rather
    than falling back to an older failure. "The last thing that happened was
    not a failure" and "the last FAILURE was X" are different claims, and only
    the first one is true; surfacing the older crash would tell a reader the
    card is stuck on something it already moved past.

    Batched per ledger, never per card — the board is a hot path.
    """
    unique = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    if not unique:
        return {}
    connection = getattr(store, "_connection", None)
    if connection is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for table, source in _ATTEMPT_LEDGERS:
        for start in range(0, len(unique), _ATTEMPT_CHUNK):
            chunk = unique[start : start + _ATTEMPT_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = connection.execute(
                    f"SELECT board_task_id, seq, end_reason, detail, ended_at FROM {table} "  # noqa: S608 -- table name is a module constant, ids are bound
                    f"WHERE board_task_id IN ({placeholders}) "
                    "AND end_reason IS NOT NULL AND end_reason != '' "
                    "ORDER BY board_task_id ASC, seq ASC",
                    tuple(chunk),
                ).fetchall()
            except Exception:  # noqa: BLE001 -- ledger absent in minimal/legacy stores
                LOG.debug("intake could not read %s for blocked reasons", table, exc_info=True)
                break
            for row in rows:
                task_id = str(row["board_task_id"])
                candidate = {
                    "reason": str(row["end_reason"]),
                    "detail": _short_detail(row["detail"]),
                    "source": source,
                    "at": row["ended_at"],
                }
                previous = out.get(task_id)
                # Rows arrive in ascending seq, so the later row of the SAME
                # ledger always wins. Across ledgers (a card that changed lanes)
                # the later end timestamp wins; a missing timestamp never
                # displaces a dated one.
                if (
                    previous is None
                    or previous["source"] == source
                    or (str(candidate["at"] or "") > str(previous["at"] or ""))
                ):
                    out[task_id] = candidate
    return {
        task_id: entry
        for task_id, entry in out.items()
        if str(entry["reason"]) not in _NON_FAILURE_END_REASONS
    }


def _enrich_board_row(
    store: Store,
    row: dict[str, Any],
    session_map: dict[str, dict[str, Any]] | None = None,
    run_map: dict[str, dict[str, Any]] | None = None,
    step_count_map: dict[str, tuple[int, int]] | None = None,
    orchestration_map: dict[str, dict[str, Any]] | None = None,
    approval_map: dict[str, dict[str, Any]] | None = None,
    chat_map: dict[str, dict[str, Any]] | None = None,
    blocked_reason_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach compatibility fields and normalized work context to a board row.

    All maps are prefetched for a board load, so this function performs no per-card
    database reads on the live board path."""
    out = dict(row)
    run_id = str(row.get("run_id") or "")
    ref = row.get("result_ref")
    if run_map is None:
        run_map = _run_map(store, [run_id] if run_id else [])
    if step_count_map is None:
        step_count_map = _step_count_map(store, [run_id] if run_id else [])
    run = run_map.get(run_id)

    work: dict[str, Any] = {
        "kind": None,
        "state": None,
        "agent": None,
        "steps_done": 0,
        "steps_total": 0,
        "current_step": None,
        "files_count": None,
        "cost_usd": None,
        "last_activity_at": None,
        "error": None,
    }
    if run_id:
        work["kind"] = "run"
        if run is not None:
            worker = run.get("worker_id")
            agent = worker or run.get("agent") or run.get("harness")
            done, total = step_count_map.get(run_id, (0, 0))
            work.update(
                state=run.get("state"),
                agent=str(agent) if agent else None,
                steps_done=done,
                steps_total=total,
                cost_usd=_cost(run.get("cost_usd")),
                last_activity_at=run.get("updated_at"),
                error=run.get("error"),
            )
    elif _is_session_ref(ref):
        work["kind"] = "session"
        session = session_map.get(str(ref)) if session_map is not None else None
        if session is not None:
            progress = _session_progress(session)
            current = next(
                (
                    str(todo.get("content"))
                    for todo in _session_todos(session)
                    if todo.get("status") == "in_progress" and todo.get("content")
                ),
                None,
            )
            # Prefer model; fall back to provider so external multi-provider
            # cards (process-discovered, often model=NULL) still show who is working.
            agent = session.get("model") or session.get("provider")
            work.update(
                state=session.get("state"),
                agent=str(agent) if agent else None,
                steps_done=progress["steps_done"],
                steps_total=progress["steps_total"],
                current_step=current,
                files_count=_session_files_count(session),
                cost_usd=_cost(session.get("cost_usd")),
                last_activity_at=session.get("last_activity_at"),
                error=session.get("error"),
            )
    elif _is_orchestration_ref(ref):
        work["kind"] = "orchestration"
        orchestration = orchestration_map.get(str(ref)) if orchestration_map is not None else None
        if orchestration is not None:
            work.update(
                state=orchestration.get("status"),
                current_step=orchestration.get("stage"),
                last_activity_at=(
                    orchestration.get("heartbeat_at") or orchestration.get("updated_at")
                ),
                error=orchestration.get("error"),
            )

    out["work"] = work
    out["run_state"] = work["state"]
    out["run_agent"] = work["agent"]
    out["run_progress"] = (
        {
            "steps_done": work["steps_done"],
            "steps_total": work["steps_total"],
        }
        if work["kind"] is not None
        else None
    )
    out["run_error"] = work["error"]
    # Chat v2 (§3.9): the four fields the kanban dock + everything panel need.
    # ``project_id`` comes from the 087 column (NULL on pre-087 cards with no
    # run/chat link — honesty over heuristic backfills).
    out["project_id"] = row.get("project_id")
    raw_org = row.get("org")
    org_env: dict[str, Any] = raw_org if isinstance(raw_org, dict) else {}
    brief = org_env.get("planner_brief")
    out["planner_brief"] = brief if isinstance(brief, str) and brief.strip() else None
    chat_row = (chat_map or {}).get(str(row.get("id", "")))
    out["chat_origin"] = (
        {"chat_id": str(chat_row["chat_id"]), "title": chat_row.get("title")}
        if chat_row is not None
        else None
    )
    out["checklist"] = (
        {"done": work["steps_done"], "total": work["steps_total"]}
        if work["steps_total"] > 0
        else None
    )
    # Longhaul projection fields are additive.  The longhaul engine remains the
    # owner of status/park transitions; this read path only exposes its state.
    out["category_id"] = row.get("category_id")
    out["lane"] = row.get("lane")
    out["park_state"] = row.get("park_state")
    # The blocker itself, so the card can offer Approve without a second round-trip.
    out["pending_approval"] = None
    if approval_map:
        approval = None
        if _is_session_ref(ref):
            approval = approval_map.get(str(ref))
        if approval is None and run_id:
            approval = approval_map.get(f"run:{run_id}")
        if approval is not None:
            out["pending_approval"] = {
                "id": str(approval.get("id") or ""),
                "command": _approval_command(approval),
                "action_class": str(approval.get("action_class") or ""),
                "risk": approval.get("risk"),
                "created_at": approval.get("created_at"),
            }
    # Attribution (read-only projection; never a write). ``run_id`` and
    # ``claimed_by`` come off the card's own columns when set. A card dispatched
    # into the runs lane records the run in ``result_ref`` and leaves the column
    # NULL, so the linkage is projected from the ref rather than left blank —
    # the value is the SAME run id either way, so nothing is invented here.
    out["run_id"] = row.get("run_id") or (str(ref) if _is_run_ref(ref) else None)
    out["claimed_by"] = row.get("claimed_by")
    # Best-effort "why is this card stuck" (read-only). Only ever populated for
    # cards that are actually blocked: an ``end_reason`` of ``completed`` on a
    # done card is not a blocked reason, and naming it one would be a lie the UI
    # would render. Attempt ledger first (it names the failure mode), then the
    # linked run's own error text.
    out["blocked_reason"] = None
    if str(row.get("status") or "") == BoardTaskStatus.BLOCKED.value:
        human_reason = str(row.get("blocked_reason") or "").strip()
        entry = (blocked_reason_map or {}).get(str(row.get("id") or ""))
        if human_reason:
            # A human-authored reason (board_tasks.blocked_reason, migration 123)
            # outranks the inferred attempt ledger: the owner said why it is stuck.
            out["blocked_reason"] = {
                "reason": "human",
                "detail": human_reason,
                "source": "owner",
                "at": row.get("updated_at"),
            }
        elif entry is not None:
            out["blocked_reason"] = dict(entry)
        elif work["error"]:
            out["blocked_reason"] = {
                "reason": "error",
                "detail": _short_detail(work["error"]),
                "source": work["kind"] or "run",
                "at": work["last_activity_at"],
            }
    out["category"] = None
    try:
        connection = cast(Any, store)._connection
        if row.get("category_id"):
            cat_row = connection.execute(
                "SELECT id, name, color FROM task_categories WHERE id = ?",
                (str(row["category_id"]),),
            ).fetchone()
            if cat_row:
                out["category"] = {
                    "id": cat_row["id"],
                    "name": cat_row["name"],
                    "color": cat_row["color"],
                }
        attempt_row = connection.execute(
            "SELECT COUNT(*) AS count FROM task_sessions "
            "WHERE board_task_id = ? AND ended_at IS NOT NULL",
            (str(row.get("id", "")),),
        ).fetchone()
        out["attempt_count"] = int(attempt_row["count"]) if attempt_row else 0
    except Exception:  # noqa: BLE001 -- legacy/test stores may not have W1 tables.
        out["attempt_count"] = 0
    return out


_RECONCILE_DAL: SessionsDal | None = None
_RECONCILE_DAL_LOCK = threading.Lock()
_RECONCILE_ORCH_DALS: dict[str, OrchestrationsDal] = {}
_RECONCILE_ORCH_DALS_LOCK = threading.Lock()
_RECONCILE_STALE_CHECK_LOCK = threading.Lock()
_RECONCILE_STALE_CHECK_AT: dict[str, float] = {}
_RECONCILE_STALE_CHECK_SECONDS = 30.0


def _reconcile_sessions_dal() -> SessionsDal:
    """Process-lifetime SessionsDal for board-reconciliation reads.

    Constructing a SessionsDal re-runs ``migrate_connection()`` every time; doing that
    on every ``/api/board`` poll under concurrent-session write load wedged the board
    (10s timeout). This cached read connection is migrated once; WAL lets it see
    committed session writes without blocking the writers."""
    global _RECONCILE_DAL
    with _RECONCILE_DAL_LOCK:
        if _RECONCILE_DAL is None:
            _RECONCILE_DAL = SessionsDal(default_db_path())
        return _RECONCILE_DAL


def _reconcile_orchestrations_dal(db_path: str) -> OrchestrationsDal:
    with _RECONCILE_ORCH_DALS_LOCK:
        dal = _RECONCILE_ORCH_DALS.get(db_path)
        if dal is None:
            dal = OrchestrationsDal(db_path)
            _RECONCILE_ORCH_DALS[db_path] = dal
        return dal


def _orchestration_stale_minutes() -> int:
    try:
        return max(0, int(os.environ.get("OMNIAGENTOS_ORCH_STALE_MINUTES", "10")))
    except ValueError:
        LOG.warning("invalid OMNIAGENTOS_ORCH_STALE_MINUTES; using default 10")
        return 10


def _claim_reconcile_stale_check(db_path: str) -> bool:
    now = time.monotonic()
    with _RECONCILE_STALE_CHECK_LOCK:
        last_check = _RECONCILE_STALE_CHECK_AT.get(db_path, float("-inf"))
        if now - last_check < _RECONCILE_STALE_CHECK_SECONDS:
            return False
        _RECONCILE_STALE_CHECK_AT[db_path] = now
        return True


def _reset_reconcile_stale_throttle(db_path: str | None = None) -> None:
    """Force the next stale check; intended for deterministic tests."""
    with _RECONCILE_STALE_CHECK_LOCK:
        if db_path is None:
            _RECONCILE_STALE_CHECK_AT.clear()
        else:
            _RECONCILE_STALE_CHECK_AT.pop(db_path, None)


def _human_terminal_regression(task: dict[str, Any], desired: BoardTaskStatus) -> bool:
    return task.get("status") in {
        BoardTaskStatus.DONE.value,
        BoardTaskStatus.CANCELLED.value,
    } and desired in {BoardTaskStatus.OPEN, BoardTaskStatus.IN_PROGRESS}


def _metacog_evaluate_active_tasks(
    tasks: list[dict[str, Any]],
    step_counts: dict[str, tuple[int, int]] | None = None,
    run_map: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Run Grok metacog evaluate on live board cards (self-repair signal).

    Records progress/stall snapshots; in enforce mode strategy decisions are
    available via /api/metacog. ``step_counts`` is the batched progress map
    already assembled by :func:`reconcile_board`; absent counts are honestly
    recorded as unmeasured rather than inferred from a board status. Never
    raises to callers.
    """
    from omniagentos.metacog.service import MetacogService

    active = [
        t
        for t in tasks
        if str(t.get("status") or "")
        in {
            BoardTaskStatus.IN_PROGRESS.value,
            BoardTaskStatus.CLAIMED.value,
            BoardTaskStatus.OPEN.value,
            BoardTaskStatus.BLOCKED.value,
        }
    ][:25]
    if not active:
        return
    # Kept in the boundary so the reconciler's already-fetched run identity is
    # available when more run-level signals are added. Step counts themselves
    # remain sufficient evidence of a measured quantitative signal.
    _ = run_map
    svc = MetacogService()
    for task in active:
        tid = str(task.get("id") or "")
        if not tid:
            continue
        status = str(task.get("status") or "")
        run_id = str(task.get("run_id") or "")
        passed = 0
        total = 0
        progress_measured = False
        if run_id and step_counts is not None:
            steps = step_counts.get(run_id)
            if steps is not None:
                passed, total = steps
                progress_measured = True

        # A terminal board state is strong evidence even without a linked run.
        # Active cards without step counts stay unmeasured; status alone is not
        # a quantitative progress signal.
        if not progress_measured and status == BoardTaskStatus.DONE.value:
            passed, total = 1, 1
            progress_measured = True
        try:
            svc.evaluate(
                task_id=tid,
                run_id=run_id or str(task.get("result_ref") or "") or None,
                criteria_total=total,
                criteria_passed=passed,
                progress_measured=progress_measured,
                strategy_id="grok-orchestrator",
                context_pressure=0.3 if status != BoardTaskStatus.BLOCKED.value else 0.7,
                recent_outputs=[str(task.get("title") or "")],
            )
        except Exception:  # noqa: BLE001
            LOG.debug("metacog evaluate failed for %s", tid, exc_info=True)


def _reconcile_card_fields(
    task: dict[str, Any],
    *,
    runs: dict[str, dict[str, Any]],
    session_map: dict[str, dict[str, Any]],
    orchestration_map: dict[str, dict[str, Any]],
    stale_ids: set[str],
) -> dict[str, Any]:
    """The column writes ONE card's linked work implies. Pure: reads, no writes.

    Extracted verbatim from the board loop so the single-card read
    (:func:`reconcile_board_card`) reconciles a card exactly the way the list
    read does — two implementations of "which column is this card in" is how a
    drawer and a kanban start disagreeing about the same card.
    """
    # Longhaul's journal owns status and parking.  Projecting a linked failed
    # session here would incorrectly turn waiting_capacity into blocked.
    if task.get("lane") == "longhaul":
        return {}
    # Swarm's coordinator owns its members' status (claims, attempts, blocked
    # propagation); the generic reconciler must never fight it.
    if task.get("swarm_run_id"):
        return {}
    fields: dict[str, Any] = {}
    desired: BoardTaskStatus | None = None
    run = runs.get(str(task.get("run_id")))
    if run is not None:
        try:
            desired = _RUN_TO_BOARD.get(RunState(str(run["state"])))
        except ValueError:
            desired = None
        if desired is None:
            return {}
        if task.get("status") != desired.value:
            fields["status"] = desired.value
        if desired == BoardTaskStatus.DONE and not task.get("result_ref"):
            fields["result_ref"] = str(run["id"])
        return fields
    ref = str(task.get("result_ref") or "")
    session = session_map.get(ref)
    orchestration = orchestration_map.get(ref)
    if session is not None:
        try:
            desired = _SESSION_TO_BOARD.get(SessionState(str(session["state"])))
        except ValueError:
            desired = None
    elif orchestration is not None:
        desired = _ORCH_TO_BOARD.get(str(orchestration.get("status")))
        if ref in stale_ids:
            description = str(task.get("description") or "")
            if _ORCHESTRATION_DIED_MARKER not in description:
                fields["description"] = description + _ORCHESTRATION_DIED_MARKER
    if (
        desired is not None
        and task.get("status") != desired.value
        and not _human_terminal_regression(task, desired)
    ):
        fields["status"] = desired.value
    return fields


def _apply_card_reconcile(
    store: Store,
    collab_store: CollabStore,
    task: dict[str, Any],
    fields: dict[str, Any],
    *,
    stale_ids: set[str],
    db_path: str,
) -> None:
    """Persist one card's reconciled fields, with the bell + event seams intact."""
    if not fields:
        return
    try:
        collab_store.update_board_task(str(task["id"]), fields)
        task.update(fields)
        # C0: emit the "finished files" bell ONLY on a transition this
        # reconcile writes TO done -- ``fields["status"]`` is set solely
        # when the card was NOT already done, so we never re-bell on
        # merely OBSERVING an already-done card. Kind-aware dedupe in
        # notify_task_done covers the race with the lifecycle path.
        if fields.get("status") == BoardTaskStatus.DONE.value:
            task_id = str(task["id"])
            result_ref = str(task.get("result_ref") or "")
            _notify_task_done_safe(
                collab_store,
                task_id,
                workspace=_resolve_done_workspace(store, collab_store, task_id),
                run_id=str(task.get("run_id")) if task.get("run_id") else None,
                session_id=result_ref if _is_session_ref(result_ref) else None,
                db_path=db_path,
            )
        if str(task.get("result_ref") or "") in stale_ids:
            _emit_board_event(
                collab_store,
                str(task["id"]),
                run_id=str(task["result_ref"]),
            )
    except ValueError:
        LOG.debug("intake reconcile skipped illegal board update", exc_info=True)


def reconcile_board_card(
    store: Store,
    collab_store: CollabStore,
    task_id: str,
    sessions_dal: Any = None,
    orchestrations_dal: Any = None,
) -> dict[str, Any] | None:
    """ONE reconciled card, shaped like a ``reconcile_board`` element.

    The task-details drawer used to download the WHOLE board (2.66 MB, and a
    second time for the archived feed) to find one row. This reads only that
    row's own linkages: at most one run, one session, one orchestration, one
    attempt-ledger lookup.

    Returns ``None`` when the card does not exist. The payload is EQUAL to a
    list element, never a superset: this route is served unauthenticated, like
    the list it mirrors, so it deliberately exposes nothing the already-public
    list does not — the raw ``swarm_json`` envelope is dropped and only the two
    flat swarm fields the list projects survive (see the pop below), and the
    company enrichment the list applies is applied here too.

    Deliberately skips the whole-board side quests (external-session discovery,
    stale-orchestration sweeps, metacog ticks): those are board-level
    maintenance, and running them per drawer open would make opening a card
    cost more than reading the board.
    """
    row = collab_store.get_board_task(task_id)
    if row is None:
        return None
    if sessions_dal is None:
        sessions_dal = _reconcile_sessions_dal()

    ref = row.get("result_ref")
    session_map: dict[str, dict[str, Any]] = {}
    if _is_session_ref(ref):
        try:
            session_map = sessions_dal.get_sessions_by_ids([str(ref)])
        except Exception:  # noqa: BLE001 -- a bad link must not break the read
            LOG.debug("intake could not read the card's session", exc_info=True)
    db_path = _sqlite_db_path(store)
    orchestration_map: dict[str, dict[str, Any]] = {}
    if _is_orchestration_ref(ref):
        if orchestrations_dal is None:
            orchestrations_dal = _reconcile_orchestrations_dal(db_path)
        try:
            orchestration_map = orchestrations_dal.get_by_ids([str(ref)])
        except Exception:  # noqa: BLE001
            LOG.debug("intake could not read the card's orchestration", exc_info=True)
    run_id = str(row.get("run_id") or "")
    runs = _run_map(store, [run_id]) if run_id else {}
    step_counts = _step_count_map(store, [run_id]) if run_id else {}

    fields = _reconcile_card_fields(
        row,
        runs=runs,
        session_map=session_map,
        orchestration_map=orchestration_map,
        # Stale-orchestration marking is a board-sweep responsibility; a single
        # card read observes it, never declares it.
        stale_ids=set(),
    )
    _apply_card_reconcile(store, collab_store, row, fields, stale_ids=set(), db_path=db_path)

    blocked_reasons = (
        _blocked_reason_map(store, [str(row.get("id") or "")])
        if str(row.get("status") or "") == BoardTaskStatus.BLOCKED.value
        else {}
    )
    # The SAME company enrichment the list read applies (one project row, one
    # company row for this card). Without it a card carries
    # ``org.organization_context.company_*`` in the list and not in the drawer,
    # which is exactly the kind of drift the shared ``_reconcile_card_fields``
    # extraction exists to prevent.
    from omniagentos.orgdims.enrich import enrich_company_from_projects

    connection = getattr(store, "_connection", None)
    if connection is not None:
        enrich_company_from_projects([row], connection)
    card = _enrich_board_row(
        store,
        row,
        session_map,
        runs,
        step_counts,
        orchestration_map,
        _pending_approval_map(store),
        _chat_origin_map(store),
        blocked_reasons,
    )
    # Same two swarm fields the LIST projection flattens out of the envelope
    # (``json_extract`` server-side), so a reader written against the list shape
    # needs no special case here.
    #
    # The raw ``swarm_json`` envelope itself is dropped, deliberately. Single-row
    # reads could afford its size, but this route is served UNAUTHENTICATED (like
    # the board list it mirrors), and the envelope carries working detail the
    # list does not — acceptance text, verify commands, per-attempt dirty file
    # paths. Keeping the single-card payload equal to a list element means this
    # route exposes nothing the already-public list does not.
    envelope = card.pop("swarm_json", None)
    if isinstance(envelope, str) and envelope.strip():
        try:
            envelope = json.loads(envelope)
        except (TypeError, json.JSONDecodeError):
            envelope = None
    if not isinstance(envelope, dict):
        envelope = {}
    card["swarm_phase"] = envelope.get("swarm_phase")
    card["swarm_integration"] = envelope.get("integration") is True
    return card


def reconcile_board(
    store: Store,
    collab_store: CollabStore,
    archived: int = 0,
    sessions_dal: Any = None,
    orchestrations_dal: Any = None,
    *,
    statuses: list[str] | None = None,
    updated_after: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Project every linked run's state onto its board card, then return the live board.

    This is the live-kanban read path: it persists the column each linked card
    belongs in (so other consumers of ``board_tasks`` see the same truth) and
    returns rows enriched with the run's state, working agent, and step progress.
    Hand-created cards with no ``run_id`` are returned untouched.

    Also (throttled) discovers interactive agent CLI processes started outside
    OmniAgentOS — Claude, Codex, Gemini, Grok, Kimi, Aider, Cursor Agent —
    and projects them onto the board as ``source=external`` session cards so
    every active multi-provider terminal is visible on the Kanban.

    Args:
        store: the H1 store
        collab_store: the collab store
        archived: 0 (default) = exclude archived, 1 = only archived
        statuses: optional server-side status filter (``status IN (…)``)
        updated_after: optional inclusive ``updated_at >= ?`` ISO-8601 bound
        limit: optional server-side row cap, newest card first

    The three bounding arguments are keyword-only and default to ``None``, which
    issues the SAME unbounded query this function has always issued — a caller
    that passes none of them gets today's feed, card for card. When one IS
    passed it is pushed down to SQL, so a bounded read also reconciles (and
    writes) only the cards it selected: an explicitly bounded read is a bounded
    amount of work, not a full board scan with a slice on the end.
    """
    if sessions_dal is None:
        # Cached, process-lifetime connection: constructing a SessionsDal runs
        # migrate_connection() every time, and doing that per /api/board poll under
        # concurrent-session write load wedged the board (10s timeout). Reused here,
        # never closed.
        sessions_dal = _reconcile_sessions_dal()

    # Multi-provider external terminal projection (best-effort, throttled).
    # Never allowed to break the board read path. Gated on discovery_enabled()
    # so hermetic tests (OMNIAGENTOS_DISCOVER_EXTERNAL=0) keep a single
    # list_board_tasks query and never contend for writer locks.
    if archived == 0:
        try:
            from omniagentos.sessions.discover import discovery_enabled
            from omniagentos.sessions.external_board import (
                sync_external_sessions_to_board_safe,
            )

            if discovery_enabled():
                sync_external_sessions_to_board_safe(
                    sessions_dal,
                    collab_store,
                    db_key=_sqlite_db_path(store),
                )
        except Exception:  # noqa: BLE001
            LOG.debug("external session board sync skipped", exc_info=True)

    # Only pass the bounding arguments when a caller actually set one: in-memory
    # CollabStore doubles (tests/lab) predate them, the same capability-sniffing
    # discipline the collab route uses for ``limit``.
    select_kwargs: dict[str, Any] = {}
    if statuses is not None:
        select_kwargs["statuses"] = statuses
    if updated_after is not None:
        select_kwargs["updated_after"] = updated_after
    if limit is not None:
        select_kwargs["limit"] = limit
    tasks = collab_store.list_board_tasks(archived=archived, **select_kwargs)
    # ONE batched query for every linked session, not N per-card round-trips.
    session_refs = [
        str(task.get("result_ref")) for task in tasks if _is_session_ref(task.get("result_ref"))
    ]
    session_map = sessions_dal.get_sessions_by_ids(session_refs) if session_refs else {}
    approval_map = _pending_approval_map(store)
    run_ids = [str(task["run_id"]) for task in tasks if task.get("run_id")]
    runs = _run_map(store, run_ids)
    step_counts = _step_count_map(store, run_ids)
    orchestration_refs = [
        str(task.get("result_ref"))
        for task in tasks
        if _is_orchestration_ref(task.get("result_ref"))
    ]
    orchestration_db_path = _sqlite_db_path(store)
    if orchestrations_dal is None:
        orchestrations_dal = _reconcile_orchestrations_dal(orchestration_db_path)
    check_stale_orchestrations = _claim_reconcile_stale_check(orchestration_db_path)
    stale_rows = (
        orchestrations_dal.mark_stale_failed(stale_minutes=_orchestration_stale_minutes())
        if check_stale_orchestrations
        else []
    )
    stale_ids = {str(row["id"]) for row in stale_rows}
    orchestration_map = (
        orchestrations_dal.get_by_ids(orchestration_refs) if orchestration_refs else {}
    )
    # ONE chat-companion map for the whole board: feeds chat_origin on every
    # card and the companion-only exclusion in the route (P0-9).
    chat_map = _chat_origin_map(store)
    # Company enrichment: shared with collab.list_board + org views
    from omniagentos.orgdims.enrich import enrich_company_from_projects

    conn = getattr(store, "_connection", None)
    if conn is not None:
        enrich_company_from_projects(tasks, conn)

    for task in tasks:
        fields = _reconcile_card_fields(
            task,
            runs=runs,
            session_map=session_map,
            orchestration_map=orchestration_map,
            stale_ids=stale_ids,
        )
        _apply_card_reconcile(
            store,
            collab_store,
            task,
            fields,
            stale_ids=stale_ids,
            db_path=orchestration_db_path,
        )

    if check_stale_orchestrations:
        resume_orphaned_orchestrations(store=store, collab_store=collab_store)

    # Metacog evaluate tick (Grok self-repair signal): best-effort, throttled via
    # the same stale-check claim window so board polls stay cheap.
    if archived == 0 and check_stale_orchestrations:
        try:
            _metacog_evaluate_active_tasks(tasks, step_counts=step_counts, run_map=runs)
        except Exception:  # noqa: BLE001
            LOG.debug("metacog evaluate tick skipped", exc_info=True)

    # ONE attempt-ledger read for every blocked card on the page (two queries per
    # 400-card chunk), never one per card. Only blocked cards are looked up: the
    # projection is defined only for them.
    blocked_reasons = _blocked_reason_map(
        store,
        [
            str(task.get("id") or "")
            for task in tasks
            if str(task.get("status") or "") == BoardTaskStatus.BLOCKED.value
        ],
    )

    # Enrich the already-reconciled in-memory rows (no second board query, no per-row
    # session lookup -- both mattered under load).
    return [
        _enrich_board_row(
            store,
            task,
            session_map,
            runs,
            step_counts,
            orchestration_map,
            approval_map,
            chat_map,
            blocked_reasons,
        )
        for task in tasks
    ]


# ---------------------------------------------------------------------------
# Chat v2 §3.10 — board ETA (honest estimates: null when nothing qualifies)
# ---------------------------------------------------------------------------


def _parse_iso_seconds(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds (None when unparseable)."""
    if not value or not isinstance(value, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _eta_from_run_steps(store: Store, run_id: str) -> dict[str, Any] | None:
    """Basis ``run_steps``: remaining x median of the last 5 completed steps."""
    try:
        rows = (
            cast(Any, store)
            ._connection.execute(
                "SELECT seq, status, started_at, finished_at FROM steps "
                "WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            )
            .fetchall()
        )
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    completed = [
        row
        for row in rows
        if row["status"] == "completed" and row["started_at"] and row["finished_at"]
    ]
    if len(completed) < 2:
        return None
    durations = []
    for row in completed[-5:]:
        start = _parse_iso_seconds(row["started_at"])
        finish = _parse_iso_seconds(row["finished_at"])
        if start is not None and finish is not None and finish > start:
            durations.append(finish - start)
    if not durations:
        return None
    remaining = len(rows) - len(completed)
    if remaining <= 0:
        return None
    import statistics

    estimate = max(30.0, remaining * statistics.median(durations))
    return {
        "estimate_seconds": int(estimate),
        "basis": "run_steps",
        "sample_size": len(durations),
        "confidence": "high" if len(completed) >= 5 else "medium",
    }


def _eta_from_session(sessions_dal: Any, session_id: str) -> dict[str, Any] | None:
    """Basis ``session_progress``: (elapsed / steps_done) x remaining."""
    try:
        session = sessions_dal.get_session(session_id)
    except Exception:  # noqa: BLE001
        return None
    if session is None:
        return None
    if str(session.get("state") or "") not in {
        "queued",
        "planning",
        "starting",
        "running",
        "awaiting_approval",
        "resuming",
    }:
        return None
    progress = _session_progress(session)
    done = int(progress.get("steps_done") or 0)
    total = int(progress.get("steps_total") or 0)
    if total <= 0 or done < 2:
        return None
    started = _parse_iso_seconds(session.get("created_at"))
    if started is None:
        return None
    elapsed = time.time() - started
    if elapsed <= 0:
        return None
    remaining = total - done
    estimate = max(30.0, (elapsed / done) * remaining)
    return {
        "estimate_seconds": int(estimate),
        "basis": "session_progress",
        "sample_size": done,
        "confidence": "low",
    }


def _eta_from_discipline_history(store: Store, task: dict[str, Any]) -> dict[str, Any] | None:
    """Basis ``discipline_history``: median wall time of same-discipline done cards."""
    discipline = str(task.get("discipline") or "").strip()
    if not discipline:
        return None
    cutoff = time.time() - 90 * 24 * 3600
    try:
        rows = (
            cast(Any, store)
            ._connection.execute(
                "SELECT created_at, updated_at FROM board_tasks "
                "WHERE discipline = ? AND status = 'done' AND id != ?",
                (discipline, str(task.get("id") or "")),
            )
            .fetchall()
        )
    except Exception:  # noqa: BLE001
        return None
    walls: list[float] = []
    for row in rows:
        start = _parse_iso_seconds(row["created_at"])
        finish = _parse_iso_seconds(row["updated_at"])
        if start is None or finish is None or finish <= start:
            continue
        if finish < cutoff:
            continue
        walls.append(finish - start)
    if len(walls) < 3:
        return None
    import statistics

    started = _parse_iso_seconds(task.get("created_at"))
    elapsed = (time.time() - started) if started is not None else 0.0
    estimate = max(0.0, statistics.median(walls) - elapsed)
    return {
        "estimate_seconds": int(estimate),
        "basis": "discipline_history",
        "sample_size": len(walls),
        "confidence": "low",
    }


def compute_board_eta(store: Store, sessions_dal: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The ``GET /api/board/{task_id}/eta`` payload (§3.10).

    First qualifying basis wins; when nothing qualifies the estimate is null
    and the UI renders "Estimating…" — never a fabricated number.
    """
    computed_at = utc_now_iso()
    run_id = str(task.get("run_id") or "")
    if run_id:
        estimate = _eta_from_run_steps(store, run_id)
        if estimate is not None:
            return {**estimate, "computed_at": computed_at}
    ref = str(task.get("result_ref") or "")
    if sessions_dal is not None and _is_session_ref(ref):
        estimate = _eta_from_session(sessions_dal, ref)
        if estimate is not None:
            return {**estimate, "computed_at": computed_at}
    estimate = _eta_from_discipline_history(store, task)
    if estimate is not None:
        return {**estimate, "computed_at": computed_at}
    return {
        "estimate_seconds": None,
        "basis": None,
        "sample_size": 0,
        "confidence": None,
        "computed_at": computed_at,
    }
