"""Deterministic API-to-provider orchestration mechanism simulations."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.provider_exec import SUPPORTED_PROVIDERS
from omniagentos.swarm.summary import compute_metrics
from tests.simharness import runner as runner_module
from tests.simharness.assertions import (
    assert_attempt_usage_complete,
    assert_budget_blocked_admission,
    assert_cancel_terminalizes_attempts,
    assert_completed_tasks_preserved,
    assert_concurrency_matches_plan,
    assert_concurrency_respects_ceiling,
    assert_dependency_blocked_not_done,
    assert_end_reasons_recorded,
    assert_escalated_model,
    assert_malformed_json_isolated,
    assert_merge_conflict_routed_to_integration,
    assert_operator_db_unchanged,
    assert_retry_cap_honoured,
    assert_run_stalled_terminal,
    assert_split_children_aggregate,
    assert_stale_run_recovered,
    assert_terminal,
    assert_timeout_escalated,
    assert_token_only_cost_unknown,
    assert_worktree_isolation,
    assert_worktrees_removed,
    assert_zero_work_score,
)
from tests.simharness.runner import (
    SimulationCampaign,
    dependency_tasks,
    escalation_tasks,
    provider_tasks,
    single_task,
    sqlite_row_counts,
    standard_tasks,
)
from tests.simharness.stub_provider import full_usage, hang, malformed_json, token_only
from tests.swarm.scheduler_fakes import wait_until


def test_simharness_isolation_env_keys_present() -> None:
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "isolated_env"
            for target in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Dict)
        keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert {
            "OMNIAGENTOS_DB",
            "OMNIAGENTOS_VAR",
            "OMNIAGENTOS_VAR_DIR",
        } <= keys
        return
    pytest.fail("SimulationCampaign.__enter__ isolated_env dict not found")


@pytest.mark.xfail(
    reason="complex-tier escalation clamps and re-resolves gpt-5.6-sol",
    strict=True,
)
def test_repeated_coder_failure_reaches_a_different_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        full_usage("REVIEW_DENY attempt 1"),
        full_usage("REVIEW_DENY attempt 2"),
        full_usage("coder complete"),
        full_usage("left complete"),
        full_usage("right complete"),
        full_usage("integration complete"),
    ]
    with SimulationCampaign(
        tmp_path / "escalation",
        monkeypatch,
        scenario="escalation",
    ) as sim:
        run_id = sim.dispatch(escalation_tasks(), results)
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_terminal(status)
        coder_attempts = [row for row in result.attempts if row["task_key"] == "coder"]
        assert len(coder_attempts) == 3
        assert_escalated_model(coder_attempts)


def test_planned_concurrency_usage_and_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "concurrency",
        monkeypatch,
        scenario="concurrency-usage-terminal",
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"attempt {index}") for index in range(4)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_terminal(status)
        assert_concurrency_matches_plan(result)
        assert_attempt_usage_complete(result.attempts)
        assert result.scripts_remaining == 0
        assert result.network_attempts == 0

    assert_operator_db_unchanged(sim.operator_before, sim.operator_after)


def test_every_wrapped_provider_uses_the_scripted_provider_exec_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "provider-matrix",
        monkeypatch,
        scenario="provider-matrix",
    ) as sim:
        run_id = sim.dispatch(
            provider_tasks(),
            [full_usage(f"provider attempt {index}") for index in range(6)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_terminal(status)
        assert {row["provider"] for row in result.attempts} == set(SUPPORTED_PROVIDERS)
        assert result.network_attempts == 0
        assert result.scripts_remaining == 0

    assert_operator_db_unchanged(sim.operator_before, sim.operator_after)


def test_token_only_provider_does_not_persist_false_zero_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "token-only",
        monkeypatch,
        scenario="token-only-cost",
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [token_only(f"token-only {index}") for index in range(4)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_terminal(status)
        assert all(row["input_tokens"] == 100 for row in result.attempts)
        assert_token_only_cost_unknown(result.attempts)


def test_stale_coordinator_is_adopted_and_terminalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "adoption",
        monkeypatch,
        scenario="stale-adoption",
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"adopted {index}") for index in range(4)],
            barrier_size=2,
            activate=False,
        )
        sweep = sim.adopt_stale(run_id)
        pre_join = sim.dal.get_run(run_id)
        assert pre_join is not None
        assert_stale_run_recovered(run_id, sweep, str(pre_join["status"]))
        status = sim.join(run_id)
        result = sim.result(run_id)
        evidence = result.normalized()
        evidence["resume_sweep"] = {
            "resumed": run_id in sweep["resumed"],
            "errors": list(sweep["errors"]),
        }
        sim.write_evidence(evidence)

        assert_terminal(status)
        assert result.network_attempts == 0

    assert_operator_db_unchanged(sim.operator_before, sim.operator_after)


def test_zero_work_run_does_not_score_well(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "zero-work",
        monkeypatch,
        scenario="zero-work-score",
    ) as sim:
        run_id = sim.dispatch(standard_tasks(), [], activate=False)
        sim.dal._connection.execute(
            "UPDATE swarm_runs "
            "SET status = 'completed', "
            "started_at = '2026-01-01T00:00:00Z', "
            "finished_at = '2026-01-01T00:01:00Z' "
            "WHERE id = ?",
            (run_id,),
        )
        sim.dal._connection.commit()
        metrics = compute_metrics(run_id, sim.dal)
        attempt_count = len(sim.dal.attempts_for_run(run_id))
        sim.write_evidence(
            {
                "scenario": sim.scenario,
                "status": "completed",
                "attempt_count": attempt_count,
                "score": metrics["score"],
                "network_attempts": len(sim.network_calls),
            }
        )

        assert_zero_work_score(float(metrics["score"]), attempt_count)


def test_inherited_operator_db_row_counts_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_db = tmp_path / "operator-control-plane.db"
    migrate(str(operator_db))
    operator_dal = SwarmDal(str(operator_db))
    try:
        operator_dal.create_run(working_dir="/operator/workspace", goal="operator sentinel", source="test")
    finally:
        operator_dal.close()
    before = sqlite_row_counts(operator_db)
    assert before["tables"]["swarm_runs"] == 1
    monkeypatch.setenv("OMNIAGENTOS_DB", str(operator_db))

    with SimulationCampaign(
        tmp_path / "isolation",
        monkeypatch,
        scenario="operator-db-isolation",
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"isolated {index}") for index in range(4)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "operator_swarm_runs_before": before["tables"]["swarm_runs"],
            }
        )
        assert_terminal(status)

    after = sqlite_row_counts(operator_db)
    assert_operator_db_unchanged(before, after)
    assert_operator_db_unchanged(sim.operator_before, sim.operator_after)
    # The roots mtime-manifest proof (campaign(manifest_operator_roots=True) +
    # sim.operator_roots_unchanged) is UNSOUND on a live operator machine:
    # launchd daemons write var/ between the before/after walks, so it false-
    # diffs by construction — and each walk costs minutes on a grown var/.
    # The pinned operator-DB row-count assertions above are the enforced
    # isolation proof; the manifest stays available for hermetic checkouts.
    assert os.environ["OMNIAGENTOS_DB"] == str(sim.db_path)


# ---------------------------------------------------------------------------
# Expanded scenarios (7 → 20)
# ---------------------------------------------------------------------------


def test_failed_dependency_blocks_dependents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked upstream task parks dependents — never silently marks them done."""
    # Solo parent→child: only parent is eligible; 3 REVIEW_DENY → retry_cap blocks
    # parent, then propagate_blocked parks child without ever spawning it.
    results = [
        full_usage("REVIEW_DENY parent 1"),
        full_usage("REVIEW_DENY parent 2"),
        full_usage("REVIEW_DENY parent 3"),
    ]
    with SimulationCampaign(
        tmp_path / "dep-blocked",
        monkeypatch,
        scenario="dep-blocked",
        target_cap=1,
        retry_cap=2,
    ) as sim:
        run_id = sim.dispatch(dependency_tasks(swarm=False), results)
        status = sim.join(run_id)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
            }
        )

        assert_terminal(status)
        assert_dependency_blocked_not_done(statuses, failed_key="parent", dependent_key="child")
        assert result.network_attempts == 0


def test_merge_conflict_routes_to_integration_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "merge-conflict",
        monkeypatch,
        scenario="merge-conflict",
        worktrees_enabled=True,
        target_cap=2,
    ) as sim:
        sim.worktrees.set_merge("parent", "conflict")
        # parent + sibling complete (child blocked by conflict park); integration runs.
        results = [
            full_usage("parent complete"),
            full_usage("sibling complete"),
            full_usage("integration resolves conflict"),
        ]
        run_id = sim.dispatch(dependency_tasks(swarm=True), results)
        status = sim.join(run_id)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        integration_json = sim.swarm_json_for(run_id, "integration")
        feedback = list(integration_json.get("feedback") or [])
        branch = f"swarm/{run_id}/parent"
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
                "integration_feedback": feedback,
                "worktree_merges": list(sim.worktrees.merges),
            }
        )

        assert_terminal(status)
        assert statuses.get("parent") == "done"
        assert statuses.get("child") == "blocked"
        assert_merge_conflict_routed_to_integration(feedback, branch=branch)
        assert result.network_attempts == 0


def test_budget_enforcement_blocks_admission_after_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "budget-block",
        monkeypatch,
        scenario="budget-block",
        budget_enforcement_block=True,
        target_cap=1,
    ) as sim:
        # Each full_usage costs 0.25; budget 0.3 lets one attempt land then blocks.
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"budget {index}") for index in range(4)],
            budget_usd_max=0.3,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        run_row = sim.dal.get_run(run_id) or {}
        budget_blocked_ids = sim.budget_cap_blocked_task_ids(run_id)
        key_by_id = sim.task_key_by_id(run_id)
        blocked = [key for key, value in statuses.items() if value == "blocked"]
        capacity_loss_events = [
            event
            for event in sim.dal.events_for_run(run_id, actions=("resize",))
            if str((event.get("payload") or {}).get("reason") or "").startswith(
                "worker_capacity_loss:"
            )
        ]
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
                "budget_usd_max": run_row.get("budget_usd_max"),
                "cost_usd": run_row.get("cost_usd"),
                "blocked": blocked,
                "budget_blocked_task_ids": budget_blocked_ids,
                "capacity_loss_events": capacity_loss_events,
            }
        )

        # Partial success with remaining work budget-blocked → completed, not
        # failed/cancelled. Pinning the status stops unrelated terminal paths
        # (stall, script exhaustion) from satisfying the scenario.
        assert status == "completed", (
            f"budget-blocked partial run should complete, got status={status!r} statuses={statuses}"
        )
        assert result.target_concurrency == 1, (
            "the budget admission scenario must exercise its declared one-slot "
            f"ceiling, got target_concurrency={result.target_concurrency}"
        )
        assert_budget_blocked_admission(
            statuses,
            budget_blocked_task_ids=budget_blocked_ids,
            task_key_by_id=key_by_id,
            cost_usd=float(run_row.get("cost_usd") or 0.0),
            budget_usd_max=float(run_row.get("budget_usd_max") or 0.0),
        )
        assert capacity_loss_events == [], (
            "an intentional budget-stop worker exit must not be reported as "
            f"capacity loss: {capacity_loss_events}"
        )
        assert result.network_attempts == 0


def test_worktree_removed_after_task_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "worktree-gc",
        monkeypatch,
        scenario="worktree-gc",
        worktrees_enabled=True,
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"wt {index}") for index in range(4)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "worktrees_created": list(sim.worktrees.created),
                "worktrees_removed": list(sim.worktrees.removed),
            }
        )

        assert_terminal(status)
        assert_worktrees_removed(sim.worktrees.removed, min_count=1)
        assert result.network_attempts == 0


def test_two_runs_do_not_share_worktree_paths_or_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "worktree-isolation",
        monkeypatch,
        scenario="worktree-isolation",
        worktrees_enabled=True,
    ) as sim:
        run_a = sim.dispatch(
            standard_tasks(),
            [full_usage(f"run-a {index}") for index in range(4)],
            barrier_size=2,
        )
        status_a = sim.join(run_a)
        run_b = sim.dispatch(
            standard_tasks(),
            [full_usage(f"run-b {index}") for index in range(4)],
            barrier_size=2,
        )
        status_b = sim.join(run_b)
        result_b = sim.result(run_b)
        sim.write_evidence(
            {
                "scenario": sim.scenario,
                "run_a": run_a,
                "run_b": run_b,
                "status_a": status_a,
                "status_b": status_b,
                "worktrees_created": list(sim.worktrees.created),
                "network_attempts": result_b.network_attempts,
            }
        )

        assert_terminal(status_a)
        assert_terminal(status_b)
        assert_worktree_isolation(sim.worktrees.created, run_ids=[run_a, run_b])
        assert result_b.network_attempts == 0


def test_attempt_timeout_is_closed_and_escalated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tiny tier timeout: hang processes are killed + escalated without wall waits.
    tiny = {"simple": 0.001, "standard": 0.001, "complex": 0.001, "max": 0.001}
    with SimulationCampaign(
        tmp_path / "timeout-escalate",
        monkeypatch,
        scenario="timeout-escalate",
        timeout_minutes=tiny,
        target_cap=1,
    ) as sim:
        # first hang → timeout escalate; second succeeds on next tier.
        run_id = sim.dispatch(
            single_task(task_id="slow"),
            [hang("timeout me"), full_usage("recovered")],
        )
        status = sim.join(run_id, timeout=15.0)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_terminal(status)
        assert_timeout_escalated(result.attempts, task_key="slow")
        assert result.network_attempts == 0


def test_cancel_terminalizes_in_flight_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "cancel-inflight",
        monkeypatch,
        scenario="cancel-inflight",
        timeout_minutes={
            "simple": 30.0,
            "standard": 30.0,
            "complex": 30.0,
            "max": 30.0,
        },
        target_cap=1,
    ) as sim:
        run_id = sim.dispatch(single_task(task_id="hanging"), [hang("cancel me")])
        assert wait_until(
            lambda: any(row.get("end_reason") is None for row in sim.dal.attempts_for_run(run_id)),
            timeout=5.0,
        )
        sim.cancel(run_id)
        status = sim.join(run_id, timeout=15.0)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_cancel_terminalizes_attempts(status, result.attempts)
        assert result.network_attempts == 0


def test_split_parent_children_aggregate_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def splitter(task: dict, swarm_json: dict) -> list[dict]:
        del task
        parent_key = str(swarm_json.get("task_key") or "solo")
        owned = list(swarm_json.get("owned_paths") or [f"src/{parent_key}.py"])
        return [
            {
                "title": f"{parent_key} part 1",
                "description": "first half",
                "owned_paths": [owned[0] if owned else f"src/{parent_key}_a.py"],
                "est_agent_minutes": 5,
                "complexity": "simple",
                "acceptance": "part 1 done",
            },
            {
                "title": f"{parent_key} part 2",
                "description": "second half",
                "owned_paths": [f"src/{parent_key}_b.py"],
                "est_agent_minutes": 5,
                "complexity": "simple",
                "acceptance": "part 2 done",
            },
        ]

    tiny = {"simple": 0.001, "standard": 0.001, "complex": 0.001, "max": 0.001}
    with SimulationCampaign(
        tmp_path / "split-aggregate",
        monkeypatch,
        scenario="split-aggregate",
        timeout_minutes=tiny,
        splitter=splitter,
        target_cap=2,
    ) as sim:
        # Two hangs → second timeout splits; two children complete.
        run_id = sim.dispatch(
            single_task(task_id="chunky"),
            [hang("t1"), hang("t2"), full_usage("child1"), full_usage("child2")],
        )
        status = sim.join(run_id, timeout=15.0)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
            }
        )

        assert_terminal(status)
        assert_split_children_aggregate(
            statuses, parent_key="chunky", child_keys=["chunky.1", "chunky.2"]
        )
        assert result.network_attempts == 0


def test_resume_preserves_already_completed_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No replan concept in swarm/; adoption/resume must not re-run completed work."""
    with SimulationCampaign(
        tmp_path / "resume-preserve",
        monkeypatch,
        scenario="resume-preserve",
    ) as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"resume {index}") for index in range(3)],
            activate=False,
        )
        # Mark alpha done before the coordinator ever runs.
        for task in sim.dal.tasks_for_run(run_id):
            swarm_json = sim.dal.get_swarm_json(str(task["id"])) or {}
            if str(swarm_json.get("task_key") or "") == "alpha":
                sim.collab.update_board_task(str(task["id"]), {"status": "done"})
                break
        else:
            raise AssertionError("alpha task not provisioned")

        sweep = sim.adopt_stale(run_id)
        status = sim.join(run_id)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
                "resumed": run_id in sweep.get("resumed", []),
            }
        )

        assert_terminal(status)
        assert_completed_tasks_preserved(
            statuses,
            result.attempts,
            completed_key="alpha",
            expected_worked_keys=("beta", "gamma", "integration"),
            scripts_remaining=result.scripts_remaining,
        )
        assert result.network_attempts == 0


def test_malformed_provider_json_does_not_kill_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "malformed-json",
        monkeypatch,
        scenario="malformed-json",
        target_cap=1,
    ) as sim:
        # Malformed first attempt → mechanical retry → success for solo task.
        run_id = sim.dispatch(
            single_task(task_id="fragile"),
            [malformed_json("NOT_JSON{{{"), full_usage("recovered")],
        )
        status = sim.join(run_id)
        result = sim.result(run_id)
        sim.write_evidence(result.normalized())

        assert_malformed_json_isolated(status, result.attempts)
        # Negative control: the same mechanical-attempt evidence cannot turn a
        # failed run into a successful recovery merely by being terminal.
        with pytest.raises(AssertionError, match="did not complete the run"):
            assert_malformed_json_isolated("failed", result.attempts)
        assert result.network_attempts == 0


def test_concurrency_never_exceeds_plan_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "concurrency-ceiling",
        monkeypatch,
        scenario="concurrency-ceiling",
    ) as sim:
        # Provision without activating so we can bind max_concurrency to the
        # plan target before the coordinator seeds/resizes (recompute uses
        # _run_cap → max_concurrency; without the bind, demand-driven growth
        # past target_concurrency is legal and would make the plan-ceiling
        # assertion untestable).
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"ceiling {index}") for index in range(4)],
            barrier_size=2,
            activate=False,
        )
        sim.pin_run_cap_to_plan(run_id)
        sim.start(run_id)
        status = sim.join(run_id)
        result = sim.result(run_id)
        resize_levels = sim.resize_targets(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "resize_targets": resize_levels,
            }
        )

        assert_terminal(status)
        assert_concurrency_respects_ceiling(result, resize_levels)
        assert result.network_attempts == 0


def test_run_with_no_eligible_tasks_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SimulationCampaign(
        tmp_path / "stall-guard",
        monkeypatch,
        scenario="stall-guard",
        stall_minutes=0.05,  # 3s of coordinator idle with real clock polls
        target_cap=1,
    ) as sim:
        run_id = sim.dispatch(standard_tasks(), [], activate=False)
        # Nothing can ever claim — stall guard must fail the run, not spin.
        sim.scheduler._try_claim = (  # type: ignore[method-assign]
            lambda state, index, worker_id: None
        )
        sim.start(run_id)
        status = sim.join(run_id, timeout=15.0)
        run_row = sim.dal.get_run(run_id) or {}
        sim.write_evidence(
            {
                "scenario": sim.scenario,
                "status": status,
                "error": run_row.get("error"),
                "network_attempts": len(sim.network_calls),
            }
        )

        assert_run_stalled_terminal(status, run_row.get("error"))
        assert len(sim.network_calls) == 0


def test_retry_cap_blocks_persistent_denials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choice property: review DENY past retry_cap blocks the task (no false completion)."""
    with SimulationCampaign(
        tmp_path / "retry-cap",
        monkeypatch,
        scenario="retry-cap",
        retry_cap=2,
        target_cap=1,
    ) as sim:
        results = [
            full_usage("REVIEW_DENY 1"),
            full_usage("REVIEW_DENY 2"),
            full_usage("REVIEW_DENY 3"),
        ]
        run_id = sim.dispatch(single_task(task_id="stubborn"), results)
        status = sim.join(run_id)
        result = sim.result(run_id)
        statuses = sim.task_statuses(run_id)
        sim.write_evidence(
            {
                **result.normalized(),
                "task_statuses": statuses,
            }
        )

        assert_terminal(status)
        assert_retry_cap_honoured(statuses, result.attempts, task_key="stubborn", retry_cap=2)
        assert_end_reasons_recorded(result.attempts)
        assert result.network_attempts == 0
