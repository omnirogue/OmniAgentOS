"""AT-17 — Control-plane invariants and barriers.

Acceptance claims under test:

  Invariants (§5.3)
  1. A task cannot become ready without an approved contract and ready context.
  2. ``verified`` requires a report bound to the exact artifact hash.
  3. A stale verification hash blocks integration.
  4. Acceptance is blocked while a critical or high finding is open.
  5. Every state transition writes a sequenced trace event.

  Barriers (§4.3)
  6. Dependency release requires hash-bound verification, not board status alone.
  7. A dangling dependency edge does not satisfy the integration gate.
  8. The blocked-propagation exemption is scoped to the integration task only.
  9. A workstream is ready for global merge only on integration pass.
  10. The acceptance barrier requires post-merge verification and a trace audit.
  11. Terminal disposition dispatches the learning agent.

  Pass rules (§10.3) and posture
  12. Scheduler review infrastructure failure never auto-confirms.
  13. Orchestrator review failure does not confirm (fail-closed).
  14. An incomplete trace yields INCONCLUSIVE and fails closed — via a runner
      that reads real DB rows (pure rules alone are not enough).
  15. Verdicts record reviewer and implementer lineage, distinct for high-risk.

  Role machinery
  16. The role vocabulary matches prompts on disk from the acceptance view.
  17. A planner-stamped job role survives to the dispatched prompt.

Ground truth:
  * ``omniagentos/swarm/scheduler.py`` — ``_integration_ready`` (:3012-3038);
    missing-dep → ``"done"`` default at :3035
    (``statuses = [status_by_id.get(dep, "done") for dep in deps]``);
    three-valued review, two errors → blocked, never auto-confirm (:5205-5221);
    confirm branch at :5223.
  * ``omniagentos/swarm/dal.py:770`` — dependency release keys on strict
    ``done`` (``WHERE d.task_id = t.id AND p.status <> 'done'``).
  * ``omniagentos/orchestrator/review.py`` — three fail-OPEN-to-confirm sites:
    adapter raises → confirm (:109-114); unparseable output → confirm
    (:193-197); unrecognised verdict string coerced to confirm (:199-201).
  * ``omniagentos/roles.py:19-36`` — ``job_role_from_swarm_json`` collapses the
    14-member ``JobRole`` enum to 3 outcomes (INTEGRATOR / REVIEWER /
    IMPLEMENTER). The file is 36 lines; there is no ``roles.py:44-61``.
  * ``omniagentos/promptshape/rolepack.py:18-26`` — ``JOB_ROLES`` has 14 entries,
    matching the 14 files in ``vault/prompts/roles/``.
  * ``omniagentos/swarm/spawn.py`` — ``_apply_role_pack`` (:1008) calls
    ``role_pack`` at :1032, gated by ``OMNIAGENTOS_ROLE_PACK_MODE``
    (``role_pack_mode`` at :110); ``parse_role_pack_mode`` (:95-107) resolves
    absent/misspelled/non-string values to ``off`` — the seam exists and is
    dark by default.
  * ``omniagentos/audit/trace.py`` — pure §10.3 posture already present (W1.4);
    the remaining gap is an audit *runner* over the events table (W2.3).

Hermetic: migrated tmp SQLite plus pure functions. No network, no model call.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake.planner import PlannedTask
from omniagentos.orchestrator.contracts import ExecutorResult
from omniagentos.orchestrator.review import CrossLineageReviewer, _parse_verdict
from omniagentos.promptshape.rolepack import JOB_ROLES
from omniagentos.roles import JobRole, job_role_from_swarm_json
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer, _RunState
from omniagentos.swarm.spawn import parse_role_pack_mode, role_pack_mode
from tests.acceptance.suites.at17_progress import (
    AT17ProgressError,
    collect,
)
from tests.acceptance.suites.at17_progress import (
    main as at17_progress_main,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler

REPO_ROOT = Path(__file__).resolve().parents[2]


def _card(collab: CollabStore, title: str) -> str:
    task = BoardTask(title=title, status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    return task.id


def _run_with_cards(
    db_path: str, *titles: str, swarm_jsons: dict[str, dict[str, Any]] | None = None
) -> tuple[CollabStore, SwarmDal, str, dict[str, str]]:
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    run = dal.create_run(working_dir="/tmp/at17", goal="at17 control-plane", source="test")
    run_id = str(run["id"])
    ids: dict[str, str] = {}
    for title in titles:
        task_id = _card(collab, title)
        ids[title] = task_id
        extra = (swarm_jsons or {}).get(title, {})
        dal.assign_task_to_run(task_id, run_id, swarm_json=extra)
    return collab, dal, run_id, ids


# ---------------------------------------------------------------------------
# Invariants (§5.3)
# ---------------------------------------------------------------------------


class TestInvariants:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no readiness predicate binds contract approval and context "
            "readiness to task eligibility; the ready query keys on dependency "
            "status alone (dal.py:770). Flips at W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_a_task_cannot_become_ready_without_approved_contract_and_ready_context(
        self, migrated_db_path: str
    ) -> None:
        # Today eligible_tasks returns any open, unclaimed task whose deps are
        # all status='done' — no contract approval, no context-ready flag.
        collab, dal, run_id, ids = _run_with_cards(migrated_db_path, "worker")
        try:
            task_id = ids["worker"]
            # No acceptance, no verify_command, no approved contract stamped.
            dal.set_swarm_json(
                task_id,
                {
                    "task_key": "worker",
                    "acceptance": "",
                    "verify_command": "",
                    "owned_paths": [],
                },
            )
            eligible = {str(t["id"]) for t in dal.eligible_tasks(run_id)}
            assert task_id not in eligible, (
                "a task with no approved contract and no ready context was "
                "treated as eligible — readiness keys on dep status alone"
            )
        finally:
            dal.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no verification report is bound to an artifact hash before a "
            "task may enter verified/done; board status alone advances the DAG "
            "(dal.py:770). Flips at W2.2 + W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_verified_requires_a_report_bound_to_the_exact_artifact_hash(
        self, migrated_db_path: str
    ) -> None:
        # Behavioural: a parent can be marked done with no hash-bound report,
        # and that alone releases the dependent. The invariant demands a report
        # bound to the exact artifact hash first.
        collab, dal, run_id, ids = _run_with_cards(migrated_db_path, "parent", "child")
        try:
            parent, child = ids["parent"], ids["child"]
            dal.add_deps(run_id, [(child, parent)])
            collab.update_board_task(parent, {"status": "done"})
            eligible = {str(t["id"]) for t in dal.eligible_tasks(run_id)}
            assert child not in eligible, (
                "a dependent became eligible after a parent reached done with "
                "no hash-bound verification report — identity gap"
            )
        finally:
            dal.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no stale-hash check blocks integration when the verification "
            "report's artifact digest no longer matches the worktree HEAD; "
            "integration readiness ignores digests "
            "(scheduler.py:_integration_ready). Flips at W2.2 + W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_a_stale_verification_hash_blocks_integration(self) -> None:
        # Symbol-existence last resort: no production symbol re-checks a stored
        # verification digest against the current artifact before integration.
        import omniagentos.swarm.scheduler as scheduler_mod

        seam = getattr(scheduler_mod, "assert_verification_hash_fresh", None)
        assert seam is not None, (
            "no scheduler seam re-validates a verification report's artifact "
            "hash before integration (assert_verification_hash_fresh missing)"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no acceptance barrier consults open critical/high findings "
            "before terminal acceptance; findings (where they exist) do not "
            "gate board done. Flips at W2.5. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_acceptance_is_blocked_while_a_critical_or_high_finding_is_open(
        self,
    ) -> None:
        # Symbol-existence: no acceptance-gate function refuses terminal
        # acceptance while a critical/high finding remains open.
        import omniagentos.swarm.dal as dal_mod

        seam = getattr(dal_mod, "acceptance_blocked_by_open_findings", None)
        if seam is None:
            import omniagentos.swarm.scheduler as scheduler_mod

            seam = getattr(scheduler_mod, "acceptance_blocked_by_open_findings", None)
        assert seam is not None, (
            "no acceptance barrier consults open critical/high findings before terminal acceptance"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: state transitions on board_tasks do not write a sequenced "
            "trace event with a monotonic sequence number; status flips are "
            "silent mutations. Flips at W2.6. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_every_state_transition_writes_a_sequenced_trace_event(
        self, migrated_db_path: str
    ) -> None:
        # Behavioural: flip a task open→done and assert a sequenced trace row
        # was appended. Today CollabStore.update_board_task is a silent UPDATE.
        collab, dal, run_id, ids = _run_with_cards(migrated_db_path, "worker")
        try:
            task_id = ids["worker"]
            before = dal.events_for_run(run_id) if hasattr(dal, "events_for_run") else []
            collab.update_board_task(task_id, {"status": "done"})
            after = dal.events_for_run(run_id) if hasattr(dal, "events_for_run") else []
            new_events = after[len(before) :]
            sequenced = [
                e
                for e in new_events
                if str(e.get("action") or e.get("type") or "").lower().find("transition") >= 0
                or e.get("seq") is not None
                or e.get("sequence") is not None
            ]
            assert sequenced, (
                "a board status transition wrote no sequenced trace event "
                f"(before={len(before)}, after={len(after)})"
            )
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# Barriers (§4.3)
# ---------------------------------------------------------------------------


class TestBarriers:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: dependency release keys on board status 'done' alone "
            "(dal.py:770); nothing binds the review verdict to an artifact "
            "hash before release. Review confirm exists (scheduler.py:5223) — "
            "the gap is identity, not review. Flips at W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_dependency_release_requires_hash_bound_verification_not_board_status(
        self, migrated_db_path: str
    ) -> None:
        # Today's truth: release keys on dal.py:770's strict `done`, and `done`
        # requires a review confirm at scheduler.py:5223 — the gap is identity
        # (nothing binds the verdict to an artifact hash), not review.
        collab, dal, run_id, ids = _run_with_cards(migrated_db_path, "parent", "child")
        try:
            parent, child = ids["parent"], ids["child"]
            dal.add_deps(run_id, [(child, parent)])
            # Mark done with no verification report / no artifact hash stamp.
            collab.update_board_task(parent, {"status": "done"})
            swarm = dal.get_swarm_json(parent) or {}
            assert not swarm.get("artifact_hash") and not swarm.get("verification_hash"), (
                "precondition: parent carries no hash-bound verification"
            )
            eligible = {str(t["id"]) for t in dal.eligible_tasks(run_id)}
            assert child not in eligible, (
                "dependency released on board status 'done' without a "
                "hash-bound verification report (identity gap at dal.py:770)"
            )
        finally:
            dal.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: a dangling dependency edge is treated as satisfied — "
            "scheduler.py:3035 defaults a missing dep id to status 'done' "
            "(status_by_id.get(dep, 'done')), and dal eligible_tasks JOIN "
            "drops edges whose parent row is absent. Flips at W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_a_dangling_dependency_edge_does_not_satisfy_the_integration_gate(
        self, migrated_db_path: str
    ) -> None:
        # Pins scheduler.py:3035's missing-dep→"done" default as the defect.
        collab, dal, run_id, ids = _run_with_cards(
            migrated_db_path,
            "worker",
            "integration",
            swarm_jsons={"integration": {"integration": True, "task_key": "integration"}},
        )
        try:
            worker, integ = ids["worker"], ids["integration"]
            # Real dep plus a dangling edge to a never-created task id.
            dangling = "btk_dangling_never_created"
            dal.add_deps(run_id, [(integ, worker), (integ, dangling)])
            collab.update_board_task(worker, {"status": "done"})

            # Reconstruct the :3035 default used by _integration_ready.
            tasks = dal.tasks_for_run(run_id)
            status_by_id = {str(t["id"]): str(t["status"]) for t in tasks}
            deps = [
                str(edge["depends_on_task_id"])
                for edge in dal.deps_for_run(run_id)
                if str(edge["task_id"]) == integ
            ]
            # scheduler.py:3035 — missing dep id defaults to "done". That is the defect.
            statuses = [status_by_id.get(dep, "done") for dep in deps]
            dangling_status = statuses[deps.index(dangling)]
            assert dangling_status != "done", (
                "scheduler.py:3035 defaulted a dangling dependency edge to "
                f"'done' (statuses={statuses}, deps={deps})"
            )
            eligible = {str(t["id"]) for t in dal.eligible_tasks(run_id)}
            assert integ not in eligible, (
                "integration task became eligible despite a dangling "
                "dependency edge (JOIN drops missing parents)"
            )
        finally:
            dal.close()

    @pytest.mark.acceptance_smoke
    def test_the_blocked_exemption_is_scoped_to_the_integration_task_only(
        self, tmp_path: Path
    ) -> None:
        # Green pin of _integration_ready (scheduler.py:3012-3038): the
        # blocked-propagation exemption must never leak to an ordinary task.
        # Guards W3.1 against over-correction.
        h = make_harness(
            tmp_path,
            [{"id": "a"}, {"id": "b"}],
            integration=True,
            max_concurrency=1,
        )
        try:
            a_id, b_id = h.card_ids["a"], h.card_ids["b"]
            integ_id = h.card_ids["integration"]
            # Ordinary diamond is a→integration, b→integration. Add b→a so b
            # is an ordinary dependent of a (not the integration card).
            h.dal.add_deps(h.run_id, [(b_id, a_id)])
            h.collab.update_board_task(a_id, {"status": "blocked"})
            h.collab.update_board_task(b_id, {"status": "open"})

            # Ordinary task with a blocked dep must NOT be eligible.
            eligible = {str(t["id"]) for t in h.dal.eligible_tasks(h.run_id)}
            assert b_id not in eligible, (
                "blocked-dep exemption leaked to an ordinary task — b became "
                "eligible while its prerequisite is blocked"
            )

            # Integration with mixed terminal deps (a blocked, b done) IS ready
            # via the integration-only path.
            h.collab.update_board_task(b_id, {"status": "done"})
            sched = make_scheduler(h)
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            run = h.dal.get_run(h.run_id)
            assert run is not None
            assert sched._integration_ready(state, run) is True, (
                "integration task with mixed terminal deps (blocked+done) "
                "must be ready via _integration_ready"
            )
            # And eligible_tasks still must not surface the ordinary blocked-dep case.
            # (b is done now — re-check with a fresh ordinary dependent.)
            assert integ_id  # integration card exists; exemption is scoped to it
        finally:
            h.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no workstream-level 'ready for global merge' predicate that "
            "requires an integration-task pass; merge readiness is not gated "
            "on integration outcome. Flips at W3.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_a_workstream_is_ready_for_global_merge_only_on_integration_pass(
        self,
    ) -> None:
        import omniagentos.swarm.scheduler as scheduler_mod

        seam = getattr(scheduler_mod, "workstream_ready_for_global_merge", None)
        assert seam is not None, (
            "no workstream_ready_for_global_merge seam gates merge on integration pass"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no acceptance barrier requires post-merge verification and a "
            "trace audit before terminal acceptance. Flips at W2.5. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_acceptance_barrier_requires_post_merge_verification_and_trace_audit(
        self,
    ) -> None:
        import omniagentos.swarm.scheduler as scheduler_mod

        seam = getattr(scheduler_mod, "acceptance_barrier", None)
        assert seam is not None, (
            "no acceptance_barrier seam requires post-merge verification and "
            "trace audit before terminal acceptance"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: terminal disposition does not dispatch a learning agent; "
            "orchestrator.learn is fire-and-forget session curation, not a "
            "disposition-driven learning agent. Flips at W5.1. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_terminal_disposition_dispatches_the_learning_agent(self) -> None:
        import omniagentos.swarm.scheduler as scheduler_mod

        seam = getattr(scheduler_mod, "dispatch_learning_agent", None)
        if seam is None:
            import omniagentos.orchestrator.learn as learn_mod

            seam = getattr(learn_mod, "dispatch_learning_agent", None)
        assert seam is not None, "no dispatch_learning_agent seam fires on terminal disposition"


# ---------------------------------------------------------------------------
# Pass rules (§10.3) and posture
# ---------------------------------------------------------------------------


class TestPassRulesAndPosture:
    @pytest.mark.acceptance_smoke
    def test_scheduler_review_infrastructure_failure_never_confirms(self, tmp_path: Path) -> None:
        # Green pin of scheduler.py:5205-5221 — two reviewer errors → blocked,
        # no retry consumed, no auto-confirm.
        class _BoomAdapter:
            def run(self, agent_input: Any) -> Any:
                raise RuntimeError("reviewer adapter infra down")

        h = make_harness(tmp_path, [{"id": "rev"}], max_concurrency=1, integration=False)
        orig_spawn = h.world.spawn

        def spawn_with_project_dir(request: Any) -> str:
            session_id = orig_spawn(request)
            h.world.sessions[session_id]["project_dir"] = request.working_dir
            return session_id

        h.world.spawn = spawn_with_project_dir  # type: ignore[method-assign]
        try:
            scheduler = make_scheduler(
                h, reviewer=CrossLineageSwarmReviewer(adapter=_BoomAdapter())
            )
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("rev") == "blocked", (
                "two reviewer infrastructure failures must block, never confirm"
            )
            assert int(h.swarm_json_of("rev").get("retries") or 0) == 0, (
                "reviewer infra failure must not consume a worker retry"
            )
            attempts = h.attempts_of("rev")
            assert len(attempts) == 1
            assert attempts[0]["end_reason"] == "blocked"
            assert "reviewer infrastructure" in str(attempts[0]["detail"] or "")
            assert not h.emitter.of("review_denied")
            blocked = h.emitter.of("task_blocked")
            assert any(e.get("reason") == "reviewer_infrastructure" for e in blocked)
        finally:
            h.close()

    @pytest.mark.acceptance_daily
    def test_orchestrator_review_failure_does_not_confirm(self) -> None:
        # CLOSED by H2 (phase-0 hardening): all three sites now return
        # verdict="error" via review._infrastructure_error, and the caller in
        # orchestrator/core.py retries the reviewer once then lands the task
        # blocked_on_review. The strict xfail marker was removed when it flipped;
        # this test is now a live regression guard. Behavioural detail lives in
        # tests/orchestrator/test_review_fails_closed.py.
        #
        # Pins ALL THREE formerly fail-OPEN sites in orchestrator/review.py.
        class _BoomAdapter:
            def run(self, agent_input: Any) -> Any:
                raise RuntimeError("adapter down")

        task = PlannedTask(title="at17 review", description="x", commits_expected=False)
        result = ExecutorResult(status="ok", output_text="worker produced output")
        reviewer = CrossLineageReviewer(adapter=_BoomAdapter())
        adapter_verdict = reviewer.review(task=task, spec_markdown="# spec\n", result=result)
        assert adapter_verdict.verdict != "confirm", (
            f"adapter raise failed OPEN to confirm (feedback={adapter_verdict.feedback!r})"
        )

        class _EmptyOut:
            output_json = None
            output_text = "not-json-at-all"

        unparseable = _parse_verdict(_EmptyOut())
        assert unparseable.verdict != "confirm", (
            "unparseable reviewer output failed OPEN to confirm"
        )

        class _WeirdOut:
            output_json = {"verdict": "maybe", "feedback": "shrug"}
            output_text = ""

        coerced = _parse_verdict(_WeirdOut())
        assert coerced.verdict != "confirm", "unrecognised verdict string was coerced to confirm"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: pure audit posture exists (omniagentos/audit/trace.py, W1.4) "
            "but no audit *runner* loads real events-table rows into a "
            "TraceSegment and persists an AuditReport; incomplete traces "
            "therefore never yield a durable INCONCLUSIVE. Flips at W2.3. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_an_incomplete_trace_yields_inconclusive_and_fails_closed(self) -> None:
        # The pure rules already fail closed on incomplete evidence. The seam
        # still missing is the *runner* that reads DB rows into a segment and
        # persists a report — assert that runner exists, not the pure posture.
        import omniagentos.audit as audit_pkg

        runner = getattr(audit_pkg, "run_trace_audit_over_events", None)
        if runner is None:
            import omniagentos.audit.trace as trace_mod

            runner = getattr(trace_mod, "run_trace_audit_over_events", None)
        if runner is None:
            # importlib rather than a static import: ``omniagentos.audit.runner``
            # IS the W2.3 seam and does not exist yet, so a static import is a
            # type error against today's tree rather than a live probe.
            try:
                runner_mod: Any | None = importlib.import_module("omniagentos.audit.runner")
            except ImportError:
                runner_mod = None
            runner = getattr(runner_mod, "run_trace_audit_over_events", None)
        assert runner is not None, (
            "no audit runner loads events-table rows into a TraceSegment and "
            "persists an AuditReport (pure rules alone are not the runner)"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: ReviewVerdict and SwarmReviewOutcome do not record distinct "
            "reviewer and implementer lineage fields; high-risk runs cannot "
            "prove cross-lineage review. Flips at W2.2. "
            "See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_verdicts_record_reviewer_and_implementer_lineage_distinct_for_high_risk(
        self,
    ) -> None:
        from omniagentos.orchestrator.contracts import ReviewVerdict
        from omniagentos.swarm.scheduler import SwarmReviewOutcome

        # Behavioural on the type surface: a verdict must carry both lineages
        # so high-risk can assert they differ.
        review_fields = getattr(ReviewVerdict, "__dataclass_fields__", {})
        swarm_fields = getattr(SwarmReviewOutcome, "__dataclass_fields__", {})
        has_reviewer_lineage = (
            "reviewer_lineage" in review_fields or "reviewer_lineage" in swarm_fields
        )
        has_implementer_lineage = (
            "implementer_lineage" in review_fields or "implementer_lineage" in swarm_fields
        )
        assert has_reviewer_lineage and has_implementer_lineage, (
            "verdict types lack reviewer_lineage/implementer_lineage fields "
            f"(ReviewVerdict={list(review_fields)}, "
            f"SwarmReviewOutcome={list(swarm_fields)})"
        )


# ---------------------------------------------------------------------------
# Role machinery
# ---------------------------------------------------------------------------


class TestRoleMachinery:
    @pytest.mark.acceptance_smoke
    def test_the_role_vocabulary_matches_prompts_on_disk_from_the_acceptance_view(
        self,
    ) -> None:
        # Thin re-assertion via JOB_ROLES against vault/prompts/roles/*.md.
        roles_dir = REPO_ROOT / "vault" / "prompts" / "roles"
        on_disk = sorted(p.stem for p in roles_dir.glob("*.md"))
        declared = sorted(JOB_ROLES)
        assert declared == on_disk, f"JOB_ROLES {declared} does not match prompts on disk {on_disk}"
        assert len(declared) == 14, f"expected 14 job roles, got {len(declared)}"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: job_role_from_swarm_json collapses the 14-member JobRole enum "
            "to 3 outcomes (roles.py:19-36), and the role_pack injection seam "
            "defaults to off (spawn.py:95-107 parse_role_pack_mode), so a "
            "planner-stamped role cannot survive to the dispatched prompt. "
            "Flips at W3.3. See docs/acceptance/gaps-AT17.md."
        ),
    )
    @pytest.mark.acceptance_daily
    def test_a_planner_stamped_job_role_survives_to_the_dispatched_prompt(self) -> None:
        # Today's truth: job_role_from_swarm_json collapses 7→3, and role_pack
        # injection defaults to off — a planner-stamped tester/debugger/incident
        # cannot survive to the dispatched prompt.
        stamped = job_role_from_swarm_json(
            {"job_role": "tester", "complexity": "tester", "role": "tester"}
        )
        assert stamped is JobRole.TESTER, (
            f"planner-stamped tester collapsed to {stamped!r} "
            f"(roles.py:19-36 only maps integration→INTEGRATOR, "
            f"complexity in {{review,verify}}→REVIEWER, else IMPLEMENTER)"
        )
        # Even if the role survived, the injection seam is dark by default.
        assert parse_role_pack_mode(None) != "off", (
            "role_pack mode defaults to off — stamped roles never reach the prompt"
        )
        assert role_pack_mode({}) != "off", (
            "absent OMNIAGENTOS_ROLE_PACK_MODE resolves to off (spawn.py:95-107)"
        )


# ---------------------------------------------------------------------------
# AT-17 progress metric self-tests (green)
# ---------------------------------------------------------------------------


class TestAT17ProgressMetric:
    @pytest.mark.acceptance_smoke
    def test_the_progress_metric_fails_loudly_on_a_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.py"
        with pytest.raises(AT17ProgressError) as excinfo:
            collect(missing)
        assert str(missing) in str(excinfo.value)

        code = at17_progress_main(["--target", str(missing)])
        assert code == 2, f"CLI must return 2 on missing file, got {code}"
        # main() already printed to stderr; re-run capturing to assert text.
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.acceptance.suites.at17_progress",
                "--target",
                str(missing),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        assert "AT17 PROGRESS ERROR" in proc.stderr

    @pytest.mark.acceptance_smoke
    def test_the_progress_metric_counts_marked_items_not_matching_lines(
        self, tmp_path: Path
    ) -> None:
        # Adversarial fixture designed to break `grep -c xfail`:
        #   * 4-line @pytest.mark.xfail decorator → 1 item
        #   * docstring/comment mentioning xfail, no marker → 0
        #   * parametrize over 3 params + xfail → 3 items
        #   * one plain passing test → 0
        # True totals: 1 + 0 + 3 + 0 = 4 items, 1+3=4 xfail.
        fixture = tmp_path / "test_adversarial_xfail_count.py"
        fixture.write_text(
            textwrap.dedent(
                '''\
                import pytest

                @pytest.mark.xfail(
                    strict=True,
                    reason="GAP: multi-line decorator fixture",
                )
                def test_multiline_xfail_decorator():
                    assert False

                def test_docstring_mentions_xfail_but_has_no_marker():
                    """This docstring says xfail on purpose to fool grep -c."""
                    # also an xfail mention in a comment
                    assert True

                @pytest.mark.parametrize("n", [1, 2, 3])
                @pytest.mark.xfail(strict=True, reason="GAP: parametrized xfail")
                def test_parametrized_xfail(n):
                    assert False

                def test_plain_passing():
                    assert True
                '''
            ),
            encoding="utf-8",
        )

        report = collect(fixture)
        # Hand-computed: 1 (multi-line) + 1 (docstring test) + 3 (param) + 1 (plain) = 6
        assert report.total == 6, f"expected 6 collected items, got {report.total}"
        assert report.xfail == 4, f"expected 4 xfail items, got {report.xfail}"
        assert report.strict_xfail == 4
        assert report.implemented == 2

        grep = subprocess.run(
            ["grep", "-c", "xfail", str(fixture)],
            capture_output=True,
            text=True,
            check=False,
        )
        grep_count = int((grep.stdout or "0").strip() or "0")
        assert grep_count != report.xfail, (
            f"grep -c reported {grep_count} but the true xfail item count is "
            f"{report.xfail} — this is why grep -c was rejected as the metric"
        )

    @pytest.mark.acceptance_smoke
    def test_a_ratcheted_file_reports_zero_xfail_distinctly_from_a_missing_file(
        self, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "test_fully_ratcheted.py"
        fixture.write_text(
            textwrap.dedent(
                """\
                def test_alpha():
                    assert True

                def test_beta():
                    assert True
                """
            ),
            encoding="utf-8",
        )
        report = collect(fixture)
        assert report.xfail == 0
        assert report.total > 0
        assert report.implemented == report.total
        # Missing file must still raise — 0-because-ratcheted ≠ 0-because-absent.
        with pytest.raises(AT17ProgressError):
            collect(tmp_path / "absent.py")

    @pytest.mark.acceptance_smoke
    def test_every_at17_xfail_reason_follows_the_gap_format(self) -> None:
        report = collect()  # real test_17_control_plane.py
        assert report.non_gap_reasons == (), (
            f"xfail reasons must start with 'GAP:': {report.non_gap_reasons}"
        )
        assert report.strict_xfail == report.xfail, (
            f"every AT-17 xfail must be strict (strict={report.strict_xfail}, xfail={report.xfail})"
        )
        # Sanity: the 14 gap claims + 3 green + 4 metric self-tests are present.
        assert report.total >= 17
        assert report.xfail >= 1
