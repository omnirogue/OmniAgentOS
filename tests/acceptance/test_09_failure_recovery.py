"""AT-09 — Failure recovery: inject a fault, prove the policy handled it.

Five faults are injected through the reusable suite in
``tests/acceptance/failure_injection`` (which is runnable on its own and
self-tests that each injector really fires):

    timeout · tool_failure · failed_test · merge_conflict · rate_limit

The recovery contract being asserted, in the order the scheduler applies it:

1. **Retry.** A mechanical failure buys ONE free same-tier retry.
2. **Escalate.** Further failures walk ``TIER_LADDER`` (simple -> standard ->
   complex), consuming the retry budget as they go; a timeout escalates once and
   then splits the task instead.
3. **Park or fail.** When the ladder is spent the task is BLOCKED with a named,
   attributable reason. It is never quietly dropped and never marked done.
4. **Never silent success.** A fault always leaves a durable attempt record, and
   a task whose fault is permanent never reaches ``done`` on a clean record.

Also asserted, because a recovery that hangs is not a recovery: every injected
scenario reaches a terminal state within a bounded wall-clock budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.swarm.scheduler import DEFAULT_RETRY_CAP, TIER_LADDER
from tests.acceptance.failure_injection import (
    INJECTIONS,
    inject_real_merge_conflict,
    injection_names,
    run_injection,
)


@pytest.fixture(autouse=True)
def _pin_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


# ==========================================================================
# Invariants that must hold for EVERY injected fault
# ==========================================================================


class TestEveryFaultRecoversUnderPolicy:
    @pytest.mark.parametrize("name", injection_names())
    def test_recovery_terminates(self, name: str, tmp_path: Path) -> None:
        """A fault must never leave the run spinning. This is the anti-hang
        assertion: ``run_finished`` is False only if the run never settled."""
        result = run_injection(name, tmp_path)
        try:
            assert result.run_finished, f"{name}: recovery hung; the run never settled"
            run = result.harness.dal.get_run(result.harness.run_id) or {}
            assert str(run.get("status")) in {"completed", "failed", "cancelled"}, (
                f"{name}: run left in non-terminal status {run.get('status')!r}"
            )
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_fault_leaves_a_durable_attributable_record(self, name: str, tmp_path: Path) -> None:
        """Recovery is only auditable if the fault is written down. Every fault
        must leave an attempt row carrying its signature end_reason."""
        result = run_injection(name, tmp_path)
        try:
            expected = result.injection.expect.attempt_end_reasons
            assert set(result.end_reasons) & expected, (
                f"{name}: no durable record of the fault. "
                f"expected one of {sorted(expected)}, saw {result.end_reasons}"
            )
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_terminal_status_matches_the_declared_policy(self, name: str, tmp_path: Path) -> None:
        result = run_injection(name, tmp_path)
        try:
            assert result.target_status == result.injection.expect.terminal_status, (
                f"{name}: expected {result.injection.expect.terminal_status!r}, "
                f"got {result.target_status!r}"
            )
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_blocking_always_names_a_reason(self, name: str, tmp_path: Path) -> None:
        """A task that stops must say WHY. An unattributed block is
        indistinguishable from work silently going missing."""
        result = run_injection(name, tmp_path)
        try:
            expected_reason = result.injection.expect.block_reason
            if expected_reason is None:
                pytest.skip(f"{name} does not block its target by design")
            reasons = result.block_reasons(result.injection.target)
            assert expected_reason in reasons, (
                f"{name}: expected block reason {expected_reason!r}, saw {reasons}"
            )
            assert all(r and r != "None" for r in reasons), f"{name}: unnamed block reason"
        finally:
            result.close()

    @pytest.mark.parametrize("name", injection_names())
    def test_never_silent_success(self, name: str, tmp_path: Path) -> None:
        """The headline invariant.

        A permanent fault must never produce a clean success. For four of the
        five faults the task must not be ``done`` at all. ``merge_conflict`` is
        the deliberate exception — the unit's WORK really did complete and sits
        on its branch — so there the conflict itself must be durably recorded and
        surfaced, which is the opposite of silent.
        """
        result = run_injection(name, tmp_path)
        try:
            if name == "merge_conflict":
                assert result.swarm_json.get("merge_conflict") is True, (
                    "a merge conflict was swallowed: the task reported plain success"
                )
                assert "merge_conflict" in result.actions
            else:
                assert result.target_status != "done", (
                    f"{name}: a permanently failing task reported success"
                )
                assert "completed" not in result.end_reasons, (
                    f"{name}: a failing task recorded a completed attempt"
                )
        finally:
            result.close()


# ==========================================================================
# The escalation ladder
# ==========================================================================


class TestRetryThenEscalateThenBlock:
    @pytest.mark.parametrize("name", ["failed_test", "tool_failure"])
    def test_mechanical_faults_walk_the_full_tier_ladder(self, name: str, tmp_path: Path) -> None:
        """One free same-tier retry, then a rung per retry, then blocked.

        Asserting the exact tier sequence is what proves escalation happened —
        a task that merely retried four times at ``simple`` would satisfy a
        count-only assertion while never escalating at all.
        """
        result = run_injection(name, tmp_path)
        try:
            tiers = [str(a["tier"]) for a in result.target_attempts]
            assert tiers == ["simple", "simple", "standard", "complex"], (
                f"{name}: the escalation ladder was not walked: {tiers}"
            )
            assert list(TIER_LADDER) == ["simple", "standard", "complex"]
            assert result.swarm_json.get("mechanical_retry_used") is True, (
                "the free mechanical retry was never taken"
            )
            assert int(result.swarm_json.get("retries") or 0) == DEFAULT_RETRY_CAP + 1
            assert result.target_status == "blocked"
            assert "retry_cap" in result.block_reasons(result.injection.target)
        finally:
            result.close()

    @pytest.mark.parametrize("name", ["failed_test", "tool_failure"])
    def test_the_failure_detail_is_fed_back_to_the_next_attempt(
        self, name: str, tmp_path: Path
    ) -> None:
        """Escalation without the error text is just a re-roll. The worker must
        be told what went wrong."""
        result = run_injection(name, tmp_path)
        try:
            feedback = result.swarm_json.get("feedback") or []
            assert feedback, f"{name}: no feedback was recorded for the retry"
            sources = {str(entry.get("source")) for entry in feedback}
            assert sources & {"mechanical", "retry"}, f"{name}: feedback sources {sources}"
            assert any(str(entry.get("text")) for entry in feedback), (
                f"{name}: feedback entries carry no detail"
            )
        finally:
            result.close()

    def test_attempts_are_bounded_by_the_retry_cap(self, tmp_path: Path) -> None:
        """The ladder terminates. Without a cap the injected fault would retry
        forever, which is the exact regression this bound exists to prevent."""
        result = run_injection("failed_test", tmp_path)
        try:
            assert len(result.target_attempts) == DEFAULT_RETRY_CAP + 2, (
                f"attempts unbounded: {result.end_reasons}"
            )
        finally:
            result.close()


class TestTimeoutLadder:
    def test_timeout_escalates_once_then_splits_then_blocks(self, tmp_path: Path) -> None:
        """A timeout takes a DIFFERENT branch of the ladder: escalate, then split
        the task rather than burning the retry budget on the same shape."""
        result = run_injection("timeout", tmp_path)
        try:
            assert result.end_reasons == ["timeout", "split"], (
                f"the timeout ladder was not followed: {result.end_reasons}"
            )
            tiers = [str(a["tier"]) for a in result.target_attempts]
            assert tiers == ["simple", "standard"], f"the timeout did not escalate: {tiers}"
            assert int(result.swarm_json.get("timeout_count") or 0) == 2
            assert result.target_status == "blocked"
            assert "split_failed" in result.block_reasons(result.injection.target)
        finally:
            result.close()

    def test_timeout_does_not_consume_the_retry_budget(self, tmp_path: Path) -> None:
        """Timeouts are counted separately from retries; conflating them would
        silently shorten the retry ladder for slow work."""
        result = run_injection("timeout", tmp_path)
        try:
            assert int(result.swarm_json.get("retries") or 0) == 0
            assert int(result.swarm_json.get("timeout_count") or 0) > 0
        finally:
            result.close()

    def test_the_hung_session_is_actually_killed(self, tmp_path: Path) -> None:
        """Reaping the attempt is not enough — the runaway worker must be
        terminated, or the fleet leaks a live session per timeout."""
        result = run_injection("timeout", tmp_path)
        try:
            assert result.harness.world.kills, "a timed-out worker was never killed"
            live = [
                row for row in result.harness.world.sessions.values() if row["state"] == "running"
            ]
            assert not live, f"sessions left running after recovery: {live}"
        finally:
            result.close()


class TestRateLimitRecovery:
    def test_rate_limit_requeues_without_consuming_a_retry(self, tmp_path: Path) -> None:
        """A provider refusal is not the worker's fault, so it must not spend the
        retry budget or escalate the tier."""
        result = run_injection("rate_limit", tmp_path)
        try:
            assert int(result.swarm_json.get("retries") or 0) == 0, "a rate limit consumed a retry"
            tiers = {str(a["tier"]) for a in result.target_attempts}
            assert tiers == {"simple"}, f"a rate limit escalated the tier: {tiers}"
            assert int(result.swarm_json.get("rate_limit_requeues") or 0) > 0
        finally:
            result.close()

    def test_flapping_is_bounded_and_reported(self, tmp_path: Path) -> None:
        """Because it costs no retry, a rate limit needs its own cap or a
        permanently limited provider loops forever."""
        result = run_injection("rate_limit", tmp_path)
        try:
            assert len(result.target_attempts) == 3, (
                f"requeue cap of 2 did not bound the attempts: {result.end_reasons}"
            )
            assert result.target_status == "blocked"
            assert "rate_limit_flapping" in result.block_reasons(result.injection.target)
            assert result.harness.limits.reports, (
                "the rate limit was never reported to the cooldown ledger"
            )
        finally:
            result.close()

    def test_provider_switch_is_announced(self, tmp_path: Path) -> None:
        result = run_injection("rate_limit", tmp_path)
        try:
            assert "provider_switched" in result.actions
            assert "rate_limit" in result.actions
        finally:
            result.close()


class TestMergeConflictRecovery:
    def test_conflict_is_recorded_and_routed_not_swallowed(self, tmp_path: Path) -> None:
        result = run_injection("merge_conflict", tmp_path)
        try:
            assert result.swarm_json.get("merge_conflict") is True
            conflicts = result.harness.emitter.of("merge_conflict")
            assert conflicts, "the conflict was never announced"
            assert conflicts[0].get("branch"), "the conflict names no branch to recover"
        finally:
            result.close()

    def test_dependents_are_parked_with_a_distinct_reason(self, tmp_path: Path) -> None:
        """Work built on unmerged work must not proceed — and the reason must be
        distinguishable from the dependency's own failure."""
        result = run_injection("merge_conflict", tmp_path)
        try:
            expect = result.injection.expect
            assert expect.collateral_blocked is not None
            assert result.harness.status_of(expect.collateral_blocked) == "blocked"
            reasons = result.block_reasons(expect.collateral_blocked)
            assert expect.collateral_block_reason in reasons, (
                f"dependent parked for the wrong reason: {reasons}"
            )
            assert expect.collateral_blocked not in result.harness.world.spawn_order, (
                "a dependent of unmerged work was executed anyway"
            )
        finally:
            result.close()

    def test_integration_is_exempt_and_still_runs(self, tmp_path: Path) -> None:
        result = run_injection("merge_conflict", tmp_path)
        try:
            assert result.harness.status_of("integration") == "done", (
                "integration was blocked; nobody is left to resolve the conflict"
            )
            assert "integration" in result.harness.world.spawn_order
        finally:
            result.close()


class TestRealGitMergeConflictRecovery:
    """The scheduler-level conflict injection runs on ``FakeWorktrees``, which
    scripts the merge STATUS but does not model git's semantics. Every claim that
    WORK SURVIVES a conflict is therefore proven here, against real git.
    """

    def test_conflict_is_detected_and_names_the_conflicting_files(self, tmp_path: Path) -> None:
        conflict = inject_real_merge_conflict(tmp_path)

        assert conflict.outcome.status == "conflict", (
            f"a real conflicting merge reported {conflict.outcome.status!r}"
        )
        assert "shared.txt" in conflict.outcome.conflict_files
        assert conflict.outcome.detail, "the conflict carries no diagnostic detail"

    def test_the_main_workspace_is_left_pristine(self, tmp_path: Path) -> None:
        """A refused merge must not wedge the shared workspace — a lingering
        MERGE_HEAD would block every subsequent unit."""
        conflict = inject_real_merge_conflict(tmp_path)

        assert not conflict.worktrees.has_pending_merge(str(conflict.repo))
        text = (conflict.repo / "shared.txt").read_text(encoding="utf-8")
        assert "<<<<<<<" not in text, "conflict markers were committed into the workspace"
        assert text == "first wins\n"

    def test_the_conflicted_work_survives_terminal_cleanup(self, tmp_path: Path) -> None:
        """Recovery routes the conflict onward for manual merge, so the branch
        carrying that work must survive branch cleanup — ``branch -d`` refuses an
        unmerged branch, and this is the assertion that pins it."""
        conflict = inject_real_merge_conflict(tmp_path)
        # Terminal cleanup order, as the coordinator does it: worktrees are
        # removed first (a checked-out branch cannot be deleted at all), then
        # the run's branches are reclaimed.
        for path, _unit in conflict.worktrees.list_run_worktrees(
            str(conflict.repo), conflict.owner_id
        ):
            conflict.worktrees.remove(str(conflict.repo), path, salvage=False)

        deleted = conflict.worktrees.delete_run_branches(str(conflict.repo), conflict.owner_id)

        assert conflict.merged_branch in deleted, "cleanup did not reclaim merged branches"
        assert conflict.conflicted_branch not in deleted, (
            "the conflicted branch was deleted; the work is unrecoverable"
        )
        import subprocess

        surviving = subprocess.run(
            ("git", "-C", str(conflict.repo), "for-each-ref", "--format=%(refname:short)"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert conflict.conflicted_branch in surviving
        blob = subprocess.run(
            ("git", "-C", str(conflict.repo), "show", f"{conflict.conflicted_sha}:shared.txt"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert blob == "second wins\n", "the conflicted unit's work was lost"


# ==========================================================================
# Cross-cutting: the suite itself must not be able to pass vacuously
# ==========================================================================


class TestSuiteIntegrity:
    def test_every_registered_injection_is_exercised(self) -> None:
        """Guards against a fault being added to the registry but never asserted
        on, which would look like coverage while proving nothing."""
        exercised = set(injection_names())
        assert exercised == set(INJECTIONS)
        assert len(exercised) == 5, f"expected five injected faults, found {sorted(exercised)}"

    def test_faults_have_distinguishable_signatures(self) -> None:
        """If two faults shared a signature, a test could pass while the wrong
        one fired."""
        signatures = {
            name: injection.expect.attempt_end_reasons for name, injection in INJECTIONS.items()
        }
        # merge_conflict legitimately closes its attempt as "completed"; every
        # OTHER fault must be distinguishable from a clean run.
        for name, sig in signatures.items():
            if name == "merge_conflict":
                continue
            assert "completed" not in sig, f"{name} is indistinguishable from success"
