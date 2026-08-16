"""High-signal assertions for orchestration evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tests.simharness.runner import TERMINAL_RUN_STATUSES, ScenarioResult


def assert_escalated_model(attempts: Sequence[Mapping[str, Any]]) -> None:
    models = [str(row["model"]) for row in attempts if row.get("task_key") == "coder"]
    assert len(set(models)) >= 2, f"escalation did not advance model: attempts={models}"


def assert_concurrency_matches_plan(result: ScenarioResult) -> None:
    assert result.max_interval_overlap == result.target_concurrency, (
        "concurrency mismatch: "
        f"planned={result.target_concurrency}, "
        f"strict_interval_overlap={result.max_interval_overlap}"
    )


def assert_attempt_usage_complete(attempts: Sequence[Mapping[str, Any]]) -> None:
    missing = [
        {
            "task_key": row.get("task_key"),
            "seq": row.get("seq"),
            "cost_usd": row.get("cost_usd"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
        }
        for row in attempts
        if row.get("cost_usd") is None
        or row.get("input_tokens") is None
        or row.get("output_tokens") is None
    ]
    assert not missing, f"attempt usage incomplete: {missing}"


def assert_token_only_cost_unknown(attempts: Sequence[Mapping[str, Any]]) -> None:
    false_zeroes = [
        {
            "task_key": row.get("task_key"),
            "seq": row.get("seq"),
            "cost_usd": row.get("cost_usd"),
        }
        for row in attempts
        if row.get("input_tokens") is not None and row.get("cost_usd") == 0.0
    ]
    assert not false_zeroes, f"token-only provider persisted false cost: {false_zeroes}"


def assert_stale_run_recovered(
    run_id: str,
    sweep: Mapping[str, Any],
    status: str,
) -> None:
    resumed = [str(value) for value in sweep.get("resumed", [])]
    recovered = run_id in resumed or status in TERMINAL_RUN_STATUSES
    assert recovered, (
        "stale coordinator was neither adopted nor reaped: "
        f"status={status}, resumed={run_id in resumed}"
    )


def assert_terminal(status: str) -> None:
    assert status in TERMINAL_RUN_STATUSES, f"run ended non-terminal: status={status}"


def assert_zero_work_score(score: float, attempt_count: int) -> None:
    assert attempt_count == 0
    assert score <= 10.0, f"zero-work run scored too well: score={score}, attempts={attempt_count}"


def assert_operator_db_unchanged(
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
) -> None:
    after_value = dict(after or {})
    before_tables = dict(before.get("tables") or {})
    after_tables = dict(after_value.get("tables") or {})
    changed = {
        table: (before_tables.get(table, 0), after_tables.get(table, 0))
        for table in sorted(before_tables.keys() | after_tables.keys())
        if before_tables.get(table, 0) != after_tables.get(table, 0)
    }
    if before.get("exists") != after_value.get("exists"):
        changed["<database exists>"] = (
            int(bool(before.get("exists"))),
            int(bool(after_value.get("exists"))),
        )
    assert after == before, f"operator DB row counts changed: {changed}"


def assert_dependency_blocked_not_done(
    statuses: Mapping[str, str],
    *,
    failed_key: str,
    dependent_key: str,
) -> None:
    """A failed/blocked dep must park dependents — never silently mark them done."""
    failed_status = statuses.get(failed_key)
    dependent_status = statuses.get(dependent_key)
    assert failed_status in {"blocked", "cancelled"}, (
        f"upstream {failed_key!r} not terminal-failed: status={failed_status}"
    )
    assert dependent_status == "blocked", (
        f"dependent {dependent_key!r} should be blocked after dep failure, "
        f"got {dependent_status!r} (statuses={dict(statuses)})"
    )
    assert dependent_status != "done", (
        f"dependent {dependent_key!r} was silently marked done after dep failure"
    )


def assert_merge_conflict_routed_to_integration(
    integration_feedback: Sequence[Mapping[str, Any]],
    *,
    branch: str,
) -> None:
    conflicts = [
        entry
        for entry in integration_feedback
        if isinstance(entry, Mapping) and str(entry.get("source") or "") == "merge_conflict"
    ]
    assert conflicts, (
        f"no merge_conflict feedback on integration (feedback={list(integration_feedback)})"
    )
    assert any(
        branch in str(entry.get("branch") or entry.get("text") or "") for entry in conflicts
    ), f"conflict branch {branch!r} not routed to integration feedback: {conflicts}"


def assert_budget_blocked_admission(
    statuses: Mapping[str, str],
    *,
    budget_blocked_task_ids: Sequence[str],
    task_key_by_id: Mapping[str, str],
    cost_usd: float,
    budget_usd_max: float,
) -> None:
    """Budget was the attributed cause of blocked admission, not a side effect.

    Cross-checks the scheduler's ACTION_TASK_BLOCKED signal
    (``reason == "budget cap reached"``) against terminal task statuses and the
    recorded run cost. Unlike a filter-then-recheck of ``status == blocked``,
    this fails when tasks are blocked for any other reason (deps, stall, script
    exhaustion) or when enforcement never fired.
    """
    assert budget_blocked_task_ids, (
        "expected at least one ACTION_TASK_BLOCKED with reason 'budget cap reached'; "
        f"statuses={dict(statuses)} cost_usd={cost_usd} budget_usd_max={budget_usd_max}"
    )
    cause_keys = sorted(
        {
            str(task_key_by_id.get(task_id, task_id))
            for task_id in budget_blocked_task_ids
            if str(task_id).strip()
        }
    )
    outcome_keys = sorted(key for key, value in statuses.items() if value == "blocked")
    assert cause_keys == outcome_keys, (
        "budget block cause/outcome mismatch: "
        f"budget_event_keys={cause_keys}, status_blocked={outcome_keys}, "
        f"budget_task_ids={list(budget_blocked_task_ids)}, statuses={dict(statuses)}"
    )
    assert float(cost_usd) >= float(budget_usd_max), (
        f"run cost below budget cap (enforcement should not fire early): "
        f"cost_usd={cost_usd} budget_usd_max={budget_usd_max}"
    )


def assert_worktrees_removed(removed: Sequence[Mapping[str, Any]], *, min_count: int = 1) -> None:
    assert len(removed) >= min_count, (
        f"expected >= {min_count} worktree removals after task completion, got {list(removed)}"
    )


def assert_worktree_isolation(
    created: Sequence[Mapping[str, Any]],
    *,
    run_ids: Sequence[str],
) -> None:
    """Two runs must not share worktree paths or branch names."""
    assert len(run_ids) >= 2, "need at least two run ids"
    by_run: dict[str, list[Mapping[str, Any]]] = {run_id: [] for run_id in run_ids}
    for entry in created:
        path = str(entry.get("path") or "")
        branch = str(entry.get("branch") or "")
        matched = False
        for run_id in run_ids:
            if f"/{run_id}/" in path.replace("\\", "/") or branch.startswith(f"swarm/{run_id}/"):
                by_run[run_id].append(entry)
                matched = True
                break
        assert matched, f"worktree entry not namespaced to a known run: {entry}"

    for run_id, entries in by_run.items():
        assert entries, f"run {run_id} created no worktrees"

    paths = [str(entry.get("path") or "") for entry in created]
    branches = [str(entry.get("branch") or "") for entry in created]
    assert len(paths) == len(set(paths)), f"shared worktree paths across runs: {paths}"
    assert len(branches) == len(set(branches)), f"shared branch names across runs: {branches}"
    for entry in created:
        path = str(entry.get("path") or "").replace("\\", "/")
        branch = str(entry.get("branch") or "")
        task_key = str(entry.get("task_key") or "")
        assert any(f"/{run_id}/{task_key}" in path for run_id in run_ids), (
            f"path not shaped as <root>/<run_id>/<task_key>: {entry}"
        )
        assert any(branch == f"swarm/{run_id}/{task_key}" for run_id in run_ids), (
            f"branch not shaped as swarm/<run_id>/<key>: {entry}"
        )


def assert_timeout_escalated(attempts: Sequence[Mapping[str, Any]], *, task_key: str) -> None:
    task_attempts = [row for row in attempts if row.get("task_key") == task_key]
    timeout_rows = [row for row in task_attempts if row.get("end_reason") == "timeout"]
    assert timeout_rows, f"no timeout end_reason for {task_key!r}: attempts={list(task_attempts)}"
    tiers = [str(row.get("tier") or "") for row in task_attempts]
    assert len(set(tiers)) >= 2 or any(row.get("end_reason") == "split" for row in task_attempts), (
        f"timeout did not escalate tier for {task_key!r}: tiers={tiers}, attempts={task_attempts}"
    )


def assert_cancel_terminalizes_attempts(
    status: str,
    attempts: Sequence[Mapping[str, Any]],
) -> None:
    assert status == "cancelled", f"run status after cancel expected cancelled, got {status!r}"
    open_ended = [
        {
            "task_key": row.get("task_key"),
            "seq": row.get("seq"),
            "end_reason": row.get("end_reason"),
        }
        for row in attempts
        if row.get("end_reason") is None
    ]
    assert not open_ended, f"in-flight attempts left open after cancel: {open_ended}"
    assert attempts, "cancel scenario expected at least one attempt row"


def assert_split_children_aggregate(
    statuses: Mapping[str, str],
    *,
    parent_key: str,
    child_keys: Sequence[str],
) -> None:
    parent_status = statuses.get(parent_key)
    assert parent_status in {"cancelled", "blocked", "done"}, (
        f"split parent {parent_key!r} not terminal: {parent_status!r}"
    )
    for child_key in child_keys:
        matches = [key for key in statuses if key == child_key or key.startswith(f"{child_key}.")]
        # Children are registered as parent.1, parent.2, ...
        prefixed = [key for key in statuses if key.startswith(f"{parent_key}.")]
        assert prefixed or matches, (
            f"split children for {parent_key!r} missing from statuses={dict(statuses)}"
        )
        break
    child_statuses = {
        key: value for key, value in statuses.items() if key.startswith(f"{parent_key}.")
    }
    assert child_statuses, f"no split children under {parent_key!r}: {dict(statuses)}"
    assert all(status == "done" for status in child_statuses.values()), (
        f"split children did not complete: {child_statuses}"
    )


def assert_completed_tasks_preserved(
    statuses: Mapping[str, str],
    attempts: Sequence[Mapping[str, Any]],
    *,
    completed_key: str,
    expected_worked_keys: Sequence[str],
    scripts_remaining: int,
) -> None:
    """Resume/adoption must not re-run tasks already marked done.

    Preservation must be selective and the remaining work must actually run:
    a zero-work counterfeit (no attempts at all) must not pass just because
    the pre-completed key stayed ``done`` and had zero re-runs over an empty set.
    ``expected_worked_keys`` is an explicit literal from the caller — never
    derived from the observed attempt set.
    """
    assert statuses.get(completed_key) == "done", (
        f"pre-completed task {completed_key!r} lost done status: {dict(statuses)}"
    )
    re_runs = [row for row in attempts if row.get("task_key") == completed_key]
    assert not re_runs, f"pre-completed task {completed_key!r} was re-attempted: {re_runs}"

    expected = {str(key) for key in expected_worked_keys}
    assert expected, "expected_worked_keys must be a non-empty literal from the scenario"
    assert completed_key not in expected, (
        f"completed_key {completed_key!r} must not appear in expected_worked_keys={sorted(expected)}"
    )

    observed_worked = {
        str(row.get("task_key") or "")
        for row in attempts
        if row.get("task_key") is not None and str(row.get("task_key") or "")
    }
    assert observed_worked == expected, (
        "resume preservation must leave only the remaining tasks attempted: "
        f"expected_worked={sorted(expected)}, observed_attempted={sorted(observed_worked)}, "
        f"completed_key={completed_key!r}, attempts={list(attempts)}"
    )
    for key in sorted(expected):
        assert statuses.get(key) == "done", (
            f"remaining task {key!r} did not reach done after resume: "
            f"status={statuses.get(key)!r}, statuses={dict(statuses)}"
        )
    assert scripts_remaining == 0, (
        f"provider script not fully consumed (zero-work/skip risk): "
        f"scripts_remaining={scripts_remaining}"
    )


def assert_malformed_json_isolated(
    status: str,
    attempts: Sequence[Mapping[str, Any]],
) -> None:
    """Malformed provider payload must fail the attempt mechanically (crashed).

    A semantic denial (``review_denied``) is not enough: that can fire on empty
    output after a swallowed parse error, which is the counterfeit this
    scenario exists to catch. The run must still reach a terminal status.
    """
    assert_terminal(status)
    assert status == "completed", (
        f"malformed JSON recovery did not complete the run: status={status}"
    )
    assert attempts, "expected at least one attempt after malformed provider output"
    crashed = [row for row in attempts if row.get("end_reason") == "crashed"]
    assert crashed, (
        f"malformed JSON did not mechanically crash an attempt "
        f"(expected end_reason=='crashed', not review_denied/killed alone): "
        f"{list(attempts)}"
    )


def assert_concurrency_respects_ceiling(
    result: ScenarioResult,
    resize_targets: Sequence[int] | None = None,
) -> None:
    assert result.max_interval_overlap <= result.target_concurrency, (
        "concurrency ceiling breached: "
        f"strict_max_overlap={result.max_interval_overlap} > "
        f"target={result.target_concurrency}"
    )
    # Deterministic admission-control signal from ACTION_RESIZE events.
    # Overlap alone cannot prove "<= plan" (a barrier only proves ">= N").
    assert resize_targets is not None, (
        "resize_targets admission signal is required (do not call without it)"
    )
    assert resize_targets, (
        "expected ACTION_RESIZE admission signal; empty resize history cannot "
        "prove the plan concurrency ceiling (undefined rate over empty set)"
    )
    over = [level for level in resize_targets if int(level) > result.target_concurrency]
    assert not over, (
        "admission ceiling breached via resize: "
        f"levels={list(resize_targets)} exceeded "
        f"target_concurrency={result.target_concurrency}"
    )


def assert_run_stalled_terminal(status: str, error: str | None) -> None:
    assert status == "failed", f"stall guard should fail the run, got status={status!r}"
    assert error and "stalled" in error, f"stall guard diagnosis missing 'stalled': error={error!r}"


def assert_retry_cap_honoured(
    statuses: Mapping[str, str],
    attempts: Sequence[Mapping[str, Any]],
    *,
    task_key: str,
    retry_cap: int,
) -> None:
    assert statuses.get(task_key) == "blocked", (
        f"retry-cap task {task_key!r} should be blocked, got {statuses.get(task_key)!r}"
    )
    task_attempts = [row for row in attempts if row.get("task_key") == task_key]
    # mechanical free retry + (retry_cap + 1) consumed failures can produce more
    # rows; the hard bound is that we never retry forever.
    assert 1 <= len(task_attempts) <= retry_cap + 3, (
        f"retry cap not bounded for {task_key!r}: "
        f"attempts={len(task_attempts)} cap={retry_cap} rows={task_attempts}"
    )


def assert_end_reasons_recorded(attempts: Sequence[Mapping[str, Any]]) -> None:
    missing = [
        {"task_key": row.get("task_key"), "seq": row.get("seq")}
        for row in attempts
        if row.get("end_reason") is None
    ]
    assert not missing, f"terminal attempts missing end_reason: {missing}"


__all__ = [
    "assert_attempt_usage_complete",
    "assert_budget_blocked_admission",
    "assert_cancel_terminalizes_attempts",
    "assert_completed_tasks_preserved",
    "assert_concurrency_matches_plan",
    "assert_concurrency_respects_ceiling",
    "assert_dependency_blocked_not_done",
    "assert_end_reasons_recorded",
    "assert_escalated_model",
    "assert_malformed_json_isolated",
    "assert_merge_conflict_routed_to_integration",
    "assert_operator_db_unchanged",
    "assert_retry_cap_honoured",
    "assert_run_stalled_terminal",
    "assert_split_children_aggregate",
    "assert_stale_run_recovered",
    "assert_terminal",
    "assert_timeout_escalated",
    "assert_token_only_cost_unknown",
    "assert_worktree_isolation",
    "assert_worktrees_removed",
    "assert_zero_work_score",
]
