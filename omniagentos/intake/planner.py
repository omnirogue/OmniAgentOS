"""The PLAN step: turn a clarified goal into an executable project structure.

Pipeline (all Fable, via :mod:`omniagentos.intake.fable`):

* :func:`plan_goal` — the PLANNER. Fable at effort HIGH, escalated to MAX for
  high-complexity goals (effort chosen from a cheap complexity heuristic that Fable
  may confirm). Produces a :class:`ProjectPlan`: a single project (with a name), or a
  decomposition into a project + sub-projects + tasks over the frozen hierarchy.
* :func:`route_project` — the project ROUTER. Fable decides whether the plan is a
  NEW top-level project or should NEST under an EXISTING one (given the existing
  project list). Effort medium — a light judgement.
* :func:`provision_plan` — walks the routed plan and materialises it through the
  hierarchy DAL (:class:`HierarchyDAL`, owned/implemented by W3-hierarchy; stubbed in
  tests) + ``dispatch_spec``: create/resolve the project, create sub-projects and
  tasks under it, and dispatch each task to a runner. The quick one-shot dispatch
  path (``service.dispatch_spec``) is untouched and remains the default for a single
  bounded request.

Every Fable step degrades to a deterministic heuristic when the model is
unavailable, so planning never hard-fails offline.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import BaseModel, Field

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import Store, default_db_path
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.fable import Effort, run_fable_json
from omniagentos.policy import PolicyConfig

if TYPE_CHECKING:
    from omniagentos.intake.service import ExecuteMode

LOG = logging.getLogger(__name__)

Complexity = Literal["simple", "standard", "complex"]

# Effort the planner runs at for each complexity band. HIGH is the floor; a
# high-complexity goal earns MAX because decomposing it well is where the reasoning
# budget actually pays off.
_EFFORT_FOR_COMPLEXITY: dict[Complexity, Effort] = {
    "simple": "high",
    "standard": "high",
    "complex": "max",
}


# ---------------------------------------------------------------------------
# Plan shapes
# ---------------------------------------------------------------------------


class PlannedTask(BaseModel):
    """One executable task under a project or sub-project."""

    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    suggested_discipline: str | None = None
    suggested_priority: str = "normal"
    commits_expected: bool = False
    validation_command: str | None = None
    # Optional swarm-shaped ownership metadata. Ordinary hierarchy plans leave
    # this empty; when supplied, provision_plan validates it before side effects.
    owned_paths: list[str] = Field(default_factory=list)

    def to_spec(self) -> RefinedSpec:
        return RefinedSpec(
            title=self.title,
            description=self.description,
            acceptance_criteria=self.acceptance_criteria,
            suggested_discipline=self.suggested_discipline,
            suggested_priority=self.suggested_priority,
        ).normalized()


class PlannedSubProject(BaseModel):
    """A sub-project: a named child of the root project holding its own tasks."""

    name: str
    description: str = ""
    tasks: list[PlannedTask] = Field(default_factory=list)


class PlanningState(BaseModel):
    """How the plan was produced, including explicit heuristic degradation."""

    status: Literal["ready", "degraded"] = "ready"
    source: Literal["model", "heuristic"] = "model"
    reason: (
        Literal[
            "model_unavailable",
            "invalid_model_output",
            "empty_model_plan",
        ]
        | None
    ) = None


class ProjectPlan(BaseModel):
    """The planner's output: a root project, optionally decomposed.

    * ``tasks`` are executed directly under the root project.
    * ``sub_projects`` each nest under the root and carry their own tasks.

    ``is_decomposed`` is True when the plan uses sub-projects — the signal that this
    goal was big enough to warrant the full hierarchy rather than a flat project.
    """

    project_name: str
    description: str = ""
    complexity: Complexity = "standard"
    effort: Effort = "high"
    tasks: list[PlannedTask] = Field(default_factory=list)
    sub_projects: list[PlannedSubProject] = Field(default_factory=list)
    planning_state: PlanningState = Field(default_factory=PlanningState)

    def is_decomposed(self) -> bool:
        return bool(self.sub_projects)

    def normalized(self) -> ProjectPlan:
        name = self.project_name.strip() or "New project"
        subs = [
            PlannedSubProject(
                name=sp.name.strip() or "Sub-project",
                description=sp.description.strip(),
                tasks=[t for t in sp.tasks if t.title.strip()],
            )
            for sp in self.sub_projects
            if sp.name.strip() or sp.tasks
        ]
        return ProjectPlan(
            project_name=name[:2000],
            description=self.description.strip(),
            complexity=self.complexity,
            effort=self.effort,
            tasks=[t for t in self.tasks if t.title.strip()],
            sub_projects=subs,
            planning_state=self.planning_state,
        )


class RouteDecision(BaseModel):
    """The router's verdict: a NEW top-level project, or NEST under an existing one."""

    decision: Literal["new", "existing"]
    # The existing project to nest under when decision == "existing"; else None.
    parent_project_id: str | None = None
    # Registry roots captured at routing time. Quick intake uses root_dirs[0] as
    # the matched workspace; an empty list means there was no usable registry
    # workspace to apply.
    root_dirs: list[str] = Field(default_factory=list)
    # The name to create the (root) project with when decision == "new".
    project_name: str = ""
    reason: str = ""
    confidence: float | None = None


# ---------------------------------------------------------------------------
# LLM seams — injectable so tests mock Fable without touching the CLI.
# ---------------------------------------------------------------------------

# The planner seam takes (prompt, schema, effort) so a test can assert the effort
# the planner selected for a given complexity.
PlannerLLM = Callable[[str, dict[str, Any], str], dict[str, Any] | None]
# The router seam is a light (prompt, schema) call — always Fable effort medium.
RouterLLM = Callable[[str, dict[str, Any]], dict[str, Any] | None]


def default_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
    """Run the planner prompt through Fable at the chosen effort (high/max)."""
    return run_fable_json(prompt, schema, effort=effort, max_turns=3, wall_ms=300_000)


def default_router_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Run the project-router prompt through Fable at effort medium."""
    return run_fable_json(prompt, schema, effort="medium", max_turns=2, wall_ms=120_000)


# ---------------------------------------------------------------------------
# Complexity heuristic — cheap, deterministic, drives the effort floor.
# ---------------------------------------------------------------------------

# Words that hint a goal spans multiple deliverables / a whole system rather than a
# single bounded change.
_COMPLEX_SIGNALS = (
    "system",
    "platform",
    "pipeline",
    "end-to-end",
    "end to end",
    "multiple",
    "several",
    "suite",
    "dashboard",
    "microservice",
    "integrate",
    "migration",
    "architecture",
    "workflow",
    "orchestrat",
)


def estimate_complexity(goal: str) -> Complexity:
    """A cheap, deterministic complexity band for a goal.

    Combines length, the number of distinct deliverables implied by conjunctions /
    enumeration, and system-scale keywords. Fable may confirm/override this, but the
    heuristic guarantees a sane effort even when the model is unavailable.
    """
    text = goal.strip().lower()
    if not text:
        return "simple"
    words = len(text.split())
    # Conjunctions + list separators approximate "how many things is this?".
    clauses = len(re.findall(r"\band\b|\bthen\b|\bplus\b|[,;]|\bwith\b", text))
    bullets = len(re.findall(r"(?m)^\s*[-*\d]", goal))
    signals = sum(1 for kw in _COMPLEX_SIGNALS if kw in text)

    score = signals + clauses + bullets
    if words > 60:
        score += 2
    elif words > 30:
        score += 1

    if score >= 4 or (signals >= 1 and clauses >= 2):
        return "complex"
    if score >= 2 or words > 25:
        return "standard"
    return "simple"


def effort_for(complexity: Complexity) -> Effort:
    """The Fable effort the planner runs at for a complexity band (high floor, max at top)."""
    return _EFFORT_FOR_COMPLEXITY.get(complexity, "high")


# ---------------------------------------------------------------------------
# PLAN step
# ---------------------------------------------------------------------------

_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "suggested_discipline": {"type": "string"},
        "suggested_priority": {"type": "string"},
        "commits_expected": {"type": "boolean"},
        "validation_command": {"type": "string"},
        "owned_paths": {"type": "array", "items": {"type": "string"}},
    },
}

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_name"],
    "properties": {
        "project_name": {"type": "string"},
        "description": {"type": "string"},
        "complexity": {"type": "string", "enum": ["simple", "standard", "complex"]},
        "tasks": {"type": "array", "items": _TASK_SCHEMA},
        "sub_projects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tasks": {"type": "array", "items": _TASK_SCHEMA},
                },
            },
        },
    },
}


def _plan_prompt(
    goal: str,
    complexity: Complexity,
    project_context: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        "You are the PLANNER for OmniAgentOS, a multi-agent operations system.",
        "Given a clarified goal, produce a concrete, executable PLAN as a project.",
        "",
        f"CLARIFIED GOAL:\n{goal.strip() or '(empty)'}",
        "",
        f"A cheap heuristic rates this goal's complexity as: {complexity}.",
        "Confirm or correct it in the `complexity` field.",
        "",
        "Produce a project structure:",
        "- `project_name`: a short, specific name for the project.",
        "- `description`: one or two sentences on the outcome.",
        "- If the goal is a single bounded piece of work, put its work in `tasks`",
        "  (each with a title, description, 1-4 acceptance_criteria) and leave",
        "  `sub_projects` empty.",
        "- If the goal spans multiple distinct workstreams, DECOMPOSE it: create",
        "  `sub_projects`, each a named child with its own `tasks`. Only decompose",
        "  when the goal genuinely has separable parts — do not invent structure.",
        "",
        "Every task must be independently executable by one agent.",
        "For code changes set `commits_expected` true and provide a deterministic",
        "`validation_command` when the repository has one; otherwise omit them.",
    ]
    if project_context:
        rendered = str(project_context.get("contract") or "").strip()
        if rendered:
            lines.extend(("", "PROJECT-AWARE CONTRACT:", rendered))
    return "\n".join(lines)


def project_planning_context(
    store: Any,
    *,
    objective: str,
    project_id: str | None = None,
    working_dir: str | Path | None = None,
    audience: str = "",
    output_format: str = "",
    deliverable_spec: str = "",
) -> dict[str, str] | None:
    """Build gated project context for a planner call.

    Shadow mode resolves and reports the candidate while returning ``None`` so
    planner behavior stays unchanged. Enforce mode returns the explicit
    project-facts/objective/audience/format/deliverable-spec contract.
    """
    from omniagentos.brandpacks.pack import (
        project_contract_mode,
        render_project_contract,
        resolve_project_contract,
    )

    mode = project_contract_mode()
    if mode == "off":
        return None
    contract = resolve_project_contract(
        store,
        project_id=project_id,
        working_dir=working_dir,
    )
    if contract is None:
        return None
    if mode == "shadow":
        LOG.info("project planner shadow diff project=%s", contract.project.get("id"))
        return None
    return {
        "contract": render_project_contract(
            contract,
            objective=objective,
            audience=audience,
            output_format=output_format,
            deliverable_spec=deliverable_spec,
        )
    }


def _parse_task(raw: Mapping[str, Any]) -> PlannedTask | None:
    title = str(raw.get("title", "")).strip()
    if not title:
        return None
    crit_raw = raw.get("acceptance_criteria")
    criteria = (
        [str(c).strip() for c in crit_raw if str(c).strip()] if isinstance(crit_raw, list) else []
    )
    owned_paths_raw = raw.get("owned_paths")
    owned_paths = (
        [str(path).strip() for path in owned_paths_raw if str(path).strip()]
        if isinstance(owned_paths_raw, list)
        else []
    )
    return PlannedTask(
        title=title,
        description=str(raw.get("description", "")).strip(),
        acceptance_criteria=criteria[:6],
        suggested_discipline=(str(raw.get("suggested_discipline", "")).strip() or None),
        suggested_priority=str(raw.get("suggested_priority", "normal")).strip() or "normal",
        commits_expected=bool(raw.get("commits_expected", False)),
        validation_command=(str(raw.get("validation_command", "")).strip() or None),
        owned_paths=owned_paths,
    )


def _heuristic_plan(
    goal: str,
    complexity: Complexity,
    effort: Effort,
    *,
    reason: Literal[
        "model_unavailable",
        "invalid_model_output",
        "empty_model_plan",
    ],
) -> ProjectPlan:
    """A deterministic flat plan with a machine-readable degraded state."""
    first = next((ln.strip() for ln in goal.splitlines() if ln.strip()), goal.strip())
    name = (first or "New project")[:120]
    return ProjectPlan(
        project_name=name,
        description=goal.strip(),
        complexity=complexity,
        effort=effort,
        tasks=[
            PlannedTask(
                title=name,
                description=goal.strip(),
                acceptance_criteria=[f"Delivers on: {name}"],
            )
        ],
        planning_state=PlanningState(
            status="degraded",
            source="heuristic",
            reason=reason,
        ),
    ).normalized()


def plan_goal(
    goal: str,
    *,
    llm: PlannerLLM | None = None,
    effort: Effort | None = None,
    project_context: Mapping[str, Any] | None = None,
    project_store: Any | None = None,
    project_id: str | None = None,
    working_dir: str | Path | None = None,
    audience: str = "",
    output_format: str = "",
    deliverable_spec: str = "",
) -> ProjectPlan:
    """Produce a :class:`ProjectPlan` for ``goal`` using Fable at the right effort.

    Effort defaults to HIGH and escalates to MAX for a high-complexity goal (chosen
    from :func:`estimate_complexity`, overridable via ``effort``). Falls back to a
    deterministic single-project plan when Fable is unavailable.
    """
    complexity = estimate_complexity(goal)
    chosen: Effort = effort or effort_for(complexity)
    runner = llm or default_planner_llm
    effective_context = project_context
    if effective_context is None:
        owns_store = False
        context_store = project_store
        try:
            from omniagentos.brandpacks.pack import project_contract_mode

            if project_contract_mode() != "off":
                if context_store is None:
                    from omniagentos.db.store import SqliteStore

                    context_store = SqliteStore(default_db_path())
                    owns_store = True
                effective_context = project_planning_context(
                    context_store,
                    objective=goal,
                    project_id=project_id,
                    working_dir=working_dir or Path.cwd(),
                    audience=audience,
                    output_format=output_format,
                    deliverable_spec=deliverable_spec,
                )
        except Exception:  # noqa: BLE001 -- optional context must never block planning.
            LOG.debug("could not build automatic project planner context", exc_info=True)
        finally:
            if owns_store and context_store is not None:
                context_store.close()

    raw = runner(_plan_prompt(goal, complexity, effective_context), _PLAN_SCHEMA, chosen)
    if raw is None:
        return _heuristic_plan(
            goal,
            complexity,
            chosen,
            reason="model_unavailable",
        )
    if not isinstance(raw, Mapping) or not str(raw.get("project_name", "")).strip():
        return _heuristic_plan(
            goal,
            complexity,
            chosen,
            reason="invalid_model_output",
        )

    # Fable may sharpen the complexity call; keep the effort we actually ran at.
    model_complexity = str(raw.get("complexity", complexity)).strip().lower()
    if model_complexity not in ("simple", "standard", "complex"):
        model_complexity = complexity
    normalized_complexity = cast(Complexity, model_complexity)

    tasks = [t for t in (_parse_task(r) for r in _as_list(raw.get("tasks"))) if t]
    subs: list[PlannedSubProject] = []
    for sp_raw in _as_list(raw.get("sub_projects")):
        if not isinstance(sp_raw, Mapping):
            continue
        sp_name = str(sp_raw.get("name", "")).strip()
        if not sp_name:
            continue
        sp_tasks = [t for t in (_parse_task(r) for r in _as_list(sp_raw.get("tasks"))) if t]
        subs.append(
            PlannedSubProject(
                name=sp_name,
                description=str(sp_raw.get("description", "")).strip(),
                tasks=sp_tasks,
            )
        )

    plan = ProjectPlan(
        project_name=str(raw["project_name"]).strip(),
        description=str(raw.get("description", "")).strip(),
        complexity=normalized_complexity,
        effort=chosen,
        tasks=tasks,
        sub_projects=subs,
    ).normalized()

    # A decomposition with no tasks anywhere is useless — fall back to a flat plan so
    # provisioning always has something to dispatch.
    if not plan.tasks and all(not sp.tasks for sp in plan.sub_projects):
        return _heuristic_plan(
            goal,
            normalized_complexity,
            chosen,
            reason="empty_model_plan",
        )
    return plan


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# ROUTER step
# ---------------------------------------------------------------------------

_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {"type": "string", "enum": ["new", "existing"]},
        "parent_project_id": {"type": "string"},
        "project_name": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


def _route_prompt(plan: ProjectPlan, existing: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "You are the PROJECT ROUTER for OmniAgentOS. A planner has produced a plan for",
        "a new piece of work. Decide whether it should be a NEW top-level project or",
        "NEST as a sub-project under an EXISTING project.",
        "",
        f"PLANNED PROJECT: {plan.project_name}",
        f"DESCRIPTION: {plan.description or '(none)'}",
    ]
    if plan.sub_projects:
        lines.append("SUB-PROJECTS: " + ", ".join(sp.name for sp in plan.sub_projects))
    lines += ["", "EXISTING TOP-LEVEL PROJECTS:"]
    if existing:
        for p in existing:
            lines.append(f"- id={p.get('id')} name={p.get('name')!r}")
    else:
        lines.append("(none)")
    lines += [
        "",
        'Return decision="existing" with the parent_project_id ONLY when this work',
        'clearly belongs inside one of the listed projects. Otherwise decision="new"',
        "with a project_name. Give a one-line reason.",
    ]
    return "\n".join(lines)


def route_project(
    plan: ProjectPlan,
    existing_projects: Sequence[Mapping[str, Any]],
    *,
    llm: RouterLLM | None = None,
    require_root_dir: bool = False,
) -> RouteDecision:
    """Fable decides NEW project vs NEST under an existing one.

    With no existing projects there is nothing to nest under, so the answer is
    always NEW without consulting the model. A model verdict of "existing" is only
    honoured when its ``parent_project_id`` actually matches a listed project.

    ``require_root_dir`` is the registry-backed quick-intake path. It filters the
    registry snapshot before asking the router, so an ``existing`` verdict is only
    actionable when the matched project has a non-empty ``root_dirs[0]``. The
    accepted roots are copied into :class:`RouteDecision`; callers can therefore
    apply the match before dispatch performs workspace resolution. A registry with
    no usable rooted project is a genuine miss and returns ``new`` without calling
    the model.
    """
    registry: dict[str, tuple[Mapping[str, Any], list[str]]] = {}
    for project in existing_projects:
        project_id = str(project.get("id") or "").strip()
        if not project_id:
            continue
        raw_roots = project.get("root_dirs")
        roots = (
            [str(root).strip() for root in raw_roots if str(root).strip()]
            if isinstance(raw_roots, (list, tuple))
            else []
        )
        if require_root_dir and not roots:
            continue
        registry[project_id] = (project, roots)

    if not registry:
        return RouteDecision(
            decision="new",
            project_name=plan.project_name,
            reason=(
                "no existing project has a usable root directory"
                if require_root_dir and existing_projects
                else "no existing projects to nest under"
            ),
        )

    candidates = [project for project, _roots in registry.values()]
    runner = llm or default_router_llm
    raw = runner(_route_prompt(plan, candidates), _ROUTE_SCHEMA)
    if raw is None:
        return RouteDecision(
            decision="new", project_name=plan.project_name, reason="router unavailable"
        )

    decision = str(raw.get("decision", "")).strip().lower()
    parent = str(raw.get("parent_project_id", "")).strip() or None
    confidence = raw.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (ValueError, TypeError):
            confidence = None

    if decision == "existing" and parent in registry:
        _matched, roots = registry[parent]
        return RouteDecision(
            decision="existing",
            parent_project_id=parent,
            root_dirs=roots,
            project_name=str(raw.get("project_name", "")).strip() or plan.project_name,
            reason=str(raw.get("reason", "")).strip(),
            confidence=confidence,
        )
    # "existing" without a real parent id is not actionable — treat as new.
    return RouteDecision(
        decision="new",
        project_name=str(raw.get("project_name", "")).strip() or plan.project_name,
        reason=str(raw.get("reason", "")).strip() or "no matching existing project",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Hierarchy DAL seam + provisioning
# ---------------------------------------------------------------------------


class HierarchyDAL(Protocol):
    """The slice of the W3-hierarchy data layer the planner provisions through.

    W3-hierarchy owns the concrete implementation (migration 031:
    ``projects.parent_project_id``). The planner codes against this frozen interface
    and stubs it in tests. ``create_project`` returns the new project id; a set
    ``parent_project_id`` nests it under that parent.
    """

    def create_project(
        self, name: str, *, parent_project_id: str | None = None, description: str = ""
    ) -> str: ...

    def list_projects(self) -> Sequence[Mapping[str, Any]]: ...


class ProjectStoreHierarchyDAL:
    """Default :class:`HierarchyDAL` over the existing ``ProjectStore``.

    Passes ``parent_project_id`` through the project payload — valid once migration
    031 (owned by W3-hierarchy) lands. Tests stub :class:`HierarchyDAL` directly, so
    this concrete path does not gate the planner's own suite.
    """

    def __init__(self, store: Store) -> None:
        from omniagentos.projects import ProjectStore

        self._projects = ProjectStore(store)  # type: ignore[arg-type]

    def create_project(
        self, name: str, *, parent_project_id: str | None = None, description: str = ""
    ) -> str:
        data: dict[str, Any] = {"name": name}
        if parent_project_id:
            data["parent_project_id"] = parent_project_id
            # Registry routing (OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE=enforce): a
            # project nested under a matched parent REUSES the parent's own
            # root_dirs rather than getting a fresh scratch workspace, so work
            # dispatched under it lands in the SAME directory the match resolved
            # to (root_dirs[0]) instead of an unrelated empty dir.
            parent = self._projects.get_project(parent_project_id)
            parent_roots = list((parent or {}).get("root_dirs") or [])
            if parent_roots:
                data["root_dirs"] = parent_roots
        if "root_dirs" not in data:
            from omniagentos.contracts import new_id
            from omniagentos.projects.activity import project_log_dir

            pid = new_id("proj")
            workspace_dir = project_log_dir(pid).parent / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            data["id"] = pid
            data["root_dirs"] = [str(workspace_dir)]
        project = self._projects.create_project(data)
        return str(project["id"])

    def list_projects(self) -> Sequence[Mapping[str, Any]]:
        return self._projects.list_projects()


@dataclass
class ProvisionResult:
    """What provisioning materialised for a plan."""

    root_project_id: str
    route: RouteDecision
    sub_project_ids: dict[str, str] = field(default_factory=dict)
    dispatched: list[dict[str, Any]] = field(default_factory=list)

    @property
    def task_count(self) -> int:
        return len(self.dispatched)


def provision_plan(
    store: Store,
    collab_store: CollabStore,
    policy_cfg: PolicyConfig,
    plan: ProjectPlan,
    route: RouteDecision,
    hierarchy: HierarchyDAL,
    *,
    harness: str | None = None,
    execute: ExecuteMode = "readonly",
    speed: str | None = None,
    priority: str | None = None,
    pins: dict[str, Any] | None = None,
) -> ProvisionResult:
    """Materialise a routed plan: create/nest the project(s), then dispatch its tasks.

    * When ``route.decision == "existing"`` the plan's root project is created as a
      sub-project of ``route.parent_project_id`` (the frozen hierarchy). The
      concrete :class:`ProjectStoreHierarchyDAL` REUSES that matched parent's own
      ``root_dirs`` for the new nested project (Phase 1, A1) instead of a fresh
      scratch workspace, so tasks dispatched under it land in the SAME directory
      the registry match resolved to.
    * Each ``plan.sub_projects`` entry becomes a project nested under the root; its
      tasks are dispatched with that sub-project as their ``project_id``.
    * Top-level ``plan.tasks`` dispatch under the root project.

    Dispatch reuses ``service.dispatch_spec`` verbatim, so each task still lands as a
    live board card + control-plane task + queued run.

    ``speed``/``priority``/``pins`` (D10 Mode dial) are threaded VERBATIM into
    every per-task ``dispatch_spec`` call — the speed→priority/pins mapping is
    the API edge's job (``routes/intake.plan_confirm``), never re-derived here.
    All three default to None so existing callers keep today's dispatch
    byte-identically. ``execute="single"`` dispatches each task as orchestrate
    with the auto solo-vs-swarm upgrade HARD-suppressed (D10 Single Task).
    """
    # Imported lazily: service.py pulls in the api package at module load, so a
    # top-level import here would drag the whole HTTP layer into every planner
    # import and tighten an already order-sensitive intake<->api import cycle.
    from omniagentos.intake.service import dispatch_spec

    plan = plan.normalized()
    # Fail-closed before any project/card side effect (mutation: refusal-creates-run).
    # Degraded planner output is a pre-run refusal, not a provisionable plan.
    if plan.planning_state.status == "degraded":
        from omniagentos.swarm.contracts import PlanDisposition, PlanIssue, SwarmPlanDecision
        from omniagentos.swarm.plan_safety import PlanSafetyError

        reason = plan.planning_state.reason or "planner_unavailable"
        disposition: PlanDisposition = (
            "planner_unavailable"
            if reason in {"model_unavailable", "empty_model_plan"}
            else "invalid_plan"
        )
        raise PlanSafetyError(
            SwarmPlanDecision(
                disposition=disposition,
                plans=[],
                issues=[
                    PlanIssue(
                        code=disposition,
                        message=f"intake planner refused to provision a {reason} plan",
                    )
                ],
                reason=f"intake plan not ready for provision: {reason}",
            )
        )
    if not plan.tasks and not any(sp.tasks for sp in plan.sub_projects):
        from omniagentos.swarm.contracts import PlanDisposition, PlanIssue, SwarmPlanDecision
        from omniagentos.swarm.plan_safety import PlanSafetyError

        raise PlanSafetyError(
            SwarmPlanDecision(
                disposition="invalid_plan",
                plans=[],
                issues=[PlanIssue(code="empty_plan", message="plan contains no tasks")],
                reason="intake plan contains no tasks",
            )
        )
    # When ownership is declared, revalidate through the central swarm safety
    # authority before any hierarchy side effect. Ordinary intake plans leave
    # owned_paths empty and are not forced through empty-ownership refusal.
    path_bearing: list[Any] = []
    for idx, task in enumerate(plan.tasks):
        if task.owned_paths:
            path_bearing.append((f"task-{idx + 1}", task))
    for sp_idx, sub in enumerate(plan.sub_projects):
        for t_idx, task in enumerate(sub.tasks):
            if task.owned_paths:
                path_bearing.append((f"sub-{sp_idx + 1}-task-{t_idx + 1}", task))
    if path_bearing:
        from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
        from omniagentos.swarm.plan_safety import PlanSafetyError, evaluate_plan_safety

        swarm_tasks = [
            SwarmTaskSpec(
                id=task_id,
                title=task.title,
                description=task.description or task.title,
                owned_paths=list(task.owned_paths),
                acceptance="; ".join(task.acceptance_criteria)
                if task.acceptance_criteria
                else "done",
                verify_command=(task.validation_command or "").strip(),
            )
            for task_id, task in path_bearing
        ]
        decision = evaluate_plan_safety(
            SwarmPlan(
                goal=plan.project_name,
                tasks=swarm_tasks,
                mode="solo",
                target_n=1,
            )
        )
        if not decision.is_ready:
            raise PlanSafetyError(decision)

    root_id = hierarchy.create_project(
        plan.project_name,
        parent_project_id=route.parent_project_id if route.decision == "existing" else None,
        description=plan.description,
    )
    result = ProvisionResult(root_project_id=root_id, route=route)

    for task in plan.tasks:
        result.dispatched.append(
            dispatch_spec(
                store,
                collab_store,
                policy_cfg,
                task.to_spec(),
                harness=harness,
                project_id=root_id,
                execute=execute,
                speed=speed,
                priority=priority,
                pins=pins,
            )
        )

    for sub in plan.sub_projects:
        sub_id = hierarchy.create_project(
            sub.name, parent_project_id=root_id, description=sub.description
        )
        result.sub_project_ids[sub.name] = sub_id
        for task in sub.tasks:
            result.dispatched.append(
                dispatch_spec(
                    store,
                    collab_store,
                    policy_cfg,
                    task.to_spec(),
                    harness=harness,
                    project_id=sub_id,
                    execute=execute,
                    speed=speed,
                    priority=priority,
                    pins=pins,
                )
            )

    return result


def plan_and_provision(
    store: Store,
    collab_store: CollabStore,
    policy_cfg: PolicyConfig,
    goal: str,
    hierarchy: HierarchyDAL,
    *,
    planner_llm: PlannerLLM | None = None,
    router_llm: RouterLLM | None = None,
    harness: str | None = None,
    execute: ExecuteMode = "readonly",
    project_id: str | None = None,
    working_dir: str | Path | None = None,
    audience: str = "",
    output_format: str = "",
    deliverable_spec: str = "",
) -> ProvisionResult:
    """End-to-end: plan the goal, route it, then create + dispatch it.

    The full path behind a confirmed intake goal (clarify already ran upstream):
    PLAN (Fable high/max) -> ROUTE (Fable) -> create project(+sub-projects/tasks via
    ``hierarchy``) -> dispatch tasks. The one-shot ``service.dispatch_spec`` remains
    available for a single bounded request that needs no planning.
    """
    plan = plan_goal(
        goal,
        llm=planner_llm,
        project_store=store,
        project_id=project_id,
        working_dir=working_dir,
        audience=audience,
        output_format=output_format,
        deliverable_spec=deliverable_spec,
    )
    route = route_project(plan, hierarchy.list_projects(), llm=router_llm)
    return provision_plan(
        store,
        collab_store,
        policy_cfg,
        plan,
        route,
        hierarchy,
        harness=harness,
        execute=execute,
    )


__all__ = [
    "Complexity",
    "HierarchyDAL",
    "PlannedSubProject",
    "PlannedTask",
    "PlanningState",
    "ProjectPlan",
    "ProjectStoreHierarchyDAL",
    "ProvisionResult",
    "RouteDecision",
    "default_planner_llm",
    "default_router_llm",
    "effort_for",
    "estimate_complexity",
    "plan_and_provision",
    "plan_goal",
    "project_planning_context",
    "provision_plan",
    "route_project",
]
