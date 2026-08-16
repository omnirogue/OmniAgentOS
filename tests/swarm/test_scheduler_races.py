"""WP5a race-condition drills — the verification gate for the scheduler.

Every named race from the plan doc: CAS claim contention, resize up/down with
no shrink-kill, atomic task-split rewiring, all-cooling park (no hot loop,
fake clock), parked-approval slot recovery, reviewer-infra-failure isolation,
crash + resume (heartbeat lease, no duplicate attempts, claim sweep), cancel
mid-run (no new spawns, running killed, late completion ignored), rate-limit
re-enqueue without retry + flap cap, budget signal ordering."""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.scheduler import SwarmScheduler, build_worker_brief
from tests.swarm.scheduler_fakes import (
    FakeWorktrees,
    make_harness,
    make_scheduler,
    wait_until,
)


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


class TestClaimContention:
    def test_cas_claim_two_workers_one_task_exactly_one_wins(self, tmp_path: Path) -> None:
        """The collab claim_version CAS IS the claim: two racing claimants,
        exactly one winner."""
        h = make_harness(tmp_path, [{"id": "solo"}], max_concurrency=2)
        task_id = h.task_id("solo")
        barrier = threading.Barrier(2)
        results: list[bool] = []
        lock = threading.Lock()

        def contender(name: str) -> None:
            barrier.wait()
            won = h.collab.claim_task(task_id, name, 0)
            with lock:
                results.append(won)

        try:
            threads = [threading.Thread(target=contender, args=(f"w{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert sorted(results) == [False, True]
            row = h.task_row("solo")
            assert row["status"] == "claimed"
            assert int(row["claim_version"]) == 1
        finally:
            h.close()

    def test_concurrent_workers_never_double_attempt(self, tmp_path: Path) -> None:
        """Three workers racing the same ordered candidate list: every task
        ends with exactly ONE attempt (CAS contention resolved cleanly)."""
        h = make_harness(
            tmp_path,
            [{"id": "p1", "est": 30}, {"id": "p2", "est": 20}, {"id": "p3", "est": 10}],
            max_concurrency=3,
            target_n=3,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            for key in ("p1", "p2", "p3", "integration"):
                assert len(h.attempts_of(key)) == 1, key
                assert h.status_of(key) == "done"
        finally:
            h.close()


class TestResize:
    def test_resize_up_and_down_counter_never_exceeds_target_no_shrink_kill(
        self, tmp_path: Path
    ) -> None:
        gates = {key: threading.Event() for key in ("g1", "g2", "g3", "g4")}
        h = make_harness(
            tmp_path,
            [
                {"id": "g1", "est": 40},
                {"id": "g2", "est": 30},
                {"id": "g3", "est": 20},
                {"id": "g4", "est": 10},
            ],
            max_concurrency=10,
            target_n=1,
        )
        for key, gate in gates.items():
            h.world.set_behavior(key, {"kind": "gated", "gate": gate, "polls": 1})
        h.limits.capacity = 1  # fair_share pins target_n to 1
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active == 1, timeout=10)
            time.sleep(0.2)  # several reconciles at capacity 1
            assert h.world.active == 1  # counter never exceeded target 1

            # GROW (eager): capacity 5 → fair_share 5, demand 4 → target 4.
            h.limits.capacity = 5
            assert wait_until(lambda: h.world.active == 4, timeout=10)
            assert any(e["from"] < e["to"] and e["to"] == 4 for e in h.emitter.of("resize"))
            assert h.world.high_water <= 4

            # SHRINK (lazy): capacity 2 with 4 gated attempts running — the
            # resize event fires, but NO running attempt is killed.
            h.limits.capacity = 2
            assert wait_until(
                lambda: any(e["to"] == 2 and e["from"] == 4 for e in h.emitter.of("resize")),
                timeout=10,
            )
            assert h.world.kills == []  # shrink never kills
            assert h.world.active == 4  # all four still running

            spawns_at_shrink = len(h.world.spawn_order)
            for gate in gates.values():
                gate.set()
            assert handle.join(timeout=20)

            # Post-shrink spawns (integration) respected the shrunken counter.
            for width in h.world.active_at_spawn[spawns_at_shrink:]:
                assert width <= 2
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # Re-contract (L10/M-34): demand-driven resize events still carry
            # fair_share/run_cap diagnostics. Capacity-loss resize events are a
            # SEPARATE signal (dead worker slots) and must not be forced into
            # the fair_share shape — that would hide real capacity deaths or
            # require false-positive capacity events to fake fair_share text.
            # Safety is preserved: demand resizes still assert their contract;
            # capacity-loss (if any) must name dead slots explicitly.
            for event in h.emitter.of("resize"):
                reason = str(event.get("reason") or "")
                if "worker_capacity_loss" in reason:
                    assert "dead slot" in reason
                    assert "dead_slots" in event
                    continue
                assert "fair_share=" in reason and "run_cap=" in reason, reason
        finally:
            h.close()


class TestTaskSplit:
    def test_dal_split_rewires_atomically_and_rolls_back(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [
                {"id": "pre"},
                {"id": "parent", "depends_on": ["pre"]},
                {"id": "child", "depends_on": ["parent"]},
            ],
            max_concurrency=1,
        )
        parent_id = h.task_id("parent")
        child_id = h.task_id("child")
        pre_id = h.task_id("pre")
        try:
            # Oversized split → ValueError → NOTHING changed (rollback).
            with pytest.raises(ValueError):
                h.dal.split_task(h.run_id, parent_id, [{"title": f"s{i}"} for i in range(5)])
            deps = h.dal.deps_for_run(h.run_id)
            assert any(
                d["task_id"] == child_id and d["depends_on_task_id"] == parent_id for d in deps
            )

            result = h.dal.split_task(
                h.run_id,
                parent_id,
                [
                    {"title": "part 1", "swarm_json": {"task_key": "parent.1"}},
                    {"title": "part 2", "swarm_json": {"task_key": "parent.2"}},
                ],
            )
            sub_ids = result["subtask_ids"]
            assert len(sub_ids) == 2
            deps = h.dal.deps_for_run(h.run_id)
            # Subtasks INHERIT the parent's deps.
            for sub in sub_ids:
                assert any(d["task_id"] == sub and d["depends_on_task_id"] == pre_id for d in deps)
            # The dependent re-points to ALL subtasks and drops the parent edge.
            child_deps = {d["depends_on_task_id"] for d in deps if d["task_id"] == child_id}
            assert child_deps == set(sub_ids)
            assert not any(parent_id in (d["task_id"], d["depends_on_task_id"]) for d in deps)
            # Parent closes split.
            parent = h.task_row("parent")
            assert parent["status"] == "cancelled"
            assert parent["claimed_by"] is None
            parent_json = h.dal.get_swarm_json(parent_id)
            assert parent_json["split"] is True
            assert parent_json["subtask_ids"] == sub_ids

            # Splitting a terminal task refuses.
            with pytest.raises(ValueError):
                h.dal.split_task(h.run_id, parent_id, [{"title": "again"}])
        finally:
            h.close()

    def test_double_timeout_triggers_split_and_subtasks_execute(self, tmp_path: Path) -> None:
        """Fake clock: first timeout kills + escalates a tier; the second
        splits (≤4 subtasks), dependents rewire, and the run completes over
        the subtasks."""
        h = make_harness(
            tmp_path,
            [
                {"id": "big", "complexity": "simple", "owned_paths": ["src/a.py", "src/b.py"]},
                {"id": "after", "depends_on": ["big"]},
            ],
            max_concurrency=1,
            fake_clock=True,
        )
        h.world.set_behavior("big", {"kind": "hang"})

        def splitter(task: Any, swarm_json: Any) -> list[dict[str, Any]]:
            del task, swarm_json
            return [
                {"title": "big part 1", "description": "half", "owned_paths": ["src/a.py"]},
                {"title": "big part 2", "description": "half", "owned_paths": ["src/b.py"]},
            ]

        try:
            scheduler = make_scheduler(h, splitter=splitter, await_poll_seconds=5.0)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            attempts = h.attempts_of("big")
            assert [a["end_reason"] for a in attempts] == ["timeout", "split"]
            assert [a["tier"] for a in attempts] == ["simple", "standard"]  # escalated once
            assert h.status_of("big") == "cancelled"
            assert h.swarm_json_of("big").get("split") is True

            split_events = h.emitter.of("task_split")
            assert len(split_events) == 1
            sub_ids = split_events[0]["subtask_ids"]
            assert len(sub_ids) == 2

            # "after" was rewired onto the subtasks and executed after them.
            deps = h.dal.deps_for_run(h.run_id)
            after_deps = {
                d["depends_on_task_id"] for d in deps if d["task_id"] == h.task_id("after")
            }
            assert after_deps == set(sub_ids)
            order = h.world.spawn_order
            assert order.index("after") > max(order.index("big.1"), order.index("big.2"))
            statuses = {str(t["id"]): str(t["status"]) for t in h.dal.tasks_for_run(h.run_id)}
            assert all(statuses[sub] == "done" for sub in sub_ids)
            assert h.status_of("after") == "done"
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # The split parent is NOT a partial-run signal.
            assert h.emitter.of("run_completed")[0]["partial"] is False
        finally:
            h.close()


class TestAllCooling:
    def test_all_cooling_parks_with_stall_event_and_no_hot_loop(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "c1", "est": 20}, {"id": "c2", "est": 10}],
            max_concurrency=2,
            fake_clock=True,
        )
        h.router.cooling = True
        h.limits.cooldown_until = h.clock.now() + timedelta(seconds=120)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None

            assert wait_until(lambda: len(h.emitter.of("rate_limit_stall")) >= 1, timeout=10)
            stall = h.emitter.of("rate_limit_stall")[0]
            assert 0 < stall["seconds"] <= 15 * 60  # bounded backoff, 15 min cap

            # NO HOT LOOP: while parked, the router is not being hammered.
            with scheduler._runs_lock:
                state = scheduler._runs[h.run_id]
            # Capture a baseline poll-pass count and wait for a DELTA of
            # completed poll passes from here, not an absolute threshold: the
            # worker polls continuously from run start, so an absolute
            # `>= 2` check can already be satisfied by the time we get here,
            # collapsing the observation window to ~zero and making the
            # no-hot-loop assertion below vacuously true.  The fake clock
            # only advances when the test directs it, so the parked deadline
            # cannot elapse during this observation window regardless.
            poll_baseline = state.worker_poll_pass
            calls_at_park = h.router.calls
            assert wait_until(
                lambda: state.worker_poll_pass >= poll_baseline + 2, timeout=10
            )
            assert h.router.calls - calls_at_park <= 2
            assert h.world.spawn_order == []  # nothing spawned while cooling

            # Cooldown lapses → the run resumes on its own park expiry.
            h.router.cooling = False
            h.clock.advance(200)  # type: ignore[attr-defined]
            assert handle.join(timeout=20)
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # Never spun: a bounded number of park entries, not thousands.
            assert len(h.emitter.of("rate_limit_stall")) <= 3
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "completed"  # never failed on cooldowns alone
        finally:
            h.close()


class TestApprovalPark:
    def test_parked_approval_releases_slot_and_resolution_reenqueues(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "appr", "est": 30}, {"id": "other", "est": 10}],
            max_concurrency=1,
        )
        h.world.set_behavior("appr", {"kind": "await_approval", "polls": 1})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None

            # appr (longer critical path) runs first and parks…
            assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 1, timeout=10)
            appr_session = h.emitter.of("approval_parked")[0]["session_id"]

            # …and the SLOT RECOVERS: "other" spawns while appr still awaits.
            assert wait_until(lambda: "other" in h.world.spawn_order, timeout=10)
            assert h.world.sessions[appr_session]["state"] == "awaiting_approval"

            # Approval resolution re-enqueues the parked task (fallback poll).
            h.world.resolve(appr_session, "completed", output="approved work")
            assert handle.join(timeout=20)

            assert h.status_of("appr") == "done"
            assert h.status_of("other") == "done"
            assert len(h.attempts_of("appr")) == 1  # the SAME attempt, re-attached
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()


class TestReviewerInfraFailure:
    def test_infra_failure_never_auto_confirms_and_consumes_no_retry(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "rev"}], max_concurrency=1)
        h.reviewer.set_script("rev", "error", "error")
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            # Retried once, then blocked-on-review. NOT a deny, NOT a confirm.
            assert len([c for c in h.reviewer.calls if c["key"] == "rev"]) == 2
            assert h.status_of("rev") == "blocked"
            assert int(h.swarm_json_of("rev").get("retries") or 0) == 0  # no retry consumed
            attempts = h.attempts_of("rev")
            assert len(attempts) == 1
            assert attempts[0]["end_reason"] == "blocked"
            assert "reviewer infrastructure" in attempts[0]["detail"]
            # Never auto-confirmed: no commit, no review_confirmed, not done.
            assert h.emitter.of("review_confirmed") == [
                e for e in h.emitter.of("review_confirmed") if e["task_id"] != h.task_id("rev")
            ]
            assert not any(
                e.get("task_id") == h.task_id("rev") for e in h.emitter.of("review_denied")
            )
            blocked = h.emitter.of("task_blocked")
            assert any(e.get("reason") == "reviewer_infrastructure" for e in blocked)
        finally:
            h.close()

    def test_single_infra_failure_retries_reviewer_then_confirms(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "rev2"}], max_concurrency=1)
        h.reviewer.set_script("rev2", "error", "confirm")
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert len([c for c in h.reviewer.calls if c["key"] == "rev2"]) == 2
            assert h.status_of("rev2") == "done"
            assert int(h.swarm_json_of("rev2").get("retries") or 0) == 0
            assert len(h.attempts_of("rev2")) == 1
        finally:
            h.close()


class TestCrashResume:
    def _stale_heartbeat(self, h: Any) -> None:
        h.dal._connection.execute(
            "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (h.run_id,),
        )

    def test_resume_sweeps_claims_attaches_attempts_no_duplicates(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "dead", "est": 30}, {"id": "live", "est": 20}, {"id": "fresh", "est": 10}],
            max_concurrency=2,
        )
        try:
            # Simulate a coordinator that died mid-run:
            assert h.dal.try_activate_run(h.run_id)
            # 1) "dead" was claimed but its attempt never opened (claim lease
            #    must be swept);
            assert h.collab.claim_task(h.task_id("dead"), "swarm-dead-w0", 0)
            # 2) "live" has a LIVE attempt with a still-running session (must
            #    be re-attached, never re-opened).
            assert h.collab.claim_task(h.task_id("live"), "swarm-dead-w1", 0)
            live_session = h.world.add_session(
                state="running",
                behavior={"kind": "complete", "polls": 1, "output": "carried over"},
            )
            h.dal.open_attempt(
                h.run_id,
                h.task_id("live"),
                provider="claude",
                model="sonnet",
                tier="simple",
                session_id=live_session,
            )
            self._stale_heartbeat(h)

            scheduler = make_scheduler(h)
            handle = scheduler.resume_swarm(h.run_id)
            assert handle is not None

            # NO DOUBLE-COORDINATOR: the adopt CAS refreshed the heartbeat, so
            # a second resume (fresh heartbeat) must refuse.
            second = SwarmScheduler(
                dal=h.dal,
                collab=h.collab,
                emitter=h.emitter,
                spawner=h.world,
                session_store=h.world,
                router=h.router,  # type: ignore[arg-type]
                reviewer=h.reviewer,  # type: ignore[arg-type]
                git=h.git,
                limits=h.limits,
                verifier=lambda t, sj, wd: (True, ""),
                splitter=lambda t, sj: None,
            )
            assert second.resume_swarm(h.run_id) is None
            # And a plain start_run cannot steal an active run either.
            assert second.start_run(h.run_id) is None

            assert handle.join(timeout=20)
            # Claim on "dead" was swept and the task executed normally.
            assert h.status_of("dead") == "done"
            assert len(h.attempts_of("dead")) == 1
            # "live" re-attached: exactly ONE attempt, its original session.
            live_attempts = h.attempts_of("live")
            assert len(live_attempts) == 1
            assert live_attempts[0]["session_id"] == live_session
            assert live_attempts[0]["end_reason"] == "completed"
            assert h.status_of("live") == "done"
            assert h.status_of("fresh") == "done"
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            started = h.emitter.of("run_started")
            assert started and started[0]["resumed"] is True
        finally:
            h.close()

    def test_adopt_refuses_fresh_heartbeat(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "x"}], max_concurrency=1)
        try:
            assert h.dal.try_activate_run(h.run_id)  # heartbeat = now
            assert h.dal.adopt_run(h.run_id, stale_minutes=2) is False
            self._stale_heartbeat(h)
            assert h.dal.adopt_run(h.run_id, stale_minutes=2) is True
            # The winning adopt refreshed the lease: a racing second loses.
            assert h.dal.adopt_run(h.run_id, stale_minutes=2) is False
        finally:
            h.close()


class TestCancel:
    def test_cancel_mid_run_kills_running_blocks_spawns_ignores_late_completion(
        self, tmp_path: Path
    ) -> None:
        gate = threading.Event()
        h = make_harness(
            tmp_path,
            [
                {"id": "hangs", "est": 30},
                {"id": "also", "est": 20},
                {"id": "dep", "depends_on": ["hangs"], "est": 10},
            ],
            max_concurrency=2,
        )
        # "hangs" outruns its kill so its session can complete LATE.
        h.world.set_behavior(
            "hangs", {"kind": "gated", "gate": gate, "polls": 1, "ignore_kill": True}
        )
        h.world.set_behavior("also", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 2, timeout=10)
            spawns_at_cancel = len(h.world.spawn_order)

            # Operator cancel (the API writes exactly this).
            h.dal.set_run_status(h.run_id, "cancelled", error="cancelled by operator")

            # Running attempts get request_kill.
            assert wait_until(lambda: len(h.world.kills) >= 2, timeout=10)

            # LATE COMPLETION: the kill-immune session finishes anyway…
            hangs_session = h.attempts_of("hangs")[0]["session_id"]
            gate.set()
            h.world.resolve(hangs_session, "completed", output="late result")

            assert handle.join(timeout=20)

            # …but is recorded and IGNORED: attempt closed, task never done.
            hangs_attempt = h.attempts_of("hangs")[0]
            assert hangs_attempt["ended_at"] is not None
            assert hangs_attempt["end_reason"] == "killed"
            assert h.status_of("hangs") != "done"
            assert h.status_of("also") != "done"
            # NO NEW SPAWNS after cancel: dep + integration never started.
            assert len(h.world.spawn_order) == spawns_at_cancel
            assert "dep" not in h.world.spawn_order
            assert "integration" not in h.world.spawn_order
            assert h.dal.get_run(h.run_id)["status"] == "cancelled"
        finally:
            h.close()


class TestRateLimits:
    def test_rate_limited_reenqueues_without_consuming_retry(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "rl"}], max_concurrency=1)
        h.world.set_behavior(
            "rl",
            {"kind": "rate_limited", "polls": 1, "error": "429 too many requests"},
            {"kind": "complete", "polls": 1},
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            attempts = h.attempts_of("rl")
            assert [a["end_reason"] for a in attempts] == ["rate_limited", "completed"]
            assert [a["tier"] for a in attempts] == ["simple", "simple"]  # no escalation
            assert int(h.swarm_json_of("rl").get("retries") or 0) == 0  # no retry consumed
            assert int(h.swarm_json_of("rl").get("rate_limit_requeues") or 0) == 1
            assert len(h.limits.reports) == 1  # durable cooldown reported
            assert h.limits.reports[0]["provider"] == "claude"
            assert h.emitter.of("rate_limit")
            assert h.emitter.of("provider_switched")
            assert h.status_of("rl") == "done"
        finally:
            h.close()

    def test_rate_limit_requeues_capped(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "flap"}], max_concurrency=1)
        h.world.set_behavior("flap", {"kind": "rate_limited", "polls": 1})
        try:
            scheduler = make_scheduler(h, rate_limit_requeue_cap=2)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert len(h.attempts_of("flap")) == 3  # 2 re-enqueues + the breach
            assert h.status_of("flap") == "blocked"
            assert any(
                e.get("reason") == "rate_limit_flapping" for e in h.emitter.of("task_blocked")
            )
            # Blocked on flapping, but the run still summarizes (partial).
            assert h.dal.get_run(h.run_id)["status"] in ("completed", "failed")
        finally:
            h.close()


class TestLeaseFencing:
    """m10: adopt_run bumps the lease generation; the original coordinator's
    conditional heartbeat then fails and it stops CLEANLY (no destructive
    ops after displacement)."""

    def test_adoption_displaces_the_original_coordinator_cleanly(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "slow", "est": 30}], max_concurrency=1)
        h.world.set_behavior("slow", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h, heartbeat_seconds=0.2)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.spawn_order == ["slow"], timeout=10)

            # Another process adopts while the original is ALIVE (its
            # heartbeat forced stale, the adopt CAS bumps the generation).
            adopted = False
            for _ in range(400):
                h.dal._connection.execute(
                    "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                    (h.run_id,),
                )
                if h.dal.adopt_run(h.run_id, stale_minutes=2):
                    adopted = True
                    break
                time.sleep(0.01)
            assert adopted

            # The original notices on its NEXT beat (rowcount=0) and stops.
            assert handle.join(timeout=15)
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "running"  # clean abort — never failed
            assert int(run["lease_generation"]) == 1
            # The fence itself: the old generation can no longer beat.
            assert h.dal.heartbeat(h.run_id, generation=0) is False
            assert h.dal.heartbeat(h.run_id, generation=1) is True
            # NOTHING destructive happened after displacement: the session
            # is alive, the attempt live, the claim intact — the adopting
            # coordinator re-attaches all of it from the DB.
            assert h.world.kills == []
            assert all(row["state"] == "running" for row in h.world.sessions.values())
            assert h.dal.current_attempt(h.task_id("slow")) is not None
            assert h.status_of("slow") in ("claimed", "in_progress")
        finally:
            h.close()


class TestWorktreeMode:
    """Phase-2 worktree isolation drills over the FakeWorktrees seam."""

    def _wt_scheduler(
        self, h: Any, tmp_path: Path, **overrides: Any
    ) -> tuple[Any, FakeWorktrees]:
        wt = FakeWorktrees(tmp_path / "wt-root")
        scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=True, **overrides)
        return scheduler, wt

    def test_confirm_merges_land_in_dependency_order(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [
                {"id": "a", "est": 30},
                {"id": "b", "depends_on": ["a"], "est": 20},
                {"id": "c", "est": 10},
            ],
            max_concurrency=3,
            real_git=False,
        )
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # Every worker task got a worktree + a merge; integration is
            # EXEMPT (it must see the merged tree in the main workspace).
            assert set(wt.merges) == {"a", "b", "c"}
            assert "integration" not in {c["task_key"] for c in wt.created}
            # Topological: a's branch merged before its dependent b's.
            assert wt.merges.index("a") < wt.merges.index("b")
            merged = h.emitter.of("branch_merged")
            assert len(merged) == 3
            created = h.emitter.of("worktree_created")
            assert {e["task_id"] for e in created} == {
                h.task_id("a"),
                h.task_id("b"),
                h.task_id("c"),
            }
            # Workers spawned INSIDE their worktree with the git common dir
            # + the run's OWN refs/heads/swarm/<run_id> branch namespace (M3)
            # + the worker's OWN linked-worktree gitdir (R1) as extra Seatbelt
            # write roots; integration in the main dir.
            by_key = {r.task_key: r for r in h.world.spawn_requests}
            ref_namespace = os.path.join(wt.common_dir, "refs", "heads", "swarm", h.run_id)
            assert by_key["a"].working_dir.endswith(f"{h.run_id}/a")
            assert by_key["a"].extra_write_roots == (
                wt.common_dir,
                ref_namespace,
                os.path.join(wt.common_dir, "worktrees", "a"),
            )
            assert by_key["integration"].working_dir == str(h.workdir)
            assert by_key["integration"].extra_write_roots == ()
            # M3: every merge targeted the EXACT sha the quality gate
            # captured for that worktree — never the branch ref.
            assert dict(wt.merge_shas) == {
                "a": "head-a",
                "b": "head-b",
                "c": "head-c",
            }
            # Completed run: worktrees gone, run branches deleted.
            assert wt.active == {}
            assert set(wt.deleted_branches) == {
                f"swarm/{h.run_id}/a",
                f"swarm/{h.run_id}/b",
                f"swarm/{h.run_id}/c",
            }
        finally:
            h.close()

    def test_conflict_parks_dependents_feeds_integration_and_exempts_it(
        self, tmp_path: Path
    ) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "a", "est": 30}, {"id": "b", "depends_on": ["a"], "est": 10}],
            max_concurrency=2,
        )
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_merge("a", "conflict")
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            # The conflicted task itself is DONE (work reviewed + committed on
            # its branch) with the conflict recorded.
            assert h.status_of("a") == "done"
            a_json = h.swarm_json_of("a")
            assert a_json.get("merge_conflict") is True
            assert a_json.get("conflict_files") == ["shared.txt"]
            conflicts = h.emitter.of("merge_conflict")
            assert len(conflicts) == 1
            assert conflicts[0]["branch"] == f"swarm/{h.run_id}/a"

            # D5: the dependent PARKED with the distinct reason...
            assert h.status_of("b") == "blocked"
            reasons = {e["task_id"]: e.get("reason") for e in h.emitter.of("task_blocked")}
            assert reasons.get(h.task_id("b")) == "dep_merge_conflict"
            assert "b" not in h.world.spawn_order  # never spawned

            # ...while INTEGRATION is exempt: it ran (in the MAIN workspace)
            # with the conflict landing in its feedback for the manual merge.
            assert h.status_of("integration") == "done"
            integ_request = next(r for r in h.world.spawn_requests if r.task_key == "integration")
            assert integ_request.working_dir == str(h.workdir)
            feedback = h.swarm_json_of("integration").get("feedback") or []
            conflict_notes = [f for f in feedback if f.get("source") == "merge_conflict"]
            assert len(conflict_notes) == 1
            assert f"swarm/{h.run_id}/a" in conflict_notes[0]["text"]
            assert "shared.txt" in conflict_notes[0]["text"]
            # The routed branch is STRUCTURED on the entry (M1 brief listing).
            assert conflict_notes[0]["branch"] == f"swarm/{h.run_id}/a"
            # R4: the entry carries the VERIFIED sha and the note instructs
            # merging it (a sibling can move the ref after routing).
            assert conflict_notes[0]["sha"] == "head-a"
            assert "merge head-a manually" in conflict_notes[0]["text"]
            # The conflict note reached the integration worker's prompt.
            assert f"swarm/{h.run_id}/a" in integ_request.prompt
            # M1: the worktree-mode integration brief is CONSISTENT with the
            # conflict feedback — no Phase-1 "NEVER run git" hard rule, an
            # explicit merge permission scoped to the routed sha (R4; branch
            # named for humans), and everything else still forbidden.
            assert "NEVER run `git add`" not in integ_request.prompt
            assert "You MAY run `git merge`, `git add`, and `git commit`" in (integ_request.prompt)
            assert f"  - merge head-a (branch swarm/{h.run_id}/a)" in integ_request.prompt
            assert "ALL other git mutation is forbidden" in integ_request.prompt

            # Run completes PARTIAL (b blocked).
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "completed"
            assert h.emitter.of("run_completed")[0]["partial"] is True
        finally:
            h.close()

    def test_resume_with_deleted_worktree_requeues_without_duplicate_attempts(
        self, tmp_path: Path
    ) -> None:
        h = make_harness(tmp_path, [{"id": "hurt", "est": 30}], max_concurrency=1)
        try:
            # Simulate a crashed coordinator whose live attempt's worktree
            # vanished from disk (var wipe / manual cleanup).
            assert h.dal.try_activate_run(h.run_id)
            assert h.collab.claim_task(h.task_id("hurt"), "swarm-dead-w0", 0)
            orphan_session = h.world.add_session(state="running")
            h.dal.open_attempt(
                h.run_id,
                h.task_id("hurt"),
                provider="claude",
                model="sonnet",
                tier="simple",
                session_id=orphan_session,
            )
            missing = str(tmp_path / "wt-root" / h.run_id / "hurt")
            merged = h.dal.get_swarm_json(h.task_id("hurt")) or {}
            merged.update(
                {
                    "worktree_path": missing,
                    "worktree_branch": f"swarm/{h.run_id}/hurt",
                    "worktree_base_sha": "base0",
                }
            )
            h.dal.set_swarm_json(h.task_id("hurt"), merged)
            h.dal._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (h.run_id,),
            )

            scheduler, wt = self._wt_scheduler(h, tmp_path)
            handle = scheduler.resume_swarm(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            attempts = h.attempts_of("hurt")
            # EXACTLY one successor: crashed close + fresh attempt, no dupes.
            assert len(attempts) == 2
            assert attempts[0]["end_reason"] == "crashed"
            assert "worktree missing" in str(attempts[0]["detail"] or "")
            assert attempts[1]["end_reason"] == "completed"
            # m7: the possibly-live orphan session was killed BEFORE the
            # requeue (aligned with the timeout path) — no concurrent writer.
            assert orphan_session in h.world.kills
            assert h.world.sessions[orphan_session]["state"] == "killed"
            assert h.status_of("hurt") == "done"
            # The successor got a FRESH worktree (branch reuse is the real
            # impl's concern; the seam records exactly one create).
            assert [c["task_key"] for c in wt.created] == ["hurt"]
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_worktree_mode_is_recorded_at_activation(self, tmp_path: Path) -> None:
        """m6: the first coordinator records the RESOLVED mode on the run row."""
        h = make_harness(tmp_path, [{"id": "a"}], max_concurrency=1)
        try:
            scheduler, _wt = self._wt_scheduler(h, tmp_path)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert h.dal.get_run_params(h.run_id) == {"worktree_mode": True}
        finally:
            h.close()

    def test_recorded_worktree_mode_wins_on_resume_without_ambient_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """m6: a run RECORDED as worktree-mode, resumed by a process without
        the env var (and with the config default off, and no injected flag),
        is STILL treated as worktree mode — the missing-worktree check and
        the worktree GC/cleanup both run."""
        monkeypatch.delenv("OMNIAGENTOS_SWARM_WORKTREES", raising=False)
        h = make_harness(tmp_path, [{"id": "hurt", "est": 30}], max_concurrency=1)
        try:
            # The run was created + activated in worktree mode (recorded).
            assert h.dal.try_activate_run(h.run_id)
            h.dal.merge_run_params(h.run_id, {"worktree_mode": True})
            assert h.collab.claim_task(h.task_id("hurt"), "swarm-dead-w0", 0)
            orphan_session = h.world.add_session(state="running")
            h.dal.open_attempt(
                h.run_id,
                h.task_id("hurt"),
                provider="claude",
                model="sonnet",
                tier="simple",
                session_id=orphan_session,
            )
            missing = str(tmp_path / "wt-root" / h.run_id / "hurt")
            merged = h.dal.get_swarm_json(h.task_id("hurt")) or {}
            merged.update(
                {
                    "worktree_path": missing,
                    "worktree_branch": f"swarm/{h.run_id}/hurt",
                    "worktree_base_sha": "base0",
                }
            )
            h.dal.set_swarm_json(h.task_id("hurt"), merged)
            h.dal._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (h.run_id,),
            )

            # Resume WITHOUT any ambient signal: worktrees_enabled=None means
            # "resolve from env/config", both of which say Phase 1 here.
            wt = FakeWorktrees(tmp_path / "wt-root")
            scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=None)
            handle = scheduler.resume_swarm(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            # The missing-worktree check ran (recorded mode, not ambient).
            attempts = h.attempts_of("hurt")
            assert attempts[0]["end_reason"] == "crashed"
            assert "worktree missing" in str(attempts[0]["detail"] or "")
            assert h.status_of("hurt") == "done"
            # The successor got a worktree + a merge — full worktree flow.
            assert [c["task_key"] for c in wt.created] == ["hurt"]
            assert wt.merges == ["hurt"]
            # Terminal worktree cleanup ran too (no orphans left).
            assert wt.active == {}
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_recorded_phase1_mode_never_flips_midrun(self, tmp_path: Path) -> None:
        """m6 (other direction): a run recorded Phase-1 stays Phase-1 even
        when a later coordinator launches with the worktree flag ON."""
        h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a.txt"]}], max_concurrency=1)
        try:
            h.dal.merge_run_params(h.run_id, {"worktree_mode": False})
            scheduler, wt = self._wt_scheduler(h, tmp_path)  # flag ON
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.dal.get_run(h.run_id)["status"] == "completed"
            assert wt.created == []
            assert wt.merges == []
            for request in h.world.spawn_requests:
                assert request.working_dir == str(h.workdir)
                assert request.extra_write_roots == ()
        finally:
            h.close()

    def test_cancel_leaves_no_orphan_worktrees_and_keeps_branches(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "one", "est": 30}, {"id": "two", "est": 20}],
            max_concurrency=2,
        )
        h.world.set_behavior("one", {"kind": "hang"})
        h.world.set_behavior("two", {"kind": "hang"})
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: len(wt.created) >= 2, timeout=10)

            h.dal.set_run_status(h.run_id, "cancelled", error="cancelled by operator")
            assert handle.join(timeout=20)

            # Terminal cleanup: no orphan worktrees, salvage on every removal,
            # branches KEPT (forensics on non-completed runs).
            assert wt.active == {}
            assert wt.removed and all(entry["salvage"] for entry in wt.removed)
            assert wt.deleted_branches == []
            assert h.dal.get_run(h.run_id)["status"] == "cancelled"
        finally:
            h.close()

    def test_ownership_violation_reverts_from_branch_base_and_commits(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "a", "owned_paths": ["src/a.txt"]}],
            max_concurrency=1,
        )
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_changed("a", ["src/a.txt", "evil/other.txt"])
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("a") == "done"
            # Reverted from the worktree BASE (covers committed out-of-scope
            # work, not just the uncommitted delta)...
            assert h.git.reverts and h.git.reverts[0][1] == ["evil/other.txt"]
            # ...and the revert is PERSISTED on the branch by a corrective
            # commit before the merge.
            revert_commits = [
                message for _, message in h.git.commits if "revert out-of-scope" in message
            ]
            assert revert_commits
            # The violation flagged the review.
            flagged = [c for c in h.reviewer.calls if c["key"] == "a" and c["flags"]]
            assert flagged and "ownership violation" in flagged[0]["flags"][0]
        finally:
            h.close()

    def test_coordinator_file_edits_reverted_from_worktree_branch(self, tmp_path: Path) -> None:
        """m8: a worker's PLAN.md edit in its worktree is reverted from the
        branch base and the revert is COMMITTED on the branch before merge —
        it can neither land in main nor manufacture a spurious conflict.
        PLAN.md is still no ownership VIOLATION (coordinator-file exemption)."""
        h = make_harness(
            tmp_path,
            [{"id": "a", "owned_paths": ["src/a.txt"]}],
            max_concurrency=1,
        )
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_changed("a", ["src/a.txt", "PLAN.md"])
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("a") == "done"
            assert wt.merges == ["a"]
            # PLAN.md reverted from the branch base...
            assert any(paths == ["PLAN.md"] for _, paths in h.git.reverts)
            # ...and the revert PERSISTED on the branch by a corrective commit.
            assert any("revert coordinator-owned files" in message for _, message in h.git.commits)
            # Flagged for the reviewer, but NOT as an ownership violation.
            a_flags = [f for c in h.reviewer.calls if c["key"] == "a" for f in c["flags"]]
            assert any("coordinator-file edits reverted" in f for f in a_flags)
            assert not any("ownership violation" in f for f in a_flags)
        finally:
            h.close()

    def test_phase1_path_is_untouched_when_flag_off(self, tmp_path: Path) -> None:
        """D4 pin: with worktrees disabled the seam is NEVER consulted and the
        run behaves exactly like Phase 1 (shared dir, no extra write roots,
        shared-directory brief, pathspec confirm commit, no worktree events)."""
        h = make_harness(
            tmp_path,
            [{"id": "a", "owned_paths": ["src/a.txt"]}],
            max_concurrency=1,
        )
        try:
            wt = FakeWorktrees(tmp_path / "wt-root")
            scheduler = make_scheduler(h, worktrees=wt, worktrees_enabled=False)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.dal.get_run(h.run_id)["status"] == "completed"
            # The worktree seam was never touched.
            assert wt.created == []
            assert wt.merges == []
            assert wt.removed == []
            # Spawns are Phase-1 shaped.
            for request in h.world.spawn_requests:
                assert request.working_dir == str(h.workdir)
                assert request.extra_write_roots == ()
                assert "dedicated worktree" not in request.prompt
                assert "private git branch" not in request.prompt
            assert "SAME directory" in h.world.spawn_requests[0].prompt
            # Phase-1 confirm: coordinator-owned pathspec commit happened.
            assert any(paths == ["src/a.txt"] for paths, _ in h.git.commits)
            # No Phase-2 events.
            actions = set(h.emitter.actions())
            assert not actions & {"worktree_created", "branch_merged", "merge_conflict"}
        finally:
            h.close()

    def test_phase1_worker_brief_is_byte_identical_pin(self) -> None:
        """The exact Phase-1 brief text, pinned: worktree mode must not change
        one byte of the flag-off prompt."""
        swarm_json = {
            "plan_version": 3,
            "plan_hash": "abcdef123456ff00",
            "owned_paths": ["src/a.py"],
            "acceptance": "ok",
            "verify_command": "true",
        }
        brief = build_worker_brief({}, {"title": "T", "description": "D"}, swarm_json, {})

        repo_root = Path(__file__).resolve().parent.parent.parent
        agents_path = (repo_root / "AGENTS.md").resolve()
        testing_path = (repo_root / "TESTING.md").resolve()
        # Use the same logic as build_worker_brief: resolve ARCHI.md with fallback to ARCHITECTURE.md
        archi_md_path = (repo_root / "ARCHI.md").resolve()
        archi_json_path = (repo_root / "ARCHI.json").resolve()
        architecture_path = archi_md_path if archi_md_path.exists() else (repo_root / "ARCHITECTURE.md").resolve()
        decisions_path = (repo_root / "DECISIONS.md").resolve()
        workbook_path = (
            repo_root / "var" / "swarm" / "run_id" / "task_id" / "WORKBOOK.md"
        ).resolve()
        task_md_path = (repo_root / "var" / "swarm" / "run_id" / "task_id" / "TASK.md").resolve()
        expected = "\n".join(
            [
                "You are ONE WORKER in an OmniAgentOS swarm run. Other agents are working",
                "in the SAME directory concurrently. Deliver ONLY your task, ONLY inside",
                "your owned paths.",
                "",
                "Plan version 3, hash abcdef123456 — PLAN.md in the",
                "workspace is a derived projection; trust it only if its hash matches.",
                "",
                "## Task",
                "T",
                "",
                "D",
                "",
                "Acceptance: ok",
                "Verify: true",
                "",
                "## Owned paths (the ONLY files you may create or modify)",
                "- src/a.py",
                "",
                "## Hard rules",
                "- NEVER run `git add`, `git commit`, or any other git mutation — all git",
                "  operations are coordinator-owned in this shared directory.",
                "- Never edit PLAN.md.",
                "- Out-of-scope changes are reverted automatically and flag your review.",
                "- Deliver by CREATING or EDITING FILES with your tools inside your owned",
                "  paths. A chat-only answer with no file changes is a FAILED attempt.",
                "",
                "## Your documents",
                f"- Task Contract: {task_md_path}",
                f"- Task Workbook (Progress Log): {workbook_path}",
                f"- House Rules & Orchestration: {agents_path}",
                f"- Testing Guide: {testing_path}",
                f"- Architecture Index: {architecture_path}" + (f" (machine-readable: {archi_json_path})" if archi_md_path.exists() else ""),
                f"- Decision Ledger (ADRs): {decisions_path}",
                "- Memory Ritual: No project is registered for this task, so there is no shared memory file yet.",
                "",
                "## Standing capability grants",
                "- You currently hold no standing capability grants.",
                "- To request access, write to: `/var/requests/capability_request.md`",
            ]
        )
        assert brief == expected
        # And the worktree variant swaps ONLY the opening + hard rules.
        wt_brief = build_worker_brief(
            {},
            {"title": "T", "description": "D"},
            {**swarm_json, "worktree_branch": "swarm/r/a"},
            {},
        )
        assert "private git branch swarm/r/a" in wt_brief
        assert "SAME directory" not in wt_brief
        assert "NEVER push, pull, merge, rebase" in wt_brief

    def test_merge_raise_in_confirm_aborts_and_routes_to_integration(self, tmp_path: Path) -> None:
        """M4b: a merge that RAISES inside _confirm_merge triggers a
        best-effort merge --abort of the main workspace, and the failure
        still routes to integration like a content conflict."""
        h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a.txt"]}], max_concurrency=1)
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_merge("a", "raise")
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("a") == "done"
            assert str(h.workdir) in wt.aborts
            feedback = h.swarm_json_of("integration").get("feedback") or []
            notes = [f for f in feedback if f.get("source") == "merge_conflict"]
            assert notes and "infrastructure failure" in notes[0]["text"]
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_wedged_merge_head_is_aborted_at_coordinator_start(self, tmp_path: Path) -> None:
        """M4c: MERGE_HEAD present in the main workspace when a coordinator
        starts (launch or resume) is aborted best-effort, logged, and emits a
        merge_aborted activity event."""
        h = make_harness(tmp_path, [{"id": "a"}], max_concurrency=1)
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.pending_merge = True
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert wt.aborts and wt.aborts[0] == str(h.workdir)
            aborted = h.emitter.of("merge_aborted")
            assert len(aborted) == 1
            assert aborted[0]["reason"] == "MERGE_HEAD present at coordinator start"
            assert aborted[0]["aborted"] is True
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_confirm_dirty_worktree_is_salvaged_before_remove(self, tmp_path: Path) -> None:
        """M2d: the post-CONFIRM removal clean-checks the worktree; owned
        dirt is salvage-committed to the branch BEFORE the remove."""
        h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a.txt"]}], max_concurrency=1)
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_dirty("a", ["src/a.txt"])
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("a") == "done"
            assert ("a", f"swarm {h.run_id}: salvage confirmed work for task a") in (wt.salvages)
            # The remove still happened — WITH salvage (M2d belt-and-
            # suspenders: dirt appearing between the clean-check and the
            # remove is committed rather than destroyed).
            a_removals = [r for r in wt.removed if r["path"].endswith(f"{h.run_id}/a")]
            assert a_removals and a_removals[0]["salvage"] is True
            assert not h.emitter.of("worktree_kept")
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_confirm_salvage_failure_leaves_worktree_in_place(self, tmp_path: Path) -> None:
        """M2c: when the salvage of confirmed work fails, the worktree is
        LEFT IN PLACE (never force-removed — not even by terminal cleanup),
        its path lands in the integration feedback, and a ``worktree_kept``
        activity event fires."""
        h = make_harness(tmp_path, [{"id": "a", "owned_paths": ["src/a.txt"]}], max_concurrency=1)
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path)
            wt.set_dirty("a", ["src/a.txt"])
            wt.set_salvage_fail("a")
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("a") == "done"
            a_path = str(tmp_path / "wt-root" / h.run_id / "a")
            # Never removed: not at confirm, not at terminal cleanup.
            assert a_path not in [r["path"] for r in wt.removed]
            assert wt.active.get(a_path) == "a"
            kept = h.emitter.of("worktree_kept")
            assert len(kept) == 1
            assert kept[0]["path"] == a_path
            assert kept[0]["dirty"] == ["src/a.txt"]
            # R2a: the kept marker is DURABLE on the task's swarm_json — a
            # successor coordinator's GC (orphan prune, terminal cleanup)
            # consults the record, not this process's memory.
            assert h.swarm_json_of("a").get("worktree_kept") is True
            # Recorded in the integration feedback for manual recovery.
            feedback = h.swarm_json_of("integration").get("feedback") or []
            kept_notes = [f for f in feedback if f.get("source") == "worktree_kept"]
            assert len(kept_notes) == 1
            assert a_path in kept_notes[0]["text"]
            # Branch deletion skipped too (the kept worktree holds its branch).
            assert wt.deleted_branches == []
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_concurrent_conflict_routings_both_survive(self, tmp_path: Path) -> None:
        """M5: two workers routing merge conflicts CONCURRENTLY must both
        land their integration feedback entries. The wrapping DAL sleeps
        inside the read half of the RMW, so an unlocked read-modify-write
        deterministically loses one entry — the git_lock serializes it."""
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(
            tmp_path,
            [{"id": "a"}, {"id": "b"}],
            max_concurrency=2,
        )
        try:
            integration_id = h.task_id("integration")

            class SlowRmwDal:
                def __init__(self, inner: object) -> None:
                    self._inner = inner

                def get_swarm_json(self, task_id: str) -> Any:
                    result = self._inner.get_swarm_json(task_id)  # type: ignore[attr-defined]
                    if str(task_id) == integration_id:
                        time.sleep(0.1)  # widen the read→write race window
                    return result

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            wt = FakeWorktrees(tmp_path / "wt-root")
            scheduler = make_scheduler(
                h, dal=SlowRmwDal(h.dal), worktrees=wt, worktrees_enabled=True
            )
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))

            def route(key: str) -> None:
                scheduler._route_conflict_to_integration(
                    state, key, f"swarm/{h.run_id}/{key}", f"sha-{key}", ("shared.txt",), ""
                )

            threads = [threading.Thread(target=route, args=(key,)) for key in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            feedback = h.swarm_json_of("integration").get("feedback") or []
            branches = {f.get("branch") for f in feedback if f.get("source") == "merge_conflict"}
            assert branches == {f"swarm/{h.run_id}/a", f"swarm/{h.run_id}/b"}
        finally:
            h.close()

    def test_all_conflict_feedback_entries_reach_the_integration_brief(self) -> None:
        """M5: merge-conflict entries are exempt from the last-3 feedback
        truncation — with 4 routed conflicts buried under newer notes, the
        integration brief still names ALL of them."""
        conflicts = [
            {"source": "merge_conflict", "branch": f"swarm/r/t{i}", "text": f"merge t{i}"}
            for i in range(4)
        ]
        others = [{"source": "retry", "text": f"note {i}"} for i in range(4)]
        swarm_json = {
            "integration": True,
            "worktree_integration": True,
            "owned_paths": ["."],
            # All four conflicts FIRST, then four newer non-conflict notes —
            # a plain feedback[-3:] would drop every conflict.
            "feedback": [*conflicts, *others],
        }
        brief = build_worker_brief({}, {"title": "I", "description": "D"}, swarm_json, {})
        for i in range(4):
            assert f"merge t{i}" in brief  # feedback text
            assert f"  - merge swarm/r/t{i}" in brief  # routed-merge listing (M1/R4)
        # The non-conflict notes still truncate to the last three.
        assert "note 0" not in brief
        assert "note 1" in brief and "note 2" in brief and "note 3" in brief

    def test_worktree_integration_brief_permits_only_routed_conflict_merges(self) -> None:
        """M1 pin: the worktree-mode INTEGRATION brief variant (both the
        scheduler brief and spawn.py's relay-rules mirror) drops the Phase-1
        no-git hard rule, names the routed conflict branches, and forbids all
        other git mutation — consistent with the conflict feedback that tells
        it to merge those branches manually."""
        from omniagentos.swarm.spawn import UnifiedSpawner

        swarm_json = {
            "integration": True,
            "worktree_integration": True,
            "owned_paths": ["."],
            "feedback": [
                {"source": "merge_conflict", "branch": "swarm/r/a", "text": "merge a"},
                {"source": "retry", "text": "unrelated"},
                {"source": "merge_conflict", "branch": "swarm/r/b", "text": "merge b"},
            ],
        }
        brief = build_worker_brief({}, {"title": "I", "description": "D"}, swarm_json, {})
        rules = UnifiedSpawner._swarm_rules(swarm_json)
        for text in (brief, rules):
            assert "NEVER run `git add`" not in text
            assert "You MAY run `git merge`, `git add`, and `git commit`" in text
            assert "  - merge swarm/r/a" in text
            assert "  - merge swarm/r/b" in text
            assert "ALL other git mutation is forbidden" in text
        # Without routed branches the permission grants NOTHING.
        empty = build_worker_brief(
            {},
            {"title": "I", "description": "D"},
            {"integration": True, "worktree_integration": True, "owned_paths": ["."]},
            {},
        )
        assert "(none routed — in that case run NO git mutation at all)" in empty
        # A NON-integration task never gets the merge permission, and a
        # Phase-1 integration task keeps the byte-identical no-git brief.
        phase1 = build_worker_brief(
            {}, {"title": "I", "description": "D"}, {"integration": True}, {}
        )
        assert "NEVER run `git add`" in phase1
        assert "You MAY run `git merge`" not in phase1

    def test_blocked_task_worktree_is_salvage_removed(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "bad", "est": 10}], max_concurrency=1)
        h.world.set_behavior("bad", {"kind": "fail", "polls": 1})
        try:
            scheduler, wt = self._wt_scheduler(h, tmp_path, retry_cap=0)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.status_of("bad") == "blocked"
            # The blocked task's worktree was removed WITH salvage so partial
            # work stays inspectable on the branch.
            assert any(entry["salvage"] for entry in wt.removed)
            assert wt.active == {}
        finally:
            h.close()
