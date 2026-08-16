"""Tests for pure §4.3 barrier predicates (W1.5).

Hermetic: local stubs only, no DB, no network, no real models. Report stubs implement
the real ``satisfies`` rule (``verdict == "pass" and not is_stale_for(h)``).
"""

from __future__ import annotations

from dataclasses import dataclass

from omniagentos.swarm.barriers import (
    BARRIER_ACCEPTANCE,
    BARRIER_CONTRACT,
    BARRIER_DEPENDENCY,
    BARRIER_GLOBAL_MERGE,
    BARRIER_INTEGRATION_EXEMPTION,
    BARRIER_LEARNING,
    BARRIER_LOCAL_COMPLETION,
    BARRIER_WORKSTREAM,
    CONDITION_DONE,
    CONDITION_VERIFIED,
    TERMINAL_DISPOSITIONS,
    TERMINAL_TASK_STATUSES,
    DependencyEdge,
    WorkerTask,
    WorkstreamState,
    acceptance_barrier,
    contract_barrier,
    dependency_barrier,
    global_merge_barrier,
    integration_exemption,
    learning_barrier,
    local_completion_barrier,
    workstream_barrier,
)

# ---------------------------------------------------------------------------
# Stubs implementing the real W1.2 / W1.3 / W1.4 seams
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StubReport:
    """VerificationReport-like stub with the real satisfies rule."""

    artifact_hash: str
    verdict: str  # "pass" | "fail" | "insufficient_evidence"

    def is_stale_for(self, current_hash: str) -> bool:
        return self.artifact_hash != current_hash

    def satisfies(self, current_hash: str) -> bool:
        return self.verdict == "pass" and not self.is_stale_for(current_hash)


@dataclass(frozen=True)
class StubContext:
    ready: bool


@dataclass(frozen=True)
class StubAudit:
    verdict: str  # "pass" | "fail" | "inconclusive"


@dataclass(frozen=True)
class StubProject:
    project_id: str
    disposition: str | None


# ---------------------------------------------------------------------------
# Contractual tests (exact names)
# ---------------------------------------------------------------------------


def test_dependency_release_requires_a_satisfying_report_for_the_current_hash() -> None:
    """VERIFIED edge needs report.satisfies(current_hash); status done alone is not enough."""
    edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_VERIFIED,
            predecessor_status="done",
        )
    ]
    current = "hash-v2"

    # Missing report: blocked
    missing = dependency_barrier(edges, reports_by_task={}, hashes_by_task={"task-a": current})
    assert missing.released is False
    assert missing.reason == "missing_report"
    assert missing.blocking_ids == ("task-a",)
    assert "task-a" in missing.detail

    # Fail verdict for current hash: blocked
    fail_report = StubReport(artifact_hash=current, verdict="fail")
    failing = dependency_barrier(
        edges,
        reports_by_task={"task-a": fail_report},
        hashes_by_task={"task-a": current},
    )
    assert failing.released is False
    assert failing.reason == "verification_not_passing"
    assert failing.blocking_ids == ("task-a",)

    # Pass + matching hash: released
    good = StubReport(artifact_hash=current, verdict="pass")
    ok = dependency_barrier(
        edges,
        reports_by_task={"task-a": good},
        hashes_by_task={"task-a": current},
    )
    assert ok.released is True
    assert ok.reason == "all_dependencies_satisfied"
    assert ok.satisfied_ids == ("task-a",)
    assert "task-a" in ok.detail

    # DONE condition still releases on status alone (no report required)
    done_edge = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_DONE,
            predecessor_status="done",
        )
    ]
    done_ok = dependency_barrier(done_edge, reports_by_task={}, hashes_by_task={})
    assert done_ok.released is True
    assert done_ok.reason == "all_dependencies_satisfied"


def test_a_stale_report_hash_blocks_release_even_with_verdict_pass() -> None:
    """A pass verdict on a different artifact hash must not release a VERIFIED edge."""
    edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_VERIFIED,
            predecessor_status="done",
        )
    ]
    stale = StubReport(artifact_hash="hash-old", verdict="pass")
    result = dependency_barrier(
        edges,
        reports_by_task={"task-a": stale},
        hashes_by_task={"task-a": "hash-new"},
    )
    assert result.released is False
    assert result.reason == "stale_report"
    assert result.blocking_ids == ("task-a",)
    assert "task-a" in result.detail
    # Prove the stub seam: satisfies is False because of staleness, not verdict.
    assert stale.verdict == "pass"
    assert stale.is_stale_for("hash-new") is True
    assert stale.satisfies("hash-new") is False


def test_blocked_or_cancelled_predecessors_never_satisfy_a_verified_edge() -> None:
    """Terminal-not-done predecessors never satisfy VERIFIED, even with a good report."""
    good = StubReport(artifact_hash="h1", verdict="pass")
    for status in ("blocked", "cancelled"):
        edges = [
            DependencyEdge(
                task_id="task-b",
                depends_on_task_id="task-a",
                condition=CONDITION_VERIFIED,
                predecessor_status=status,
            )
        ]
        result = dependency_barrier(
            edges,
            reports_by_task={"task-a": good},
            hashes_by_task={"task-a": "h1"},
        )
        assert result.released is False, f"status={status}"
        assert result.reason == "predecessor_terminal_not_done", f"status={status}"
        assert result.blocking_ids == ("task-a",)
        assert "task-a" in result.detail


def test_a_missing_dependency_row_blocks_instead_of_defaulting_to_done() -> None:
    """:3035 regression pin — missing board rows must never default to done.

    Today's buggy scheduler line was::

        statuses = [status_by_id.get(dep, "done") for dep in deps]

    A dangling ``swarm_deps`` edge therefore satisfied the integration gate. Both
    ``dependency_barrier`` and ``integration_exemption`` refuse that case.
    """
    dangling = DependencyEdge(
        task_id="integration",
        depends_on_task_id="ghost-pred",
        condition=CONDITION_VERIFIED,
        predecessor_status=None,  # board row does not exist
    )
    edges = [dangling]

    # General barrier: missing predecessor row is NOT released.
    dep = dependency_barrier(edges, reports_by_task={}, hashes_by_task={})
    assert dep.released is False
    assert dep.reason == "missing_predecessor"
    assert dep.blocking_ids == ("ghost-pred",)
    assert "ghost-pred" in dep.detail

    # Integration exemption: missing from statuses is NOT released (intentional fix).
    exempt = integration_exemption(edges, statuses={})  # ghost-pred absent
    assert exempt.released is False
    assert exempt.reason == "missing_dependency"
    assert exempt.blocking_ids == ("ghost-pred",)
    assert "ghost-pred" in exempt.detail

    # Inline reimplementation of the old buggy line: it WOULD have released.
    # status_by_id.get(dep, "done")  <-- the defect being killed
    deps = [e.depends_on_task_id for e in edges]
    status_by_id: dict[str, str] = {}  # empty: ghost-pred missing
    buggy_statuses = [status_by_id.get(dep, "done") for dep in deps]
    assert all(s in TERMINAL_TASK_STATUSES for s in buggy_statuses)
    # And the old integration logic would have treated all-done as "plain eligibility"
    # or mixed-terminal as release — with the default, a lone missing row becomes "done"
    # and returns False from exemption (plain path). The critical bug is that a *mix*
    # of real blocked + missing-as-done still released. Encode that:
    status_by_id_mixed = {"real-blocked": "blocked"}
    mixed_deps = ["real-blocked", "ghost-pred"]
    buggy_mixed = [status_by_id_mixed.get(d, "done") for d in mixed_deps]
    assert buggy_mixed == ["blocked", "done"]
    assert all(s in TERMINAL_TASK_STATUSES for s in buggy_mixed)
    # Old exemption would release on that mix; new exemption must not for the ghost.
    mixed_edges = [
        DependencyEdge(
            task_id="integration",
            depends_on_task_id="real-blocked",
            predecessor_status="blocked",
        ),
        DependencyEdge(
            task_id="integration",
            depends_on_task_id="ghost-pred",
            predecessor_status=None,
        ),
    ]
    fixed = integration_exemption(mixed_edges, statuses={"real-blocked": "blocked"})
    assert fixed.released is False
    assert fixed.reason == "missing_dependency"
    assert "ghost-pred" in fixed.blocking_ids


def test_the_integration_exemption_releases_on_all_terminal_statuses() -> None:
    """Mixed done + blocked + cancelled predecessors release the integration exemption."""
    edges = [
        DependencyEdge(task_id="int", depends_on_task_id="a", predecessor_status="done"),
        DependencyEdge(task_id="int", depends_on_task_id="b", predecessor_status="blocked"),
        DependencyEdge(task_id="int", depends_on_task_id="c", predecessor_status="cancelled"),
    ]
    statuses = {"a": "done", "b": "blocked", "c": "cancelled"}
    result = integration_exemption(edges, statuses=statuses)
    assert result.released is True
    assert result.reason == "all_predecessors_terminal"
    assert set(result.satisfied_ids) == {"a", "b", "c"}
    assert "a" in result.detail and "b" in result.detail

    # All-done must NOT use the exemption (plain eligibility covers it).
    all_done = integration_exemption(
        [DependencyEdge(task_id="int", depends_on_task_id="a")],
        statuses={"a": "done"},
    )
    assert all_done.released is False
    assert all_done.reason == "plain_eligibility_covers_this"

    # Non-terminal predecessor refuses.
    open_pred = integration_exemption(
        [
            DependencyEdge(task_id="int", depends_on_task_id="a", predecessor_status="done"),
            DependencyEdge(task_id="int", depends_on_task_id="b", predecessor_status="open"),
        ],
        statuses={"a": "done", "b": "open"},
    )
    assert open_pred.released is False
    assert open_pred.reason == "non_terminal_predecessors"
    assert open_pred.blocking_ids == ("b",)


def test_the_integration_exemption_is_not_reachable_from_the_general_barrier() -> None:
    """With one done and one blocked predecessor, exemption releases; general barrier does not."""
    edges = [
        DependencyEdge(
            task_id="int",
            depends_on_task_id="done-pred",
            condition=CONDITION_VERIFIED,
            predecessor_status="done",
        ),
        DependencyEdge(
            task_id="int",
            depends_on_task_id="blocked-pred",
            condition=CONDITION_VERIFIED,
            predecessor_status="blocked",
        ),
    ]
    good = StubReport(artifact_hash="h", verdict="pass")
    reports = {"done-pred": good, "blocked-pred": good}
    hashes = {"done-pred": "h", "blocked-pred": "h"}
    statuses = {"done-pred": "done", "blocked-pred": "blocked"}

    exempt = integration_exemption(edges, statuses=statuses)
    assert exempt.released is True
    assert exempt.reason == "all_predecessors_terminal"

    general = dependency_barrier(edges, reports_by_task=reports, hashes_by_task=hashes)
    assert general.released is False
    assert general.reason == "predecessor_terminal_not_done"
    assert "blocked-pred" in general.blocking_ids


def test_contract_barrier_requires_both_approved_contract_and_ready_context() -> None:
    """Both APPROVED contract and ready context are required; each failure has its reason."""
    missing_contract = contract_barrier(None, StubContext(ready=True))
    assert missing_contract.released is False
    assert missing_contract.reason == "contract_not_approved"

    not_approved = contract_barrier("draft", StubContext(ready=True))
    assert not_approved.released is False
    assert not_approved.reason == "contract_not_approved"

    missing_ctx = contract_barrier("APPROVED", None)
    assert missing_ctx.released is False
    assert missing_ctx.reason == "context_missing"

    not_ready = contract_barrier("APPROVED", StubContext(ready=False))
    assert not_ready.released is False
    assert not_ready.reason == "context_not_ready"

    ok = contract_barrier("  approved  ", StubContext(ready=True))
    assert ok.released is True
    assert ok.reason == "released"
    assert ok.barrier == BARRIER_CONTRACT


def test_acceptance_barrier_fails_closed_on_an_inconclusive_audit() -> None:
    """Inconclusive audit must refuse acceptance even when the report passes."""
    report = StubReport(artifact_hash="h1", verdict="pass")
    result = acceptance_barrier(
        report,
        StubAudit(verdict="inconclusive"),
        current_hash="h1",
    )
    assert result.released is False
    assert result.reason == "audit_inconclusive"
    assert result.barrier == BARRIER_ACCEPTANCE

    # Missing audit also fails closed.
    missing = acceptance_barrier(report, None, current_hash="h1")
    assert missing.released is False
    assert missing.reason == "audit_missing"

    # Fail audit refuses.
    failed = acceptance_barrier(report, StubAudit(verdict="fail"), current_hash="h1")
    assert failed.released is False
    assert failed.reason == "audit_failed"

    # Both pass releases.
    ok = acceptance_barrier(report, StubAudit(verdict="pass"), current_hash="h1")
    assert ok.released is True
    assert ok.reason == "accepted"


def test_every_result_names_its_reason_and_ids() -> None:
    """Every BarrierResult carries a stable reason and names concrete ids in detail."""
    edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_VERIFIED,
            predecessor_status="open",
        )
    ]
    result = dependency_barrier(edges, reports_by_task={}, hashes_by_task={})
    assert result.released is False
    assert result.reason == "predecessor_not_done"
    assert result.blocking_ids == ("task-a",)
    assert "task-a" in result.detail
    assert result.barrier == BARRIER_DEPENDENCY

    # Released path also names ids.
    good_edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_DONE,
            predecessor_status="done",
        )
    ]
    ok = dependency_barrier(good_edges, reports_by_task={}, hashes_by_task={})
    assert ok.released is True
    assert ok.reason == "all_dependencies_satisfied"
    assert ok.satisfied_ids == ("task-a",)
    assert "task-a" in ok.detail


# ---------------------------------------------------------------------------
# Additional coverage: remaining predicates + fail-closed paths
# ---------------------------------------------------------------------------


def test_local_completion_barrier_requires_verified_required_workers() -> None:
    """Empty / no-required fails closed; required workers need hash + satisfying report."""
    empty = local_completion_barrier([], reports_by_task={})
    assert empty.released is False
    assert empty.reason == "no_required_workers"
    assert empty.barrier == BARRIER_LOCAL_COMPLETION

    optional_only = local_completion_barrier(
        [WorkerTask(task_id="opt", required=False, current_hash="h")],
        reports_by_task={},
    )
    assert optional_only.released is False
    assert optional_only.reason == "no_required_workers"

    no_hash = local_completion_barrier(
        [WorkerTask(task_id="w1", required=True, current_hash=None)],
        reports_by_task={},
    )
    assert no_hash.released is False
    assert no_hash.reason == "missing_current_hash"
    assert no_hash.blocking_ids == ("w1",)

    no_report = local_completion_barrier(
        [WorkerTask(task_id="w1", required=True, current_hash="h1")],
        reports_by_task={},
    )
    assert no_report.released is False
    assert no_report.reason == "missing_report"
    assert no_report.blocking_ids == ("w1",)

    stale = local_completion_barrier(
        [WorkerTask(task_id="w1", required=True, current_hash="h-new")],
        reports_by_task={"w1": StubReport(artifact_hash="h-old", verdict="pass")},
    )
    assert stale.released is False
    assert stale.reason == "stale_report"
    assert stale.blocking_ids == ("w1",)

    fail = local_completion_barrier(
        [WorkerTask(task_id="w1", required=True, current_hash="h1")],
        reports_by_task={"w1": StubReport(artifact_hash="h1", verdict="fail")},
    )
    assert fail.released is False
    assert fail.reason == "verification_not_passing"

    ok = local_completion_barrier(
        [
            WorkerTask(task_id="w1", required=True, current_hash="h1"),
            WorkerTask(task_id="opt", required=False, current_hash=None),
        ],
        reports_by_task={"w1": StubReport(artifact_hash="h1", verdict="pass")},
    )
    assert ok.released is True
    assert ok.reason == "all_required_workers_verified"
    assert ok.satisfied_ids == ("w1",)
    assert "w1" in ok.detail


def test_workstream_barrier_requires_passing_integration_verification() -> None:
    """Missing / failing / stale integration reports block; pass releases."""
    missing = workstream_barrier(None)
    assert missing.released is False
    assert missing.reason == "missing_report"
    assert missing.barrier == BARRIER_WORKSTREAM

    fail = workstream_barrier(StubReport(artifact_hash="h", verdict="fail"))
    assert fail.released is False
    assert fail.reason == "verification_not_passing"

    stale = workstream_barrier(
        StubReport(artifact_hash="old", verdict="pass"),
        current_hash="new",
    )
    assert stale.released is False
    assert stale.reason == "stale_report"

    ok_no_hash = workstream_barrier(StubReport(artifact_hash="h", verdict="pass"))
    assert ok_no_hash.released is True
    assert ok_no_hash.reason == "ready_for_global_merge"

    ok_hash = workstream_barrier(
        StubReport(artifact_hash="h1", verdict="pass"),
        current_hash="h1",
    )
    assert ok_hash.released is True
    assert ok_hash.reason == "ready_for_global_merge"


def test_global_merge_barrier_requires_all_mandatory_workstreams_ready() -> None:
    """Empty / no-mandatory fails closed; any not-ready mandatory blocks with ids."""
    empty = global_merge_barrier([])
    assert empty.released is False
    assert empty.reason == "no_mandatory_workstreams"
    assert empty.barrier == BARRIER_GLOBAL_MERGE

    optional_only = global_merge_barrier(
        [WorkstreamState(workstream_id="ws-opt", mandatory=False, ready=True)]
    )
    assert optional_only.released is False
    assert optional_only.reason == "no_mandatory_workstreams"

    not_ready = global_merge_barrier(
        [
            WorkstreamState(workstream_id="ws-a", mandatory=True, ready=True),
            WorkstreamState(workstream_id="ws-b", mandatory=True, ready=False),
        ]
    )
    assert not_ready.released is False
    assert not_ready.reason == "workstream_not_ready"
    assert not_ready.blocking_ids == ("ws-b",)
    assert "ws-b" in not_ready.detail

    ok = global_merge_barrier(
        [
            WorkstreamState(workstream_id="ws-a", mandatory=True, ready=True),
            WorkstreamState(workstream_id="ws-opt", mandatory=False, ready=False),
        ]
    )
    assert ok.released is True
    assert ok.reason == "all_mandatory_workstreams_ready"
    assert ok.satisfied_ids == ("ws-a",)


def test_learning_barrier_requires_terminal_disposition() -> None:
    """Missing state / non-terminal disposition refuse; terminal dispositions release."""
    missing = learning_barrier(None)
    assert missing.released is False
    assert missing.reason == "no_project_state"
    assert missing.barrier == BARRIER_LEARNING

    no_disp = learning_barrier(StubProject(project_id="proj-1", disposition=None))
    assert no_disp.released is False
    assert no_disp.reason == "no_terminal_disposition"
    assert no_disp.blocking_ids == ("proj-1",)

    open_disp = learning_barrier(StubProject(project_id="proj-1", disposition="active"))
    assert open_disp.released is False
    assert open_disp.reason == "no_terminal_disposition"
    assert "proj-1" in open_disp.detail

    for disposition in sorted(TERMINAL_DISPOSITIONS):
        result = learning_barrier(
            StubProject(project_id="proj-1", disposition=f"  {disposition.upper()}  ")
        )
        assert result.released is True, disposition
        assert result.reason == "terminal_disposition_reached"
        assert result.satisfied_ids == ("proj-1",)
        assert "proj-1" in result.detail


def test_dependency_unknown_condition_fails_closed() -> None:
    """Any condition string other than VERIFIED/DONE blocks with unknown_condition."""
    edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition="MAYBE",
            predecessor_status="done",
        )
    ]
    result = dependency_barrier(edges, reports_by_task={}, hashes_by_task={})
    assert result.released is False
    assert result.reason == "unknown_condition"
    assert result.blocking_ids == ("task-a",)
    assert "task-a" in result.detail


def test_dependency_no_edges_is_deliberately_eligible() -> None:
    """A task with no predecessors is genuinely eligible (not a vacuous accident)."""
    result = dependency_barrier([], reports_by_task={}, hashes_by_task={})
    assert result.released is True
    assert result.reason == "no_dependencies"
    assert result.barrier == BARRIER_DEPENDENCY


def test_dependency_collects_all_blocking_ids_with_first_reason() -> None:
    """All blocking predecessor ids are collected; overall reason is the first in edge order."""
    edges = [
        DependencyEdge(
            task_id="task-z",
            depends_on_task_id="first",
            condition=CONDITION_VERIFIED,
            predecessor_status="open",
        ),
        DependencyEdge(
            task_id="task-z",
            depends_on_task_id="second",
            condition=CONDITION_VERIFIED,
            predecessor_status=None,
        ),
    ]
    result = dependency_barrier(edges, reports_by_task={}, hashes_by_task={})
    assert result.released is False
    assert result.reason == "predecessor_not_done"  # first edge's reason
    assert result.blocking_ids == ("first", "second")


def test_dependency_missing_current_hash_blocks_verified_edge() -> None:
    """VERIFIED edge with done status but no hash in hashes_by_task is blocked."""
    edges = [
        DependencyEdge(
            task_id="task-b",
            depends_on_task_id="task-a",
            condition=CONDITION_VERIFIED,
            predecessor_status="done",
        )
    ]
    result = dependency_barrier(
        edges,
        reports_by_task={"task-a": StubReport(artifact_hash="h", verdict="pass")},
        hashes_by_task={},  # missing
    )
    assert result.released is False
    assert result.reason == "missing_current_hash"
    assert result.blocking_ids == ("task-a",)

    empty_hash = dependency_barrier(
        edges,
        reports_by_task={"task-a": StubReport(artifact_hash="h", verdict="pass")},
        hashes_by_task={"task-a": ""},
    )
    assert empty_hash.released is False
    assert empty_hash.reason == "missing_current_hash"


def test_integration_exemption_no_edges_releases() -> None:
    """No edges on the exemption path is released (mirrors scheduler)."""
    result = integration_exemption([], statuses={})
    assert result.released is True
    assert result.reason == "no_dependencies"
    assert result.barrier == BARRIER_INTEGRATION_EXEMPTION


def test_acceptance_stale_report_blocks_before_audit() -> None:
    """Stale post-merge report refuses even if audit would pass."""
    result = acceptance_barrier(
        StubReport(artifact_hash="old", verdict="pass"),
        StubAudit(verdict="pass"),
        current_hash="new",
    )
    assert result.released is False
    assert result.reason == "stale_report"


def test_barrier_name_constants_are_stable() -> None:
    """Stable machine names for each barrier (used by W3.1 logging)."""
    assert BARRIER_CONTRACT == "contract"
    assert BARRIER_DEPENDENCY == "dependency"
    assert BARRIER_INTEGRATION_EXEMPTION == "integration_exemption"
    assert BARRIER_LOCAL_COMPLETION == "local_completion"
    assert BARRIER_WORKSTREAM == "workstream"
    assert BARRIER_GLOBAL_MERGE == "global_merge"
    assert BARRIER_ACCEPTANCE == "acceptance"
    assert BARRIER_LEARNING == "learning"
    assert TERMINAL_TASK_STATUSES == frozenset({"done", "blocked", "cancelled"})
    assert CONDITION_VERIFIED == "VERIFIED"
    assert CONDITION_DONE == "DONE"
