"""Project provisioning: scope a goal to exactly the folders + APIs it needs.

Three moving parts, all pure of HTTP:

* ``determine_scope`` — runs a cheap LLM (the Claude CLI adapter by default, no
  hardcoded key) to decide, from a refined goal/spec, which existing project a
  goal references (if any) and which connector APIs it likely needs, drawn from
  the ``configs/connectors.yaml`` catalogue. Degrades to a deny-by-default
  heuristic (no connectors, fresh workspace) when the LLM is unavailable.
* ``provision_project`` — turns that plan into a scoped project row: an existing
  project's dir when the goal references one, else a freshly-created workspace
  under ``~/OmniAgentOS/var/projects/<project_id>/`` (never an unscoped/home dir),
  with the determined connectors as its allowed scopes. Reuses the W2 project +
  permission-grant tables.
* ``request_scope`` — the mid-task widen-scope flow: a NON-hard-stop scope (a
  normal dir under the user's home, a read/auto-tier connector) is AUTO-GRANTED
  and recorded; a HARD-STOP scope (a money-move/transfer connector, a
  delete-capable class, or a dir outside the user's home) is ESCALATED — recorded
  pending, never auto-granted. The hard-stop decision defers to the AC-policy
  ``is_hard_stop(action_class)`` contract, with a conservative fallback.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omniagentos.connectors import AUTO_CLASSES, ConnectorError, load_registry
from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    Store,
    new_id,
)
from omniagentos.projects import ProjectStore
from omniagentos.provision.contracts import (
    DECISION_AUTO_GRANTED,
    DECISION_ESCALATED,
    SCOPE_CONNECTOR,
    SCOPE_DIR,
    STATUS_GRANTED,
    STATUS_PENDING,
    ProvisionPlan,
    ProvisionResult,
    ScopeRequestResult,
)
from omniagentos.provision.store import ProvisionError, ProvisionStore

if TYPE_CHECKING:
    # Only for annotations -- importing intake at runtime would cycle back through
    # intake.service (which imports this module) during package init.
    from omniagentos.intake.contracts import RefinedSpec

LOG = logging.getLogger(__name__)

# A provisioning LLM turns a prompt + JSON schema into the model's parsed JSON
# object, or None when the model was unavailable / produced nothing usable.
ProvisionLLM = Callable[[str, dict[str, Any]], dict[str, Any] | None]

# A hard-stop predicate over an action class. Injected in tests; in production it
# defers to the AC-policy ``is_hard_stop`` contract (see ``is_hard_stop_action``).
HardStopFn = Callable[[Any], bool]

_PROVISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "existing_project": {"type": ["string", "null"]},
        "connectors": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}


class ProvisionServiceError(ValueError):
    """Raised when provisioning cannot proceed (e.g. an unknown target project)."""


def _projects_base() -> Path:
    """Parent dir for freshly-provisioned per-project workspaces.

    Anchored to ``OMNIAGENTOS_VAR_DIR`` when set (the same knob the adapters and
    intake use for their var root), else ``~/OmniAgentOS/var`` — never the process
    cwd. Each project gets its own ``<base>/projects/<project_id>/`` subtree.
    """
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    root = Path(base) if base else Path.home() / "OmniAgentOS" / "var"
    return root / "projects"


def _catalog_lines() -> list[str]:
    """One catalogue line per connector capability the model may choose from."""
    registry = load_registry()
    lines: list[str] = []
    for cap_id, cap in sorted(registry.capabilities.items()):
        lines.append(f"- {cap_id} [{cap.action_class.value}] {cap.label}")
    return lines


def _provision_prompt(spec: RefinedSpec, projects: list[str]) -> str:
    lines = [
        "You are the PROVISIONING agent for OmniAgentOS. Given a refined task spec,",
        "decide the MINIMUM scope the work needs: which existing project it belongs",
        "to (if any) and which connector APIs it will likely call.",
        "",
        f"TASK TITLE: {spec.title.strip()}",
        f"TASK DETAIL: {spec.description.strip() or '(none)'}",
    ]
    if spec.acceptance_criteria:
        lines.append("ACCEPTANCE CRITERIA:")
        lines.extend(f"- {c}" for c in spec.acceptance_criteria)
    if projects:
        lines.append("")
        lines.append("EXISTING PROJECTS (attach only if the goal clearly belongs to one):")
        lines.extend(f"- {name}" for name in projects)
    lines += [
        "",
        "AVAILABLE CONNECTOR CAPABILITIES (id [action_class] label):",
        *_catalog_lines(),
        "",
        "Return JSON with:",
        '  "existing_project": the exact name of an existing project this belongs to,',
        "      or null to create a fresh scoped workspace.",
        '  "connectors": a list of capability ids (e.g. "stripe_acmeuni.read") this task',
        "      will actually need. Grant the FEWEST you can defend; prefer read",
        "      capabilities. Omit anything the task does not clearly need.",
        '  "reason": one sentence on why this scope.',
    ]
    return "\n".join(lines)


def default_provision_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Run the provisioning prompt through the Claude CLI adapter with a cheap model.

    Never raises: any resolution/transport fault returns None so ``determine_scope``
    falls back to its deny-by-default heuristic. No API key is hardcoded — the
    subscription CLI carries its own auth (mirrors intake.default_clarify_llm).
    """
    from omniagentos.adapters.registry import resolve_adapter

    try:
        adapter = resolve_adapter(HarnessType.CLI_CLAUDE)
        result: AgentResult = adapter.run(
            AgentInput(
                run_id=new_id("run"),
                task_id=new_id("tsk"),
                prompt=prompt,
                model=os.environ.get("OMNIAGENTOS_PROVISION_MODEL", "haiku"),
                output_schema=schema,
                budget=BudgetSpec(max_turns=2, wall_ms_max=120_000),
            )
        )
    except Exception:  # noqa: BLE001 -- an unavailable CLI must degrade, not crash provisioning.
        LOG.warning("provision LLM unavailable; falling back to heuristic", exc_info=True)
        return None
    if result.status != ResultStatus.OK or result.output_json is None:
        LOG.info("provision LLM returned no structured output (%s)", result.status)
        return None
    return result.output_json


def _resolve_connectors(raw: list[str]) -> list[str]:
    """Keep only real, grantable capability ids; expand a bare connector to its read.

    A model may return a bare connector id ('stripe_acmeuni') or a capability id
    ('stripe_acmeuni.read'). Bare ids resolve to that connector's ``.read`` capability
    when it has one (the safe default); anything not in the registry is dropped so
    a hallucinated API can never widen scope.
    """
    registry = load_registry()
    caps = registry.capabilities
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        if name in caps:
            if name not in out:
                out.append(name)
            continue
        if "." not in name:
            read_id = f"{name}.read"
            if read_id in caps and read_id not in out:
                out.append(read_id)
    return out


def determine_scope(
    spec: RefinedSpec,
    *,
    existing_projects: list[str] | None = None,
    llm: ProvisionLLM | None = None,
) -> ProvisionPlan:
    """Decide the connectors + existing-project attach for a goal (LLM, else heuristic)."""
    runner = llm or default_provision_llm
    try:
        raw = runner(_provision_prompt(spec, existing_projects or []), _PROVISION_SCHEMA)
    except Exception:  # noqa: BLE001 -- a faulty seam degrades to the heuristic.
        LOG.warning("provision scope determination failed; using heuristic", exc_info=True)
        raw = None
    if raw is None:
        # Deny-by-default: no connectors, fresh workspace. The agent can request
        # more mid-task, which the auto-grant/escalate flow gates.
        return ProvisionPlan(
            existing_project=None, connectors=[], reason="heuristic: minimal scope"
        )
    plan = ProvisionPlan.model_validate(raw)
    plan.connectors = _resolve_connectors(plan.connectors)
    existing = (plan.existing_project or "").strip()
    plan.existing_project = existing or None
    return plan


def _match_project(projects: list[dict[str, Any]], name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    needle = name.strip().lower()
    for project in projects:
        if str(project.get("name", "")).strip().lower() == needle:
            return project
        if str(project.get("id", "")).strip().lower() == needle:
            return project
    return None


def _first_root(project: dict[str, Any]) -> str | None:
    roots = project.get("root_dirs") or []
    if roots and str(roots[0]).strip():
        return str(roots[0])
    return None


def provision_project(
    store: Store,
    spec: RefinedSpec,
    *,
    project_id: str | None = None,
    llm: ProvisionLLM | None = None,
    plan: ProvisionPlan | None = None,
) -> ProvisionResult:
    """Provision a scoped project for ``spec`` and return its dirs + connectors.

    Resolution order for the working dir / project:

    * an explicit ``project_id`` that exists -> attach to it (widen its connectors);
    * else an ``existing_project`` the plan names and that exists -> attach to it;
    * else CREATE a fresh scoped workspace under
      ``~/OmniAgentOS/var/projects/<project_id>/`` with the determined connectors.

    The connectors are recorded as the project's allowed capability scopes
    (``projects.allowed_tools_json``); the primary root dir is the run's working
    dir. Nothing here executes the task or relaxes any policy floor.
    """
    projects = ProjectStore(store)  # type: ignore[arg-type]
    prov = ProvisionStore(store)  # type: ignore[arg-type]

    existing_rows = projects.list_projects()
    if plan is None:
        plan = determine_scope(
            spec,
            existing_projects=[str(p.get("name", "")) for p in existing_rows],
            llm=llm,
        )
    connectors = plan.connectors

    target = None
    if project_id:
        target = projects.get_project(project_id)
    if target is None:
        target = _match_project(existing_rows, plan.existing_project)

    if target is not None:
        pid = str(target["id"])
        for cap_id in connectors:
            prov.grant_connector(pid, cap_id)
        refreshed = projects.get_project(pid) or target
        root = _first_root(refreshed)
        if not root:
            # An existing project with no root dir: give it a scoped workspace so a
            # dispatched run is never left to fall back to an unscoped cwd.
            root = str(_projects_base() / pid)
            Path(root).mkdir(parents=True, exist_ok=True)
            prov.grant_dir(pid, root)
            refreshed = projects.get_project(pid) or refreshed
        return ProvisionResult(
            project_id=pid,
            name=str(refreshed.get("name", "")),
            root_dirs=[str(d) for d in (refreshed.get("root_dirs") or [])],
            working_dir=root,
            allowed_connectors=[
                str(t) for t in (refreshed.get("allowed_tools") or []) if "." in str(t)
            ],
            created=False,
            reason=plan.reason,
        )

    # Create a fresh scoped workspace. Generate the id first so the dir and the
    # row agree, and the dir lives under the project's own subtree.
    pid = new_id("proj")
    workspace = _projects_base() / pid
    workspace.mkdir(parents=True, exist_ok=True)
    root = str(workspace)
    name = spec.title.strip()[:120] or f"provisioned {pid}"
    created = projects.create_project(
        {
            "id": pid,
            "name": _unique_name(name, existing_rows),
            "root_dirs": [root],
            "allowed_dirs": [root],
            "allowed_tools": connectors,
        }
    )
    return ProvisionResult(
        project_id=pid,
        name=str(created["name"]),
        root_dirs=[str(d) for d in created.get("root_dirs", [])],
        working_dir=root,
        allowed_connectors=[str(t) for t in created.get("allowed_tools", []) if "." in str(t)],
        created=True,
        reason=plan.reason,
    )


def _unique_name(name: str, existing: list[dict[str, Any]]) -> str:
    """Disambiguate a project name against existing rows (names are UNIQUE)."""
    taken = {str(p.get("name", "")).strip().lower() for p in existing}
    if name.strip().lower() not in taken:
        return name
    for suffix in range(2, 1000):
        candidate = f"{name} ({suffix})"
        if candidate.strip().lower() not in taken:
            return candidate
    return f"{name} {new_id('proj')}"


def is_hard_stop_action(action_class: ActionClass | str) -> bool:
    """Is this action class a hard stop (never auto-grantable)?

    Defers to the AC-policy ``is_hard_stop(action_class)`` contract when present.
    Fallback (before that lands / if unavailable): any class OUTSIDE the connector
    registry's AUTO tier (read_only / sandboxed_creation / internal_reversible) is
    a hard stop — i.e. external_reversible and consequential must escalate.
    """
    from omniagentos import policy as policy_mod

    fn = getattr(policy_mod, "is_hard_stop", None)
    if fn is not None:
        return bool(fn(action_class))
    try:
        normalized = ActionClass(action_class)
    except (TypeError, ValueError):
        return True  # unknown class: fail closed to escalation.
    return normalized not in AUTO_CLASSES


def _normalize_dir(path: str) -> str:
    """Absolutize + reject unsafe dir requests (NUL, '..', non-absolute)."""
    value = str(path or "").strip()
    if not value:
        raise ProvisionServiceError("dir path is required")
    if "\x00" in value:
        raise ProvisionServiceError("path must not contain NUL bytes")
    if ".." in value.replace("\\", "/").split("/"):
        raise ProvisionServiceError("path must not contain '..' segments")
    if not value.startswith("/"):
        raise ProvisionServiceError("dir must be an absolute path")
    return os.path.normpath(value)


def request_scope(
    store: Store,
    project_id: str,
    *,
    connector: str | None = None,
    dir: str | None = None,
    run_id: str | None = None,
    allow_roots: list[str] | None = None,
    hard_stop: HardStopFn | None = None,
) -> ScopeRequestResult:
    """Request an additional dir or connector mid-task; auto-grant or escalate.

    Exactly one of ``connector`` / ``dir`` must be given.

    * connector: resolve its capability action class; if ``is_hard_stop`` says it
      is a hard stop (money-move/transfer, delete-capable) -> ESCALATE (pending,
      not granted); else AUTO-GRANT (append to the project's allowed connectors).
    * dir: a path INSIDE a user-approved allow-root (see
      :func:`omniagentos.policy.dir_grants.allowed_grant_roots`) is AUTO-GRANTED; a
      path that IS or CONTAINS a secret-registry dir is HARD-REJECTED (never
      grantable, not escalatable — ``~/.ssh`` and friends); any other dir (outside
      the approved roots but not a secret) is a hard stop -> ESCALATE (pending).

    ``allow_roots`` overrides the approved-root set in production (tests inject
    it); simulation always uses its canonical campaign root. Every normal
    outcome is recorded in ``project_scope_requests``. Raises
    :class:`ProvisionServiceError` on bad input, :class:`ProvisionError` for an
    unknown project.
    """
    if bool(connector) == bool(dir):
        raise ProvisionServiceError("request exactly one of 'connector' or 'dir'")

    projects = ProjectStore(store)  # type: ignore[arg-type]
    if projects.get_project(project_id) is None:
        raise ProvisionError(f"unknown project {project_id!r}")
    prov = ProvisionStore(store)  # type: ignore[arg-type]
    hard_stop_fn = hard_stop or is_hard_stop_action

    from omniagentos.simgate import SimGateError, resolve_sim_context

    try:
        sim_ctx = resolve_sim_context()
    except SimGateError as exc:
        LOG.warning("provision/request_scope: sim gate refused: %s", exc)
        raise

    if connector:
        return _request_connector(prov, project_id, connector, run_id, hard_stop_fn)
    from omniagentos.policy.dir_grants import allowed_grant_roots

    roots = (
        allowed_grant_roots(sim_ctx=sim_ctx)
        if sim_ctx.sim_mode or allow_roots is None
        else allow_roots
    )
    return _request_dir(prov, project_id, str(dir), run_id, roots)


def _request_connector(
    prov: ProvisionStore,
    project_id: str,
    capability_id: str,
    run_id: str | None,
    hard_stop_fn: HardStopFn,
) -> ScopeRequestResult:
    registry = load_registry()
    try:
        cap = registry.capability(capability_id)
    except ConnectorError as exc:
        raise ProvisionServiceError(str(exc)) from exc
    action_class = cap.action_class.value

    if hard_stop_fn(cap.action_class):
        reason = (
            f"{capability_id} is a hard-stop scope ({action_class}); "
            "requires human approval, not auto-granted"
        )
        row = prov.record_request(
            project_id,
            scope_kind=SCOPE_CONNECTOR,
            scope_value=capability_id,
            action_class=action_class,
            decision=DECISION_ESCALATED,
            status=STATUS_PENDING,
            reason=reason,
            run_id=run_id,
        )
        return ScopeRequestResult(
            request_id=row["id"],
            project_id=project_id,
            scope_kind=SCOPE_CONNECTOR,
            scope_value=capability_id,
            action_class=action_class,
            decision=DECISION_ESCALATED,
            granted=False,
            status=STATUS_PENDING,
            reason=reason,
        )

    prov.grant_connector(project_id, capability_id)
    reason = f"{capability_id} is an auto-tier scope ({action_class}); granted"
    row = prov.record_request(
        project_id,
        scope_kind=SCOPE_CONNECTOR,
        scope_value=capability_id,
        action_class=action_class,
        decision=DECISION_AUTO_GRANTED,
        status=STATUS_GRANTED,
        reason=reason,
        run_id=run_id,
    )
    return ScopeRequestResult(
        request_id=row["id"],
        project_id=project_id,
        scope_kind=SCOPE_CONNECTOR,
        scope_value=capability_id,
        action_class=action_class,
        decision=DECISION_AUTO_GRANTED,
        granted=True,
        status=STATUS_GRANTED,
        reason=reason,
    )


def _request_dir(
    prov: ProvisionStore,
    project_id: str,
    dir_path: str,
    run_id: str | None,
    allow_roots: list[str] | None,
) -> ScopeRequestResult:
    from omniagentos.policy.dir_grants import references_secret_dir, within_allowed_roots

    normalized = _normalize_dir(dir_path)

    # AC-policy fix7 HARD-REJECT: a path that IS or CONTAINS a secret-registry dir
    # (~/.ssh, ~/.aws, ~/.config/omni, repo var/secrets, ...) is never grantable and
    # never escalatable. The old ``_dir_within_home`` rule auto-granted ~/.ssh — this
    # refuses it outright rather than routing it to a human approval queue.
    if references_secret_dir(normalized):
        raise ProvisionServiceError(
            f"{normalized} resolves to or contains a secret/credential directory; "
            "it can never be granted"
        )

    if not within_allowed_roots(normalized, allow_roots=allow_roots):
        reason = (
            f"{normalized} is outside the approved grant roots; "
            "hard-stop scope, requires human approval"
        )
        row = prov.record_request(
            project_id,
            scope_kind=SCOPE_DIR,
            scope_value=normalized,
            decision=DECISION_ESCALATED,
            status=STATUS_PENDING,
            reason=reason,
            run_id=run_id,
        )
        return ScopeRequestResult(
            request_id=row["id"],
            project_id=project_id,
            scope_kind=SCOPE_DIR,
            scope_value=normalized,
            decision=DECISION_ESCALATED,
            granted=False,
            status=STATUS_PENDING,
            reason=reason,
        )

    prov.grant_dir(project_id, normalized)
    reason = f"{normalized} is inside an approved grant root; auto-granted"
    row = prov.record_request(
        project_id,
        scope_kind=SCOPE_DIR,
        scope_value=normalized,
        decision=DECISION_AUTO_GRANTED,
        status=STATUS_GRANTED,
        reason=reason,
        run_id=run_id,
    )
    return ScopeRequestResult(
        request_id=row["id"],
        project_id=project_id,
        scope_kind=SCOPE_DIR,
        scope_value=normalized,
        decision=DECISION_AUTO_GRANTED,
        granted=True,
        status=STATUS_GRANTED,
        reason=reason,
    )


def build_scope_view(
    store: Store,
    project_id: str,
    *,
    hard_stop: HardStopFn | None = None,
) -> dict[str, Any] | None:
    """Assemble a project's provisioned scope for the UI: dirs + APIs + grants.

    Returns ``None`` when the project is unknown. Connector scopes are enriched
    from the registry (label, group, action_class, hard_stop) so the UI can render
    exactly what the project is allowed to touch and what would escalate.
    """
    projects = ProjectStore(store)  # type: ignore[arg-type]
    project = projects.get_project(project_id)
    if project is None:
        return None
    prov = ProvisionStore(store)  # type: ignore[arg-type]
    registry = load_registry()
    hard_stop_fn = hard_stop or is_hard_stop_action

    tools = [str(t) for t in (project.get("allowed_tools") or [])]
    connectors: list[dict[str, Any]] = []
    workspace_tools: list[str] = []
    for tool in tools:
        if "." not in tool:
            workspace_tools.append(tool)
            continue
        cap = registry.capabilities.get(tool)
        if cap is None:
            connectors.append({"id": tool, "known": False})
            continue
        connectors.append(
            {
                "id": tool,
                "known": True,
                "label": cap.label,
                "group": cap.group,
                "action_class": cap.action_class.value,
                "is_auto": cap.is_auto,
                "hard_stop": hard_stop_fn(cap.action_class),
            }
        )

    return {
        "project_id": project_id,
        "name": project.get("name"),
        "root_dirs": [str(d) for d in (project.get("root_dirs") or [])],
        "allowed_dirs": [str(d) for d in (project.get("allowed_dirs") or [])],
        "connectors": connectors,
        "workspace_tools": workspace_tools,
        "grants": project.get("grants", []),
        "scope_requests": prov.list_requests(project_id),
    }


__all__ = [
    "HardStopFn",
    "ProvisionLLM",
    "ProvisionServiceError",
    "build_scope_view",
    "default_provision_llm",
    "determine_scope",
    "is_hard_stop_action",
    "provision_project",
    "request_scope",
]
