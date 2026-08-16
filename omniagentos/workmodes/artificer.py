"""TN.2 — the artificer path: a non-code deliverable never touches the repo.

Today a copywriting task gets a repo-scoped worker started with
``--permission-mode acceptEdits`` and the repo as its working directory. That is
the wrong shape entirely: the deliverable is an ad, the worker is holding a
license to edit source, and the only thing standing between the two is the
model's good judgement. This module gives the non-code modes the shape they
should have had:

* :func:`plan_artificer` resolves the mode's roots (TN.1) plus any explicitly
  granted directories, and names ONE working directory that is not the repo.
* :func:`guard_writes` classifies every path the task actually wrote. A path
  outside the mode's write roots is a violation, a path inside the repo is a
  violation with its own name (``repo_write``) because that is the failure
  everyone will be looking for, and a path inside a read-only root
  (``intake_processing``'s uploads) is a violation even though the root is
  granted — read-only means read-only.
* :func:`enforce_writes` is the only function that can refuse, and it refuses
  only when :func:`omniagentos.workmodes.modes.work_modes_mode` says ``enforce``.

Every containment decision delegates to
:func:`omniagentos.scope.paths.resolve_into_realm`, which resolves ``..`` and
symlinks and fails closed on anything ambiguous. There is no second copy of that
logic here, and there must never be one: a guard with its own path algebra is a
guard with its own bypass.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from omniagentos.contracts import SandboxLevel, TaskMode
from omniagentos.scope.paths import resolve_into_realm
from omniagentos.workmodes.modes import (
    ModeRoots,
    WorkModeError,
    coerce_task_mode,
    policy_for,
    resolve_roots,
    target_location_for,
    work_modes_mode,
)

__all__ = [
    "ArtificerPlan",
    "ArtifactScopeViolation",
    "GuardVerdict",
    "VIOLATION_KINDS",
    "WorkerShape",
    "WriteFinding",
    "artificer_prompt",
    "coerce_mode_or_raise",
    "enforce_writes",
    "guard_writes",
    "plan_artificer",
    "worker_shape",
]


class ArtifactScopeViolation(WorkModeError):
    """A non-code task tried to write outside its artifact roots.

    Carries the whole :class:`GuardVerdict` rather than a message, because the
    interesting part of a scope failure is the LIST of offending paths, and a
    caller that only gets a string has to parse it back out.
    """

    def __init__(self, verdict: GuardVerdict) -> None:
        paths = ", ".join(f.path for f in verdict.violations[:5])
        extra = "" if len(verdict.violations) <= 5 else f" (+{len(verdict.violations) - 5} more)"
        super().__init__(f"{verdict.mode.value} task wrote outside its roots: {paths}{extra}")
        self.verdict = verdict


#: Every non-ok classification a written path can get. ``repo_write`` is broken
#: out from ``outside_roots`` on purpose: they are the same rule but not the same
#: incident, and only one of them is "the bug this package exists to stop".
VIOLATION_KINDS: tuple[str, ...] = (
    "read_only_root",
    "repo_write",
    "outside_roots",
    "unresolvable",
)


def _repo_root_default() -> str:
    """The OmniAgentOS checkout, derived from this file's location.

    Same anchor as ``contracts._repo_root``. A caller working on some OTHER
    repository passes ``repo_root=`` explicitly; the default is only used to give
    the ``repo_write`` violation its name.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class ArtificerPlan:
    """Where one mode+scope may write, and where it may not.

    ``write_roots`` is ordered most-specific-first (mode roots, then granted
    dirs) so the first matching root in :func:`guard_writes` is the most precise
    explanation of why a path was allowed.
    """

    mode: TaskMode
    scope: str
    roots: ModeRoots
    working_dir: str
    write_roots: tuple[str, ...]
    read_only_roots: tuple[str, ...]
    repo_root: str
    repo_write_allowed: bool
    dropped_roots: tuple[tuple[str, str], ...] = ()

    @property
    def is_artificer(self) -> bool:
        """True for every mode that is forbidden the repo (everything but ``code``)."""
        return not self.repo_write_allowed


def _resolved(path: str) -> str | None:
    text = path.strip().strip("\"'") if isinstance(path, str) else ""
    if not text:
        return None
    try:
        return os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):
        return None


def _secret_adjacent(path: str) -> str | None:
    """A drop reason when ``path`` is/contains a credential store, else ``None``.

    Reuses ``policy.dir_grants.references_secret_dir`` — the repo's existing
    granted-root check, which rejects BOTH directions (a grant OF ``~/.ssh`` and
    a grant of ``~``, which engulfs it). Imported lazily because ``dir_grants``
    reads the mounts and secrets registries at import time, and this package must
    stay cheap to import.

    Fails CLOSED: if the check itself cannot run, the root is dropped. A write
    root is an expansion of authority, so "we could not verify it" and "it is not
    allowed" have to have the same consequence.
    """
    try:
        from omniagentos.policy.dir_grants import references_secret_dir

        if references_secret_dir(path):
            return "is or engulfs a secret directory"
    except Exception as exc:  # noqa: BLE001 -- fail closed on an unusable registry
        return f"secret-directory check unavailable ({type(exc).__name__})"
    return None


def _contained_in(candidate: str, roots: Sequence[str]) -> str | None:
    """The first root in ``roots`` that provably contains ``candidate``.

    ``resolve_into_realm`` returns ``None`` for anything it cannot prove, so an
    unresolvable path, a different drive and a symlink that escapes all read as
    "not contained" instead of as a plausible-looking relative path.
    """
    for root in roots:
        if resolve_into_realm(candidate, root) is not None:
            return root
    return None


def plan_artificer(
    mode: TaskMode | str,
    scope: str,
    *,
    goal: str | None = None,
    granted_roots: Iterable[str] = (),
    base: str | None = None,
    repo_root: str | None = None,
) -> ArtificerPlan:
    """Resolve the write/read roots and working directory for one mode+scope. PURE.

    Creates nothing on disk (call
    :func:`omniagentos.workmodes.modes.provision_roots` for that) so a planner
    can reason about the shape of a task before deciding to run it.

    ``goal`` is parsed for an explicit destination through
    :func:`omniagentos.workmodes.modes.target_location_for` — the fast lane's
    own parser, which already refuses everything outside ``$HOME`` and every
    secret/system dir. What it does NOT refuse is a path inside this repo (the
    repo lives under ``$HOME``), so every extra root — parsed or granted — is
    additionally dropped here when it lands inside the repo and the mode is not
    ``code``. That is the whole point of the mode: "write the ad copy into
    ~/OmniAgentOS/src" is not a destination, it is the bug.

    The working directory is the mode's most task-shaped root: the mutable
    workspace when the mode has one (``intake_processing`` needs somewhere to
    unpack and scratch), otherwise the artifacts root. For ``code`` it is the
    repo, unchanged from today.
    """
    policy = policy_for(mode)
    roots = resolve_roots(policy.mode, scope, base=base)
    repo = _resolved(repo_root or _repo_root_default()) or _repo_root_default()

    mode_write_roots = list(roots.write_roots)
    read_only = list(roots.read_only_roots)

    extras: list[str] = []
    dropped: list[tuple[str, str]] = []
    candidates: list[str] = []
    parsed = target_location_for(goal)
    if parsed:
        candidates.append(parsed)
    candidates.extend(str(root) for root in granted_roots)

    for raw in candidates:
        resolved = _resolved(raw)
        if resolved is None:
            dropped.append((str(raw), "unresolvable"))
            continue
        secret_reason = _secret_adjacent(resolved)
        if secret_reason is not None:
            dropped.append((resolved, secret_reason))
            continue
        if resolved in extras or resolved in mode_write_roots:
            continue
        # Order matters: the mode roots live UNDER <repo>/var, so "is it inside a
        # mode root" has to be asked before "is it inside the repo", or every
        # artifact root would be dropped as a repo write.
        if _contained_in(resolved, mode_write_roots) is not None:
            continue
        if not policy.repo_scope and resolve_into_realm(resolved, repo) is not None:
            dropped.append((resolved, "inside the repo; a non-code mode may not write there"))
            continue
        extras.append(resolved)

    if policy.repo_scope:
        working_dir = repo
    elif roots.workspace is not None:
        working_dir = roots.workspace
    elif roots.artifacts is not None:
        working_dir = roots.artifacts
    else:  # pragma: no cover -- every non-code mode has an artifacts root by policy
        raise WorkModeError(f"mode {policy.mode.value} has no writable root to work in")

    write_roots = tuple(dict.fromkeys([*mode_write_roots, *extras]))
    if policy.repo_scope:
        write_roots = tuple(dict.fromkeys([repo, *write_roots]))

    return ArtificerPlan(
        mode=policy.mode,
        scope=roots.scope,
        roots=roots,
        working_dir=working_dir,
        write_roots=write_roots,
        read_only_roots=tuple(read_only),
        repo_root=repo,
        repo_write_allowed=policy.repo_scope,
        dropped_roots=tuple(dropped),
    )


@dataclass(frozen=True)
class WriteFinding:
    """One written path, classified. ``kind == "ok"`` means allowed."""

    path: str
    kind: str
    root: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


@dataclass(frozen=True)
class GuardVerdict:
    """The classification of every path a task wrote.

    ``applicable`` is False for ``code``, whose scope is verified by the TN.0
    declared-vs-observed machinery against a git diff. Reporting ``ok=True`` for
    code here is not a rubber stamp — it is this guard saying "not my
    jurisdiction", and the ``applicable`` flag is what stops a caller from
    reading it as evidence.
    """

    mode: TaskMode
    applicable: bool
    ok: bool
    findings: tuple[WriteFinding, ...]
    gate: str
    reasons: tuple[str, ...] = ()

    @property
    def violations(self) -> tuple[WriteFinding, ...]:
        return tuple(f for f in self.findings if not f.ok)

    @property
    def repo_writes(self) -> tuple[WriteFinding, ...]:
        return tuple(f for f in self.findings if f.kind == "repo_write")


def guard_writes(paths: Iterable[str], plan: ArtificerPlan) -> GuardVerdict:
    """Classify every written path against ``plan``. NEVER raises, never blocks.

    Read-only roots are checked BEFORE write roots would otherwise match, because
    for ``intake_processing`` the inputs root is granted-but-unwritable and a
    write there has to be named as such rather than disappearing into
    "outside_roots".
    """
    gate = work_modes_mode()
    if plan.repo_write_allowed:
        return GuardVerdict(
            mode=plan.mode,
            applicable=False,
            ok=True,
            findings=(),
            gate=gate,
            reasons=("code mode: repo scope is verified by declared-vs-observed, not here",),
        )

    findings: list[WriteFinding] = []
    for raw in paths:
        resolved = _resolved(str(raw))
        if resolved is None:
            findings.append(WriteFinding(str(raw), "unresolvable", None, "path does not resolve"))
            continue
        read_only_root = _contained_in(resolved, plan.read_only_roots)
        if read_only_root is not None:
            findings.append(
                WriteFinding(
                    resolved,
                    "read_only_root",
                    read_only_root,
                    "inputs are read-only for this mode",
                )
            )
            continue
        write_root = _contained_in(resolved, plan.write_roots)
        if write_root is not None:
            findings.append(WriteFinding(resolved, "ok", write_root))
            continue
        if resolve_into_realm(resolved, plan.repo_root) is not None:
            findings.append(
                WriteFinding(
                    resolved,
                    "repo_write",
                    plan.repo_root,
                    f"{plan.mode.value} deliverables never write the repo",
                )
            )
            continue
        findings.append(WriteFinding(resolved, "outside_roots", None, "outside every granted root"))

    ok = all(finding.ok for finding in findings)
    return GuardVerdict(
        mode=plan.mode,
        applicable=True,
        ok=ok,
        findings=tuple(findings),
        gate=gate,
        reasons=() if ok else (f"{sum(1 for f in findings if not f.ok)} write(s) out of scope",),
    )


def enforce_writes(
    paths: Iterable[str], plan: ArtificerPlan, *, gate: str | None = None
) -> GuardVerdict:
    """Classify, and RAISE on a violation — but only when the gate says ``enforce``.

    ``off`` and ``observe`` both return the verdict and let the caller record it.
    That difference is the whole rollout ramp: the verdict is free to be wrong
    while nobody is acting on it, and by the time anybody flips ``enforce`` the
    false-positive rate has been measured rather than assumed.
    """
    verdict = guard_writes(paths, plan)
    effective = gate if gate is not None else work_modes_mode()
    if verdict.applicable and not verdict.ok and effective == "enforce":
        raise ArtifactScopeViolation(verdict)
    return verdict


@dataclass(frozen=True)
class WorkerShape:
    """Advisory worker parameters for a mode. DATA, not wiring.

    Returned so the lane that spawns the worker has one place to read "what shape
    should this process be", instead of each lane re-deriving it. Nothing here
    starts a process, and nothing here is read by any existing lane yet.
    """

    mode: TaskMode
    working_dir: str
    extra_dirs: tuple[str, ...]
    sandbox_level: SandboxLevel
    repo_scoped: bool
    permission_mode: str


def worker_shape(plan: ArtificerPlan) -> WorkerShape:
    """The worker parameters implied by ``plan``.

    ``acceptEdits`` is kept for both shapes and the CONTAINMENT comes from the
    working directory plus ``extra_dirs``, not from the permission mode: a
    permission mode is a prompt-level preference the model can be talked out of,
    whereas a working directory it was never given is a directory it cannot name.
    """
    return WorkerShape(
        mode=plan.mode,
        working_dir=plan.working_dir,
        extra_dirs=tuple(root for root in plan.write_roots if root != plan.working_dir),
        sandbox_level=SandboxLevel.WORKSPACE_WRITE,
        repo_scoped=plan.repo_write_allowed,
        permission_mode="acceptEdits",
    )


def artificer_prompt(goal: str, plan: ArtificerPlan) -> str:
    """The root-discipline block appended to a non-code worker's prompt.

    Mirrors ``intake.fastlane.target_and_todo_prompt``'s shape (absolute paths, a
    worked example, an explicit out-of-scope statement) because that phrasing is
    already tuned against real sessions. It is an instruction, not a control:
    :func:`guard_writes` is what actually decides, and this text only lowers how
    often the guard has to.
    """
    if plan.repo_write_allowed:
        return goal.strip()
    parts = [goal.strip()]
    example = os.path.join(
        plan.working_dir, "example" + (".png" if plan.mode is TaskMode.IMAGE else ".md")
    )
    parts.append(
        "IMPORTANT — every deliverable goes under this exact directory, using absolute "
        f"paths: {plan.working_dir}\n(e.g. 'example' must be written to {example})."
    )
    others = [root for root in plan.write_roots if root != plan.working_dir]
    if others:
        listed = "\n".join(f"- {root}" for root in others)
        parts.append("You may also write within:\n" + listed)
    if plan.read_only_roots:
        listed = "\n".join(f"- {root}" for root in plan.read_only_roots)
        parts.append(
            "READ-ONLY — the files here are the human's originals; read them, never "
            "modify, move or delete them:\n" + listed
        )
    parts.append(
        f"This is a {plan.mode.value} task, not a code task: do NOT edit, create or delete "
        f"anything inside the repository at {plan.repo_root}. Writes outside the directories "
        "listed above are rejected."
    )
    return "\n\n".join(parts)


def coerce_mode_or_raise(value: TaskMode | str | None) -> TaskMode:
    """A :class:`TaskMode` or a clear error. Convenience for lane entry points."""
    mode = coerce_task_mode(value)
    if mode is None:
        raise WorkModeError(f"unknown task mode: {value!r}")
    return mode
