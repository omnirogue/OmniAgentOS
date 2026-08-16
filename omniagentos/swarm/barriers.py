"""Control-plane barrier predicates (§4.3) as pure functions over already-fetched state.

These predicates encode the seven §4.3 barriers plus the documented integration-task
exemption. They are deliberately free of I/O, network, DB access, and any import from
``omniagentos.swarm`` (including scheduler and dal). W3.1 is the sole scheduler owner:
that lane will call these from ``scheduler.py`` as small call-site edits.

``TERMINAL_TASK_STATUSES`` is mirrored here rather than imported from the scheduler so
this module stays scheduler-free and cannot create a cycle or force a 5,989-line merge
conflict when W3.1 lands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Barrier name constants
# ---------------------------------------------------------------------------

BARRIER_CONTRACT = "contract"
BARRIER_DEPENDENCY = "dependency"
BARRIER_INTEGRATION_EXEMPTION = "integration_exemption"
BARRIER_LOCAL_COMPLETION = "local_completion"
BARRIER_WORKSTREAM = "workstream"
BARRIER_GLOBAL_MERGE = "global_merge"
BARRIER_ACCEPTANCE = "acceptance"
BARRIER_LEARNING = "learning"

# Mirrored from the scheduler deliberately — not imported — to keep this module free of
# omniagentos.swarm imports. Values must stay in lockstep with scheduler terminal statuses.
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"done", "blocked", "cancelled"})

CONDITION_VERIFIED = "VERIFIED"  # W1.1's per-edge default
CONDITION_DONE = "DONE"  # legacy status-only release

TERMINAL_DISPOSITIONS: frozenset[str] = frozenset(
    {"accepted", "rejected", "cancelled", "abandoned"}
)


# ---------------------------------------------------------------------------
# Result and input value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarrierResult:
    """Outcome of one barrier predicate evaluation."""

    barrier: str
    released: bool
    reason: str  # stable machine code, e.g. "stale_report"
    detail: str  # human sentence naming the ids
    blocking_ids: tuple[str, ...] = ()
    satisfied_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependencyEdge:
    """One predecessor edge as already-fetched state (no DB access)."""

    task_id: str
    depends_on_task_id: str
    condition: str = CONDITION_VERIFIED
    predecessor_status: str | None = None  # None == board row missing (dangling)


@dataclass(frozen=True)
class WorkerTask:
    """A worker task participating in local-completion evaluation."""

    task_id: str
    required: bool = True
    current_hash: str | None = None


@dataclass(frozen=True)
class WorkstreamState:
    """One workstream's readiness for the global-merge barrier."""

    workstream_id: str
    mandatory: bool = True
    ready: bool = False


# ---------------------------------------------------------------------------
# Structural protocols for lanes that have not landed yet (W1.2 / W1.3 / W1.4)
# ---------------------------------------------------------------------------


class VerificationReportLike(Protocol):
    """Minimal structural shape of W1.3's VerificationReport.

    ``verdict`` is ``"pass" | "fail" | "insufficient_evidence"`` (no ``"error"``).
    ``satisfies(current_hash)`` is ``verdict == "pass" and not is_stale_for(current_hash)``.
    """

    artifact_hash: str
    verdict: str

    def is_stale_for(self, current_hash: str) -> bool: ...

    def satisfies(self, current_hash: str) -> bool: ...


class ContextVerdictLike(Protocol):
    """Minimal structural shape of W1.2's completeness verdict."""

    ready: bool


class AuditResultLike(Protocol):
    """Minimal structural shape of a post-merge audit result (W1.4 posture)."""

    verdict: str  # "pass" | "fail" | "inconclusive"


class ProjectStateLike(Protocol):
    """Minimal structural shape of project disposition for the learning barrier."""

    disposition: str | None
    project_id: str


# ---------------------------------------------------------------------------
# Shared helpers (report satisfaction with / without an expected hash)
# ---------------------------------------------------------------------------


def _report_block_reason(
    report: VerificationReportLike | None,
    current_hash: str | None,
) -> str | None:
    """Return a machine reason if the report does not release, else None."""
    if report is None:
        return "missing_report"
    if current_hash is not None:
        if not current_hash:
            return "missing_current_hash"
        if report.satisfies(current_hash):
            return None
        if report.is_stale_for(current_hash):
            return "stale_report"
        return "verification_not_passing"
    # No current_hash provided: require a plain pass verdict.
    if report.verdict == "pass":
        return None
    return "verification_not_passing"


# ---------------------------------------------------------------------------
# 1. Contract barrier
# ---------------------------------------------------------------------------


def contract_barrier(
    contract_state: str | None,
    context_verdict: ContextVerdictLike | None,
) -> BarrierResult:
    """Release only when the contract is APPROVED and context is ready.

    Both inputs are required. A missing or non-approved contract state yields
    ``contract_not_approved``; a missing context verdict yields ``context_missing``;
    a present-but-not-ready verdict yields ``context_not_ready``.
    """
    barrier = BARRIER_CONTRACT
    normalized = (contract_state or "").strip().casefold()
    if normalized != "approved":
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="contract_not_approved",
            detail=(
                f"contract state {contract_state!r} is not APPROVED"
                if contract_state is not None
                else "contract state is missing"
            ),
        )
    if context_verdict is None:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="context_missing",
            detail="context verdict is missing",
        )
    if not context_verdict.ready:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="context_not_ready",
            detail="context verdict is present but not ready",
        )
    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="released",
        detail="contract APPROVED and context ready",
    )


# ---------------------------------------------------------------------------
# 2. Dependency barrier
# ---------------------------------------------------------------------------


def dependency_barrier(
    edges: Sequence[DependencyEdge],
    reports_by_task: Mapping[str, VerificationReportLike],
    hashes_by_task: Mapping[str, str],
) -> BarrierResult:
    """Strict dependency release requiring done status and, for VERIFIED edges, a
    report that ``satisfies`` the predecessor's current artifact hash.

    A task with no predecessors is genuinely eligible (``no_dependencies``) — that is
    deliberate, not a vacuous pass. A missing predecessor board row never defaults to
    ``done`` (the scheduler ``status_by_id.get(dep, "done")`` defect at :3035 dies here).
    Blocked/cancelled predecessors never satisfy a VERIFIED edge.
    """
    barrier = BARRIER_DEPENDENCY
    if not edges:
        return BarrierResult(
            barrier=barrier,
            released=True,
            reason="no_dependencies",
            detail="task has no predecessor edges; genuinely eligible",
        )

    blocking: list[str] = []
    satisfied: list[str] = []
    first_reason: str | None = None
    first_detail: str | None = None

    for edge in edges:
        pred = edge.depends_on_task_id
        status = edge.predecessor_status
        edge_reason: str | None = None
        edge_detail: str

        if status is None:
            edge_reason = "missing_predecessor"
            edge_detail = (
                f"predecessor {pred!r} has no board row (dangling edge on {edge.task_id!r})"
            )
        elif status in ("blocked", "cancelled"):
            edge_reason = "predecessor_terminal_not_done"
            edge_detail = (
                f"predecessor {pred!r} is terminal-not-done status {status!r} "
                f"(edge on {edge.task_id!r})"
            )
        elif status != "done":
            edge_reason = "predecessor_not_done"
            edge_detail = (
                f"predecessor {pred!r} status is {status!r}, not done "
                f"(edge on {edge.task_id!r})"
            )
        elif edge.condition == CONDITION_DONE:
            edge_reason = None
            edge_detail = f"predecessor {pred!r} done under DONE condition"
        elif edge.condition == CONDITION_VERIFIED:
            current_hash = hashes_by_task.get(pred)
            if current_hash is None or current_hash == "":
                edge_reason = "missing_current_hash"
                edge_detail = (
                    f"predecessor {pred!r} has no non-empty current hash "
                    f"(edge on {edge.task_id!r})"
                )
            else:
                report = reports_by_task.get(pred)
                block = _report_block_reason(report, current_hash)
                if block is not None:
                    edge_reason = block
                    edge_detail = (
                        f"predecessor {pred!r} report does not satisfy current hash "
                        f"{current_hash!r} ({block}; edge on {edge.task_id!r})"
                    )
                else:
                    edge_reason = None
                    edge_detail = (
                        f"predecessor {pred!r} verified for hash {current_hash!r}"
                    )
        else:
            edge_reason = "unknown_condition"
            edge_detail = (
                f"predecessor {pred!r} has unknown condition {edge.condition!r} "
                f"(edge on {edge.task_id!r})"
            )

        if edge_reason is not None:
            blocking.append(pred)
            if first_reason is None:
                first_reason = edge_reason
                first_detail = edge_detail
        else:
            satisfied.append(pred)

    if blocking:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason=first_reason or "dependencies_not_satisfied",
            detail=first_detail
            or (
                f"blocking predecessors: {', '.join(blocking)} "
                f"(task {edges[0].task_id!r})"
            ),
            blocking_ids=tuple(blocking),
            satisfied_ids=tuple(satisfied),
        )

    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="all_dependencies_satisfied",
        detail=(
            f"all predecessors satisfied for task {edges[0].task_id!r}: "
            f"{', '.join(satisfied)}"
        ),
        satisfied_ids=tuple(satisfied),
    )


# ---------------------------------------------------------------------------
# 3. Integration exemption
# ---------------------------------------------------------------------------


def integration_exemption(
    edges: Sequence[DependencyEdge],
    statuses: Mapping[str, str],
) -> BarrierResult:
    """Documented integration-task-only exemption for mixed terminal predecessors.

    Mirrors ``_integration_ready`` (scheduler.py:3012-3038) **minus** the missing-dep
    default. The single intentional divergence from today's scheduler is that a
    predecessor id absent from ``statuses`` is NOT released (``missing_dependency``)
    rather than silently treated as ``"done"`` via ``status_by_id.get(dep, "done")``.

    This predicate is opt-in: the general ``dependency_barrier`` never applies this
    rule. Callers (W3.1) must invoke it only for integration tasks.

    Edge ``predecessor_status`` / ``condition`` fields are ignored; this predicate
    reads ``statuses`` exactly like the scheduler's ``status_by_id``.
    """
    barrier = BARRIER_INTEGRATION_EXEMPTION
    if not edges:
        return BarrierResult(
            barrier=barrier,
            released=True,
            reason="no_dependencies",
            detail="integration task has no predecessor edges",
        )

    dep_ids = [e.depends_on_task_id for e in edges]
    missing = [d for d in dep_ids if d not in statuses]
    if missing:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="missing_dependency",
            detail=(
                f"predecessor id(s) absent from statuses (never default to done): "
                f"{', '.join(missing)}"
            ),
            blocking_ids=tuple(missing),
        )

    status_list = [statuses[d] for d in dep_ids]
    if all(s == "done" for s in status_list):
        # Plain eligibility already covers the all-done case; exemption not needed.
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="plain_eligibility_covers_this",
            detail=(
                f"all predecessors are done ({', '.join(dep_ids)}); "
                "use dependency_barrier instead"
            ),
            satisfied_ids=tuple(dep_ids),
        )
    if all(s in TERMINAL_TASK_STATUSES for s in status_list):
        return BarrierResult(
            barrier=barrier,
            released=True,
            reason="all_predecessors_terminal",
            detail=(
                f"all predecessors terminal for integration exemption: "
                f"{', '.join(f'{d}={statuses[d]}' for d in dep_ids)}"
            ),
            satisfied_ids=tuple(dep_ids),
        )
    non_terminal = [d for d in dep_ids if statuses[d] not in TERMINAL_TASK_STATUSES]
    return BarrierResult(
        barrier=barrier,
        released=False,
        reason="non_terminal_predecessors",
        detail=(
            f"non-terminal predecessors block integration exemption: "
            f"{', '.join(f'{d}={statuses[d]}' for d in non_terminal)}"
        ),
        blocking_ids=tuple(non_terminal),
    )


# ---------------------------------------------------------------------------
# 4. Local completion barrier
# ---------------------------------------------------------------------------


def local_completion_barrier(
    worker_tasks: Sequence[WorkerTask],
    reports_by_task: Mapping[str, VerificationReportLike],
) -> BarrierResult:
    """Release when every *required* worker has a report that satisfies its hash.

    Empty input or no required workers fails closed (``no_required_workers``) — refuse
    the vacuous pass. Optional workers are ignored.
    """
    barrier = BARRIER_LOCAL_COMPLETION
    required = [w for w in worker_tasks if w.required]
    if not required:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="no_required_workers",
            detail="no required worker tasks provided; refusing vacuous pass",
        )

    blocking: list[str] = []
    satisfied: list[str] = []
    first_reason: str | None = None
    first_detail: str | None = None

    for worker in required:
        tid = worker.task_id
        if worker.current_hash is None or worker.current_hash == "":
            blocking.append(tid)
            if first_reason is None:
                first_reason = "missing_current_hash"
                first_detail = f"required worker {tid!r} has no non-empty current_hash"
            continue
        report = reports_by_task.get(tid)
        block = _report_block_reason(report, worker.current_hash)
        if block is not None:
            blocking.append(tid)
            if first_reason is None:
                first_reason = block
                first_detail = (
                    f"required worker {tid!r} report does not satisfy hash "
                    f"{worker.current_hash!r} ({block})"
                )
            continue
        satisfied.append(tid)

    if blocking:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason=first_reason or "workers_not_verified",
            detail=first_detail
            or f"blocking required workers: {', '.join(blocking)}",
            blocking_ids=tuple(blocking),
            satisfied_ids=tuple(satisfied),
        )

    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="all_required_workers_verified",
        detail=f"all required workers verified: {', '.join(satisfied)}",
        satisfied_ids=tuple(satisfied),
    )


# ---------------------------------------------------------------------------
# 5. Workstream barrier
# ---------------------------------------------------------------------------


def workstream_barrier(
    integration_verification: VerificationReportLike | None,
    *,
    current_hash: str | None = None,
) -> BarrierResult:
    """Release when the integration verification report is ready for global merge.

    With ``current_hash``, require ``satisfies(current_hash)`` (splitting stale vs
    not-passing). Without, require ``verdict == "pass"``.
    """
    barrier = BARRIER_WORKSTREAM
    block = _report_block_reason(integration_verification, current_hash)
    if block is not None:
        hash_part = f" for hash {current_hash!r}" if current_hash else ""
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason=block,
            detail=f"integration verification not ready{hash_part} ({block})",
        )
    hash_part = f" for hash {current_hash!r}" if current_hash else ""
    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="ready_for_global_merge",
        detail=f"integration verification ready for global merge{hash_part}",
    )


# ---------------------------------------------------------------------------
# 6. Global merge barrier
# ---------------------------------------------------------------------------


def global_merge_barrier(workstreams: Sequence[WorkstreamState]) -> BarrierResult:
    """Release when every mandatory workstream is ready.

    Empty input or no mandatory workstreams fails closed
    (``no_mandatory_workstreams``).
    """
    barrier = BARRIER_GLOBAL_MERGE
    mandatory = [w for w in workstreams if w.mandatory]
    if not mandatory:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="no_mandatory_workstreams",
            detail="no mandatory workstreams provided; refusing vacuous pass",
        )

    not_ready = [w.workstream_id for w in mandatory if not w.ready]
    if not_ready:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="workstream_not_ready",
            detail=f"mandatory workstreams not ready: {', '.join(not_ready)}",
            blocking_ids=tuple(not_ready),
        )

    ready_ids = tuple(w.workstream_id for w in mandatory)
    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="all_mandatory_workstreams_ready",
        detail=f"all mandatory workstreams ready: {', '.join(ready_ids)}",
        satisfied_ids=ready_ids,
    )


# ---------------------------------------------------------------------------
# 7. Acceptance barrier
# ---------------------------------------------------------------------------


def acceptance_barrier(
    post_merge_report: VerificationReportLike | None,
    audit_result: AuditResultLike | None,
    *,
    current_hash: str | None = None,
) -> BarrierResult:
    """Release only when post-merge verification and audit both pass.

    Audit posture is fail-closed: missing audit, ``inconclusive``, and ``fail`` all
    refuse release. Report handling mirrors workstream (hash-aware when given).
    """
    barrier = BARRIER_ACCEPTANCE
    report_block = _report_block_reason(post_merge_report, current_hash)
    if report_block is not None:
        hash_part = f" for hash {current_hash!r}" if current_hash else ""
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason=report_block,
            detail=f"post-merge report not accepting{hash_part} ({report_block})",
        )

    if audit_result is None:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="audit_missing",
            detail="post-merge audit result is missing",
        )
    verdict = audit_result.verdict
    if verdict == "inconclusive":
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="audit_inconclusive",
            detail="post-merge audit verdict is inconclusive (fail closed)",
        )
    if verdict == "fail":
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="audit_failed",
            detail="post-merge audit verdict is fail",
        )
    if verdict != "pass":
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="audit_failed",
            detail=f"post-merge audit verdict is {verdict!r}, not pass",
        )

    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="accepted",
        detail="post-merge report and audit both pass",
    )


# ---------------------------------------------------------------------------
# 8. Learning barrier
# ---------------------------------------------------------------------------


def learning_barrier(project_state: ProjectStateLike | None) -> BarrierResult:
    """Release only when the project has reached a terminal disposition.

    Missing state fails closed. Disposition is compared stripped and lowercased
    against ``TERMINAL_DISPOSITIONS``.
    """
    barrier = BARRIER_LEARNING
    if project_state is None:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="no_project_state",
            detail="project state is missing",
        )
    disposition = project_state.disposition
    project_id = project_state.project_id
    if disposition is None:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="no_terminal_disposition",
            detail=f"project {project_id!r} has no disposition",
            blocking_ids=(project_id,),
        )
    normalized = disposition.strip().casefold()
    if normalized not in TERMINAL_DISPOSITIONS:
        return BarrierResult(
            barrier=barrier,
            released=False,
            reason="no_terminal_disposition",
            detail=(
                f"project {project_id!r} disposition {disposition!r} "
                "is not terminal"
            ),
            blocking_ids=(project_id,),
        )
    return BarrierResult(
        barrier=barrier,
        released=True,
        reason="terminal_disposition_reached",
        detail=(
            f"project {project_id!r} reached terminal disposition {normalized!r}"
        ),
        satisfied_ids=(project_id,),
    )
