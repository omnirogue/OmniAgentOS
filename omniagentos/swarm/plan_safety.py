"""Deterministic, fail-closed safety decisions for executable swarm plans.

This module is the final authority at every provision/activation boundary.  It
does no I/O other than consulting the checked-in protected-path registry and
never attempts to repair or broaden a model-authored plan.

Ownership is normally a set of workspace-relative FILE PATHS — that is the
conflict-freedom property the coordinator enforces at run time. A task may
instead (or additionally) name a bounded non-file resource,
``resource:<kind>:<name>``, so a non-code mission plan is expressible; such a
plan is decided ``draft`` and can never provision or execute. See
``DRAFT_ONLY_OWNERSHIP_REASON``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts
from omniagentos.policy.protected_paths import path_tier
from omniagentos.swarm.contracts import (
    DRAFT_PLAN_DISPOSITION,
    READY_PLAN_DISPOSITION,
    PlanDisposition,
    PlanIssue,
    SwarmPlan,
    SwarmPlanDecision,
    SwarmTaskSpec,
)

_BOOTSTRAP_TASK_ID = "bootstrap"
_INTEGRATION_TASK_ID = "integration"
_GUARANTEED_FAIL_RE = re.compile(
    r"^\s*(?:exit\s+1|false)\s*;?\s*$",
    flags=re.IGNORECASE,
)
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
_EMBEDDED_DRIVE_PATH_RE = re.compile(r"(?:^|\s)[A-Za-z]:[/\\]")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FORBIDDEN_VERIFY_CHARS = frozenset({";", "&", "|", ">", "<", "`", "$", "\n", "\r", "\x00"})
_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-x",
        "--exitfirst",
        "--strict-config",
        "--strict-markers",
        "--disable-warnings",
    }
)
_PYTEST_VALUE_FLAG_RE = re.compile(
    r"^(?:--tb=(?:auto|long|short|line|native|no)|--color=(?:yes|no|auto)|"
    r"--durations=[1-9][0-9]*|--maxfail=[1-9][0-9]*)$"
)
_UNRESOLVED_PATH_MARKERS = frozenset(
    {"?", "??", "???", "n/a", "na", "none", "null", "tbd", "todo", "unknown", "<path>"}
)
_READ_ONLY_TERMS = (
    "analy",
    "audit",
    "inspect",
    "investigat",
    "read-only",
    "readonly",
    "report",
    "research",
    "review",
    "summar",
)

# --- bounded non-file ownership (operator ruling, 2026-08-10) ---------------
#
# A non-code mission task ("produce the launch email sequence") owns no file
# path, so the file-path-only ownership predicate below refused every such
# plan with ``empty_ownership``. An ownership entry may now instead name a
# BOUNDED non-file resource — ``resource:<kind>:<name>`` — inside the same
# ``owned_paths`` list (no schema change, and the attestation already covers
# that list).
#
# The relaxation is DRAFT-ONLY and admits nothing to execution: the conflict
# freedom the coordinator actually enforces (pre-attempt snapshot, ownership
# revert, path-scoped commit) is defined over file paths, so a resource entry
# is unenforceable at run time. Any plan carrying one is therefore decided
# ``draft`` — visible and reviewable, never executable.
DRAFT_ONLY_OWNERSHIP_CODE = "draft_only_ownership"
DRAFT_ONLY_OWNERSHIP_REASON = "draft-only ownership: operator arming required"
_OWNED_RESOURCE_PREFIX = "resource:"
# Bounded by construction: exactly three colon-separated segments, no path
# separator, no whitespace, no glob, non-empty kind and name, length-capped,
# and each segment must start alphanumeric (so ``.``/``..`` can never appear
# as a whole segment).
_OWNED_RESOURCE_RE = re.compile(
    r"^resource:(?P<kind>[a-z0-9][a-z0-9_-]{0,31}):(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)


@dataclass(frozen=True, slots=True)
class PlanSafetyAttestation:
    """Proof that the safety-relevant plan fields passed the deterministic gate."""

    digest: str


class PlanSafetyError(ValueError):
    """Raised when a provision or activation boundary receives a non-ready plan."""

    def __init__(self, decision: SwarmPlanDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "swarm plan is not safe to provision")


class VerifierCommandError(ValueError):
    """A model-authored verifier command is outside the executable grammar."""

    def __init__(self, message: str, *, code: str = "invalid_verify_command") -> None:
        self.code = code
        super().__init__(message)


def _issue(
    code: str,
    message: str,
    *,
    path: str | None = None,
    **detail: Any,
) -> PlanIssue:
    return PlanIssue(code=code, message=message, path=path, detail=detail)


def _non_ready(
    disposition: PlanDisposition,
    issues: Sequence[PlanIssue],
    reason: str,
) -> SwarmPlanDecision:
    return SwarmPlanDecision(
        disposition=disposition,
        plans=[],
        issues=list(issues),
        reason=reason,
    )


def _is_integration_task(plan: SwarmPlan, task: SwarmTaskSpec) -> bool:
    integration_id = str(plan.integration_task_id or _INTEGRATION_TASK_ID)
    return task.id == integration_id


def _is_clearly_read_only(task: SwarmTaskSpec) -> bool:
    text = f"{task.title}\n{task.description}".lower()
    return any(term in text for term in _READ_ONLY_TERMS)


def _normalize_owned_path(path: str) -> str:
    """Validate planner path shape via the already-registered planner primitive.

    Lazy import avoids a circular module load (planner imports this module at
    top level). The path-security registry already reasons about
    ``swarm.planner.normalize_owned_path``; reimplementing startswith/relative
    checks here would create unregistered decision sites.
    """
    # Import at call time so plan_safety can load before planner finishes.
    from omniagentos.swarm.planner import SwarmPlanError, normalize_owned_path

    try:
        return normalize_owned_path(path)
    except SwarmPlanError as exc:
        raise ValueError(str(exc)) from exc


def is_owned_resource_entry(entry: str) -> bool:
    """True when an ownership entry CLAIMS to be a non-file resource.

    Claim, not validity: the prefix match is case-insensitive so a near-miss
    spelling (``Resource:campaign:x``) is refused as a malformed resource
    rather than silently accepted as a file named ``Resource:campaign:x``.
    """
    return entry.strip().lower().startswith(_OWNED_RESOURCE_PREFIX)


def parse_owned_resource(entry: str) -> tuple[str, str] | None:
    """Return ``(kind, name)`` for a well-formed resource entry, else ``None``."""
    match = _OWNED_RESOURCE_RE.fullmatch(entry.strip())
    if match is None:
        return None
    return match.group("kind"), match.group("name")


def _path_issues(plan: SwarmPlan, task: SwarmTaskSpec) -> tuple[list[PlanIssue], bool, bool]:
    """Return ``(issues, owns_protected_path, owns_non_file_resource)``."""
    issues: list[PlanIssue] = []
    protected = False
    resources: list[str] = []
    is_integration = _is_integration_task(plan, task)

    if (
        not task.owned_paths
        and task.id != _BOOTSTRAP_TASK_ID
        and not is_integration
        and not _is_clearly_read_only(task)
    ):
        issues.append(
            _issue(
                "empty_ownership",
                f"mutating task {task.id!r} has no bounded owned paths",
                task_id=task.id,
            )
        )

    for raw_path in task.owned_paths:
        path_text = str(raw_path).strip()
        if is_owned_resource_entry(path_text):
            parsed = parse_owned_resource(path_text)
            if parsed is None:
                issues.append(
                    _issue(
                        "invalid_owned_resource",
                        f"task {task.id!r} has a malformed resource ownership entry; "
                        "expected resource:<kind>:<name>",
                        path=path_text,
                        task_id=task.id,
                    )
                )
                continue
            resources.append(f"{_OWNED_RESOURCE_PREFIX}{parsed[0]}:{parsed[1]}")
            continue

        try:
            normalized = _normalize_owned_path(path_text)
        except ValueError as exc:
            issues.append(
                _issue(
                    "invalid_owned_path",
                    f"task {task.id!r} has an unresolved or escaping owned path: {exc}",
                    path=path_text or None,
                    task_id=task.id,
                )
            )
            continue

        if normalized == "." and not is_integration:
            issues.append(
                _issue(
                    "root_wide_ownership",
                    f"task {task.id!r} requests root-wide ownership",
                    path=path_text,
                    task_id=task.id,
                )
            )
            continue

        if normalized.lower() in _UNRESOLVED_PATH_MARKERS or any(
            marker in normalized.lower()
            for marker in ("outside-workspace", "outside_workspace", "unresolved")
        ):
            issues.append(
                _issue(
                    "unresolved_owned_path",
                    f"task {task.id!r} has unresolved ownership",
                    path=path_text,
                    task_id=task.id,
                )
            )
            continue

        try:
            tier = path_tier(normalized)
        except Exception as exc:  # noqa: BLE001 -- registry failure must fail closed.
            protected = True
            issues.append(
                _issue(
                    "protected_paths_unavailable",
                    "protected-path policy could not be evaluated",
                    path=normalized,
                    task_id=task.id,
                    error=type(exc).__name__,
                )
            )
            continue
        if tier in {"P", "S"}:
            protected = True
            issues.append(
                _issue(
                    "protected_path",
                    f"task {task.id!r} owns protected tier {tier} path",
                    path=normalized,
                    task_id=task.id,
                    tier=tier,
                )
            )

    if resources:
        # PRESENCE, not exclusivity: a task that mixes one token file path with
        # a resource entry is still unenforceable for the resource half, so
        # mixing must not be an escape hatch back to an executable plan.
        issues.append(
            _issue(
                DRAFT_ONLY_OWNERSHIP_CODE,
                f"task {task.id!r} owns a non-file resource; {DRAFT_ONLY_OWNERSHIP_REASON}",
                task_id=task.id,
                resources=sorted(dict.fromkeys(resources)),
            )
        )

    return issues, protected, bool(resources)


def _validate_verify_target(token: str) -> str:
    """Return one bounded repo-relative verifier target or fail closed."""
    if not token or token.startswith("-"):
        raise VerifierCommandError(f"verifier target is not repo-relative: {token!r}")
    if token.startswith("@"):
        raise VerifierCommandError("verifier response files are forbidden")
    if _DRIVE_PATH_RE.match(token) or token.startswith(("/", "~")):
        raise VerifierCommandError(
            f"verifier target is absolute: {token!r}",
            code="verify_outside_workspace",
        )
    normalized = token.replace("\\", "/").split("::", 1)[0]
    if any(part == ".." for part in normalized.split("/")):
        raise VerifierCommandError(
            f"verifier target traverses outside the repository: {token!r}",
            code="verify_outside_workspace",
        )
    return token


def parse_verifier_command(command: str) -> tuple[str, ...]:
    """Parse the complete model-authored verifier grammar into immutable argv.

    Only repository-scoped pytest, Ruff, mypy, Pyright, and the exact
    ``git diff --check`` command are executable. The returned tuple is passed
    directly to ``subprocess.run`` with ``shell=False``; it is never joined for
    execution.
    """
    if not isinstance(command, str):
        raise VerifierCommandError("verifier command must be a string")
    raw = command.strip()
    if not raw:
        raise VerifierCommandError("verifier command is empty")
    if any(char in raw for char in _FORBIDDEN_VERIFY_CHARS):
        raise VerifierCommandError("verifier command contains forbidden shell syntax")
    if _EMBEDDED_DRIVE_PATH_RE.search(raw):
        raise VerifierCommandError(
            "verifier command contains an absolute path",
            code="verify_outside_workspace",
        )
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise VerifierCommandError(f"verifier command is unparsable: {exc}") from exc
    if not tokens:
        raise VerifierCommandError("verifier command is empty")
    if any(
        _DRIVE_PATH_RE.match(token) or token.startswith(("/", "~"))
        for token in tokens
    ):
        raise VerifierCommandError(
            "verifier command contains an absolute path",
            code="verify_outside_workspace",
        )
    if _ENV_ASSIGNMENT_RE.match(tokens[0]):
        raise VerifierCommandError("verifier environment assignments are forbidden")
    if any(token.startswith("@") for token in tokens):
        raise VerifierCommandError("verifier response files are forbidden")

    if tokens == ["git", "diff", "--check"]:
        return tuple(tokens)

    target_start = 1
    if tokens[:3] == ["python", "-m", "pytest"]:
        tool = "pytest"
        target_start = 3
    elif tokens[0] == "pytest":
        tool = "pytest"
    elif tokens[:2] == ["ruff", "check"]:
        tool = "ruff"
        target_start = 2
    elif tokens[0] in {"mypy", "pyright"}:
        tool = tokens[0]
    else:
        raise VerifierCommandError(
            "verifier executable is not allowed; expected pytest, python -m pytest, "
            "ruff check, mypy, pyright, or exact git diff --check"
        )

    targets: list[str] = []
    for token in tokens[target_start:]:
        if tool == "pytest" and (
            token in _PYTEST_FLAGS or _PYTEST_VALUE_FLAG_RE.fullmatch(token) is not None
        ):
            continue
        if token.startswith("-"):
            raise VerifierCommandError(f"verifier option is not allowed: {token!r}")
        targets.append(_validate_verify_target(token))
    if not targets:
        if tool != "pytest":
            raise VerifierCommandError(f"{tool} verifier requires a repo-relative target")
        # Pytest's omitted target means the current directory. Make that
        # repository-relative scope explicit in the immutable execution argv.
        tokens.append(".")
    return tuple(tokens)


def _verifier_targets(argv: Sequence[str]) -> tuple[str, ...]:
    """Extract target operands from already-parsed strict verifier argv."""
    tokens = tuple(argv)
    if tokens == ("git", "diff", "--check"):
        return ()
    if tokens[:3] == ("python", "-m", "pytest"):
        start = 3
        pytest = True
    elif tokens[:1] == ("pytest",):
        start = 1
        pytest = True
    elif tokens[:2] == ("ruff", "check"):
        start = 2
        pytest = False
    else:
        start = 1
        pytest = False
    return tuple(
        token
        for token in tokens[start:]
        if not (
            pytest
            and (token in _PYTEST_FLAGS or _PYTEST_VALUE_FLAG_RE.fullmatch(token) is not None)
        )
    )


def validate_verifier_targets(
    argv: Sequence[str],
    workspace_dir: str | Path,
) -> tuple[str, ...]:
    """Prove every verifier target exists beneath the actual workspace inode.

    This is deliberately separate from lexical parsing so both plan safety and
    the final pre-execution boundary invoke the same filesystem-truth check.
    Symlinked ancestors/leaves that resolve outside, absent targets, and an
    unavailable workspace all fail closed.
    """
    try:
        root = Path(workspace_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VerifierCommandError(
            f"verifier workspace is unavailable: {exc}",
            code="verify_workspace_unavailable",
        ) from exc
    if not root.is_dir():
        raise VerifierCommandError(
            "verifier workspace is not a directory",
            code="verify_workspace_unavailable",
        )

    for token in _verifier_targets(argv):
        path_part = token.split("::", 1)[0]
        candidate = root / path_part
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise VerifierCommandError(
                f"verifier target does not exist: {token!r}",
                code="verify_target_missing",
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise VerifierCommandError(
                f"verifier target cannot be resolved: {token!r}",
                code="verify_outside_workspace",
            ) from exc
        if inode_relative_parts(resolved, root) is None:
            raise VerifierCommandError(
                f"verifier target resolves outside the workspace: {token!r}",
                code="verify_outside_workspace",
            )
    return tuple(argv)


def _verify_issues(
    task: SwarmTaskSpec,
    workspace_dir: str | Path | None,
    *,
    is_integration: bool = False,
    draft: bool = False,
) -> tuple[list[PlanIssue], bool]:
    """Return issues and whether any issue is execution-impossible.

    ``draft`` relaxes exactly one rule: a draft plan may omit
    ``verify_command`` entirely, because a draft's verification is the human
    review that must arm it before anything runs. A draft that *does* declare
    a command still has it parsed under the same strict grammar — a plan is
    never allowed to name an unsafe verifier, draft or not.
    """
    command = task.verify_command.strip()
    issues: list[PlanIssue] = []
    impossible = False

    if draft and not command:
        return issues, impossible

    # Integration tasks may carry suite-oriented acceptance text while the
    # planner left ``suite_command`` empty; that is not an execution-impossible
    # plan by itself. Only non-integration tasks that explicitly promise a
    # mechanical verify command without providing one fail closed here.
    if not is_integration and task.id != _INTEGRATION_TASK_ID:
        acceptance = task.acceptance.lower()
        promises_mechanical_verification = any(
            phrase in acceptance
            for phrase in (
                "verify command",
                "test command",
                "tests pass",
                "suite is green",
                "suite passes",
            )
        )
        if not command and promises_mechanical_verification:
            issues.append(
                _issue(
                    "missing_verify_command",
                    f"task {task.id!r} requires mechanical verification but has no command",
                    task_id=task.id,
                )
            )
            return issues, impossible

    if not command:
        return issues, impossible
    if _GUARANTEED_FAIL_RE.fullmatch(command):
        impossible = True
        issues.append(
            _issue(
                "guaranteed_fail_verify",
                f"task {task.id!r} has a verification command guaranteed to fail",
                task_id=task.id,
                verify_command=command,
            )
        )
        return issues, impossible
    try:
        argv = parse_verifier_command(command)
        if workspace_dir is not None:
            validate_verifier_targets(argv, workspace_dir)
    except VerifierCommandError as exc:
        impossible = exc.code == "verify_outside_workspace"
        issues.append(
            _issue(
                exc.code,
                f"task {task.id!r} has an unsafe verification command",
                task_id=task.id,
                error=str(exc),
                verify_command=command,
            )
        )
        return issues, impossible
    return issues, impossible


def _planner_failure_decision(plan: SwarmPlan) -> SwarmPlanDecision | None:
    marker = next(
        (
            note.partition("planner failed closed:")[2].strip()
            for note in plan.assumptions
            if "planner failed closed:" in note
        ),
        "",
    )
    if not marker:
        return None
    unavailable = "unavailable" in marker.lower() or "no structured output" in marker.lower()
    disposition: PlanDisposition = "planner_unavailable" if unavailable else "invalid_plan"
    return _non_ready(
        disposition,
        [_issue(disposition, marker)],
        f"swarm planner failed closed: {marker}",
    )


def evaluate_plan_safety(
    plan: SwarmPlan | None = None,
    *,
    plans: Sequence[SwarmPlan] | None = None,
    workspace_dir: str | Path | None = None,
    allow_multi_bundle: bool = False,
) -> SwarmPlanDecision:
    """Return the typed executable/non-executable decision for one or more plans."""
    if plan is not None and plans is not None:
        return _non_ready(
            "invalid_plan",
            [_issue("ambiguous_plan_input", "provide plan or plans, not both")],
            "plan safety received ambiguous plan input",
        )
    selected = list(plans) if plans is not None else ([] if plan is None else [plan])
    return decide_from_plans(
        selected,
        workspace_dir=workspace_dir,
        allow_multi_bundle=allow_multi_bundle,
    )


def decide_from_plans(
    plans: Sequence[SwarmPlan],
    *,
    workspace_dir: str | Path | None = None,
    allow_multi_bundle: bool = False,
) -> SwarmPlanDecision:
    """Evaluate complete bundle output without silently dropping any plan."""
    plan_list = list(plans)
    if not plan_list:
        return _non_ready(
            "invalid_plan",
            [_issue("empty_plan", "planner returned no plans")],
            "planner returned no executable plans",
        )
    if len(plan_list) >= 2 and not allow_multi_bundle:
        return _non_ready(
            "needs_clarification",
            [
                _issue(
                    "multiple_bundles",
                    "grouped multi-bundle provisioning is unavailable",
                    bundle_count=len(plan_list),
                )
            ],
            "grouped multi-bundle provisioning is unavailable; submit each bundle separately",
        )

    all_issues: list[PlanIssue] = []
    has_protected = False
    has_impossible = False
    has_draft_ownership = False
    for candidate in plan_list:
        planner_failure = _planner_failure_decision(candidate)
        if planner_failure is not None:
            return planner_failure
        if not candidate.tasks:
            all_issues.append(_issue("empty_plan", "plan contains no tasks"))
            continue
        # Ownership is classified for EVERY task first: draft status is a
        # plan-level property (one resource-owning task makes the whole plan a
        # draft), and the verify rule depends on it.
        ownership: list[tuple[SwarmTaskSpec, list[PlanIssue]]] = []
        plan_is_draft = False
        for task in candidate.tasks:
            path_issues, protected, resource_owned = _path_issues(candidate, task)
            ownership.append((task, path_issues))
            has_protected = has_protected or protected
            plan_is_draft = plan_is_draft or resource_owned
        has_draft_ownership = has_draft_ownership or plan_is_draft
        for task, path_issues in ownership:
            verify_issues, impossible = _verify_issues(task, workspace_dir, draft=plan_is_draft)
            all_issues.extend(path_issues)
            all_issues.extend(verify_issues)
            has_impossible = has_impossible or impossible

    # A draft-ownership issue blocks execution but is not a defect: any OTHER
    # issue outranks it, so a draft plan that is also unsafe still reports the
    # unsafe disposition (and the same reason text a non-draft plan reports).
    blocking = [issue for issue in all_issues if issue.code != DRAFT_ONLY_OWNERSHIP_CODE]
    if blocking:
        disposition: PlanDisposition = (
            "policy_denied" if has_protected else "impossible" if has_impossible else "invalid_plan"
        )
        return _non_ready(
            disposition,
            all_issues,
            f"plan safety rejected {len(blocking)} issue(s)",
        )
    if has_draft_ownership:
        return _non_ready(
            DRAFT_PLAN_DISPOSITION,
            all_issues,
            DRAFT_ONLY_OWNERSHIP_REASON,
        )
    return SwarmPlanDecision(
        disposition=READY_PLAN_DISPOSITION,
        plans=plan_list,
        reason="plan passed deterministic safety checks",
    )


def plan_safety_attestation(plan: SwarmPlan) -> str:
    """Stable hash of every field that influences the safety decision."""
    payload = {
        "goal": plan.goal,
        "integration_task_id": plan.integration_task_id,
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "owned_paths": list(task.owned_paths),
                "acceptance": task.acceptance,
                "verify_command": task.verify_command,
            }
            for task in plan.tasks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assert_plan_safe_for_provision(
    plan: SwarmPlan,
    *,
    workspace_dir: str | Path | None = None,
    expected_attestation: str | None = None,
) -> PlanSafetyAttestation:
    """Raise :class:`PlanSafetyError` unless ``plan`` is safe at this boundary.

    ``draft`` is not ready: a plan with non-file (resource) ownership raises
    here, which is what keeps a draft out of ``provision_run`` — and therefore
    out of the scheduler, which only ever executes provisioned runs.
    """
    decision = evaluate_plan_safety(plan, workspace_dir=workspace_dir)
    if not decision.is_ready:
        raise PlanSafetyError(decision)
    digest = plan_safety_attestation(plan)
    if expected_attestation is not None and digest != expected_attestation:
        raise PlanSafetyError(
            _non_ready(
                "invalid_plan",
                [
                    _issue(
                        "attestation_mismatch",
                        "plan safety-relevant fields changed after validation",
                        expected=expected_attestation,
                        actual=digest,
                    )
                ],
                "plan changed after its safety decision",
            )
        )
    return PlanSafetyAttestation(digest=digest)


__all__ = [
    "DRAFT_ONLY_OWNERSHIP_CODE",
    "DRAFT_ONLY_OWNERSHIP_REASON",
    "PlanSafetyAttestation",
    "PlanSafetyError",
    "VerifierCommandError",
    "assert_plan_safe_for_provision",
    "decide_from_plans",
    "evaluate_plan_safety",
    "is_owned_resource_entry",
    "parse_owned_resource",
    "parse_verifier_command",
    "plan_safety_attestation",
    "validate_verifier_targets",
]
