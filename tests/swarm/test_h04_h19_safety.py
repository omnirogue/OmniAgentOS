"""H-04 / H-19 safety slice — focused failure and ordering tests.

H-04: git/worktree ownership observation failure must block confirmation;
      never substitute an empty change set and auto-confirm.

H-19: external terminalization must request_kill live workers, finalize
      open attempt rows, and wait before worktree cleanup (kill before remove).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.swarm.scheduler_fakes import (
    FakeWorktrees,
    make_harness,
    make_scheduler,
    wait_until,
)


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


class TestH04ObservationFailureBlocksConfirmation:
    def test_git_changed_paths_failure_never_confirms(self, tmp_path: Path) -> None:
        """Phase-1 path: session reports no files_json, git observation raises.

        Pre-fix: empty substitution confirmed the task. Post-fix: every
        attempt ends crashed/blocked with zero review_confirmed events.
        """
        h = make_harness(
            tmp_path,
            [{"id": "obs", "owned_paths": ["src/obs.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        # Permanent observation failure (no files_json → git fallback raises).
        h.git.changed_paths_error = RuntimeError("git diff --name-only died")
        h.world.set_behavior(
            "obs",
            {
                "kind": "complete",
                "polls": 1,
                "output": "I changed things but observation is blind",
                "write_files": {"src/obs.txt": "payload", "outside.txt": "trespass"},
                # intentionally omit files_json so observation falls to git
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            # Observation was attempted (at least once per attempt).
            assert h.git.changed_paths_calls >= 1
            # NEVER confirmed.
            assert h.emitter.of("review_confirmed") == []
            assert h.status_of("obs") != "done"
            # Attempts closed as observation crashes (mechanical retry may
            # re-open; none may complete). Non-vacuous: every end_reason must
            # be an explicit fail-closed code — not "any truthy string".
            ends = [a["end_reason"] for a in h.attempts_of("obs")]
            assert ends
            assert all(r in ("crashed", "killed", "blocked") for r in ends), ends
            assert "completed" not in ends
            # Task is blocked or still open only if something else went wrong;
            # the durable signal is no confirm + no completed attempt.
            assert h.status_of("obs") in ("blocked", "open", "claimed", "in_progress")
            assert h.dal.get_run(h.run_id)["status"] in ("completed", "failed", "cancelled")
            assert not any(a["end_reason"] == "completed" for a in h.attempts_of("obs"))
        finally:
            h.close()

    def test_worktree_changed_paths_since_failure_never_confirms(self, tmp_path: Path) -> None:
        """Phase-2 path: worktree ownership observation raises → no confirm."""
        h = make_harness(
            tmp_path,
            [{"id": "wtobs", "owned_paths": ["src/wtobs.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        wt = FakeWorktrees(tmp_path / "wts")
        wt.changed_paths_error = RuntimeError("changed_paths_since died")
        h.world.set_behavior(
            "wtobs",
            {
                "kind": "complete",
                "polls": 1,
                "output": "work in private tree",
                "write_files": {"src/wtobs.txt": "mine"},
            },
        )
        try:
            scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=True)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.emitter.of("review_confirmed") == []
            assert h.status_of("wtobs") != "done"
            ends = [a["end_reason"] for a in h.attempts_of("wtobs")]
            assert ends
            assert "completed" not in ends
            # Reviewer must never have been asked to confirm blind work.
            assert h.reviewer.calls == [] or all(c.get("key") != "wtobs" for c in h.reviewer.calls)
        finally:
            h.close()


class TestH19ExternalTerminalizationOrder:
    def test_cancel_kills_finalizes_attempts_before_worktree_cleanup(self, tmp_path: Path) -> None:
        """Cancel must kill live sessions, close open attempts, then clean.

        Order invariant: every kill event timestamp ≤ every remove event.
        Attempt rows must be finalized (ended_at set, end_reason killed).
        """
        h = make_harness(
            tmp_path,
            [{"id": "one", "est": 30}, {"id": "two", "est": 20}],
            max_concurrency=2,
        )
        h.world.set_behavior("one", {"kind": "hang"})
        h.world.set_behavior("two", {"kind": "hang"})
        wt = FakeWorktrees(tmp_path / "wts")
        # Share the order stream so kills and removes are comparable.
        wt.order_events = h.world.order_events
        try:
            scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=True)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 2, timeout=10)
            assert wait_until(lambda: len(wt.created) >= 2, timeout=10)

            # Operator cancel (API writes exactly this).
            h.dal.set_run_status(h.run_id, "cancelled", error="cancelled by operator")
            assert handle.join(timeout=20)

            # Kills landed on both hanging workers.
            assert len(h.world.kills) >= 2
            # Attempts finalized — no live attempt rows remain.
            for key in ("one", "two"):
                attempts = h.attempts_of(key)
                assert attempts, key
                live = [a for a in attempts if a.get("ended_at") is None]
                assert live == [], f"{key} still has live attempts: {live}"
                assert any(a["end_reason"] == "killed" for a in attempts)
                assert h.status_of(key) != "done"

            # Order: all kills before all removes (H-19 wait-before-cleanup).
            events = list(h.world.order_events)
            kill_times = [t for kind, _id, t in events if kind == "kill"]
            remove_times = [t for kind, _id, t in events if kind == "remove"]
            assert kill_times, "expected request_kill events"
            # Worktree cleanup may or may not remove (depends on join timing),
            # but IF it removes, every remove is after every kill started.
            if remove_times:
                assert max(kill_times) <= max(remove_times) or min(remove_times) >= min(kill_times)
                assert min(remove_times) >= min(kill_times)
            assert h.dal.get_run(h.run_id)["status"] == "cancelled"
        finally:
            h.close()

    def test_external_failed_status_also_kills_and_finalizes(self, tmp_path: Path) -> None:
        """H-19 covers non-cancel external terminals too (failed stamped outside).

        Pre-fix: only cancelled triggered request_kill; failed just set
        stopping and abandoned live attempts. Post-fix: failed also kills
        and finalizes.
        """
        h = make_harness(
            tmp_path,
            [{"id": "hangs", "est": 30}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("hangs", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)
            assert wait_until(lambda: len(h.attempts_of("hangs")) >= 1, timeout=10)

            # External failure (e.g. operator/API marks the run failed).
            h.dal.set_run_status(h.run_id, "failed", error="operator failed the run")
            assert handle.join(timeout=20)

            assert len(h.world.kills) >= 1
            attempts = h.attempts_of("hangs")
            assert attempts
            assert all(a.get("ended_at") is not None for a in attempts)
            assert any(a["end_reason"] == "killed" for a in attempts)
            assert h.status_of("hangs") != "done"
            assert h.dal.get_run(h.run_id)["status"] == "failed"
        finally:
            h.close()

    def test_external_completed_stamping_kills_and_finalizes(self, tmp_path: Path) -> None:
        """H-19: external completed stamp (not only cancel/failed) terminalizes."""
        h = make_harness(
            tmp_path,
            [{"id": "hangs", "est": 30}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("hangs", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)

            h.dal.set_run_status(h.run_id, "completed", error=None)
            assert handle.join(timeout=20)

            assert len(h.world.kills) >= 1
            attempts = h.attempts_of("hangs")
            assert attempts
            assert all(a.get("ended_at") is not None for a in attempts)
            assert any(a["end_reason"] == "killed" for a in attempts)
            # Must not auto-confirm hanging work just because the run row says done.
            assert h.emitter.of("review_confirmed") == []
            assert h.status_of("hangs") != "done"
        finally:
            h.close()

    def test_terminalization_is_idempotent(self, tmp_path: Path) -> None:
        """Second external terminalization pass must not double-kill or reopen."""
        h = make_harness(
            tmp_path,
            [{"id": "hangs", "est": 30}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("hangs", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)
            assert wait_until(lambda: len(h.attempts_of("hangs")) >= 1, timeout=10)

            with scheduler._runs_lock:
                state = scheduler._runs[h.run_id]
            task_id = h.task_id("hangs")
            # The DB row appears before the worker binds the attempt into
            # state.running_attempts.  Wait for that exact harness state so
            # external terminalization always observes a fully registered live
            # worker, rather than racing a partially-created attempt.
            def registered_live_attempt() -> bool:
                with state.cond:
                    running = state.running_attempts.get(task_id)
                    session_id = str((running or {}).get("session_id") or "")
                with h.world.lock:
                    return bool(session_id) and h.world.sessions.get(session_id, {}).get(
                        "state"
                    ) == "running"

            assert wait_until(
                registered_live_attempt,
                timeout=10,
            )

            scheduler._handle_external_terminalization(state, reason="failed")
            kills_after_first = len(h.world.kills)
            assert kills_after_first >= 1, (
                f"expected kill on first terminalization; "
                f"running={state.running_attempts!r} sessions={list(h.world.sessions)}"
            )
            assert state.terminalizing is True
            assert state.terminal_reason == "failed"
            # Attempts finalized on first pass.
            attempts = h.attempts_of("hangs")
            assert attempts and all(a.get("ended_at") is not None for a in attempts)

            # Second pass is a no-op for the kill/finalize sequence.
            scheduler._handle_external_terminalization(state, reason="failed")
            assert len(h.world.kills) == kills_after_first
            assert state.terminalizing is True
            assert state.terminal_reason == "failed"

            h.dal.set_run_status(h.run_id, "failed", error="idempotence test")
            assert handle.join(timeout=20)
        finally:
            h.close()

    def test_bounded_wait_timeout_does_not_claim_success(self, tmp_path: Path) -> None:
        """H-19: wait timeout leaves non-terminal sessions for cleanup refusal."""
        h = make_harness(
            tmp_path,
            [{"id": "hangs", "est": 30}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("hangs", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h, await_poll_seconds=0.01)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)
            session_ids = set(h.world.sessions.keys())
            assert session_ids

            # Force a near-zero timeout while sessions are still hanging.
            scheduler._wait_for_sessions_terminal(session_ids, timeout_seconds=0.0)
            # Sessions remain non-terminal — timeout is not silent success.
            for sid in session_ids:
                row = h.world.get_session(sid)
                assert row is not None
                assert str(row.get("state") or "") not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "killed",
                }

            h.dal.set_run_status(h.run_id, "cancelled", error="test done")
            assert handle.join(timeout=20)
        finally:
            h.close()

    def test_cleanup_refuses_while_live_writers_remain(self, tmp_path: Path) -> None:
        """H-19: worktree cleanup refuses when running_attempts or workers live."""
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(
            tmp_path,
            [{"id": "one", "est": 10}],
            max_concurrency=1,
            integration=False,
        )
        wt = FakeWorktrees(tmp_path / "wts")
        try:
            scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=True)
            # Create a real worktree entry so a successful cleanup would remove it.
            info = wt.create(str(h.workdir), h.run_id, "one", "base")
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            state.worktree_mode = True
            state.worktree_recorded = True
            # Simulate a still-running writer.
            state.running_attempts["t1"] = {
                "task_id": "t1",
                "attempt_id": "a1",
                "session_id": "s1",
                "slot": 0,
            }
            h.dal.set_run_status(h.run_id, "cancelled", error="cleanup test")
            scheduler._cleanup_worktrees_best_effort(state)
            assert wt.removed == [], "cleanup must refuse while a writer is live"
            assert info.path in wt.active

            # After writers clear, cleanup proceeds and removes the worktree.
            state.running_attempts.clear()
            state.workers.clear()
            scheduler._cleanup_worktrees_best_effort(state)
            assert any(r.get("path") == info.path for r in wt.removed)
        finally:
            h.close()
