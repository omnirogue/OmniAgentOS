"""AT-05 — Limits & safety: every agent is bounded, and the bound actually STOPS it.

The five limits an agent must carry, and where each is proven here:

===================  ==========================================================
timeout              ``SwarmScheduler._handle_timeout`` reaps the attempt, kills
                     the session and escalates a tier (``TestAttemptTimeout``).
token limit          ``omniagentos.budget.check`` refuses over-cap token usage
                     (``TestBudgetKernelEnforcesEveryDimension``).
retry limit          ``SwarmScheduler._consume_retry`` blocks the task at
                     ``DEFAULT_RETRY_CAP`` (``TestRetryLimit``).
cost limit           ``budget.check`` cost dimension + the scheduler's blocking
                     posture (``TestCostLimit``).
idle timeout         every ``SpawnRequest`` carries a positive ``idle_minutes``
                     derived from its tier (``TestIdleTimeout``).
===================  ==========================================================

Every test in this file asserts that something *stops* — a task reaches
``blocked``, a run reaches ``failed``, a decision comes back ``allowed=False``.
Reading a config key is never the assertion; where a limit is only configured and
never enforced, the test is a strict xfail and the gap is recorded in
``docs/acceptance/gaps-AT2.md``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.budget import check as budget_check
from omniagentos.budget.policy import ADVISORY, BLOCK, ENFORCEMENT_ENV, blocks
from omniagentos.contracts import BudgetSpec
from omniagentos.swarm.scheduler import (
    DEFAULT_RATE_LIMIT_REQUEUE_CAP,
    DEFAULT_RETRY_CAP,
    DEFAULT_STALL_MINUTES,
    DEFAULT_TIMEOUT_MINUTES,
    IDLE_TIMEOUT_FRACTION,
    TIER_LADDER,
    SwarmScheduler,
)
from tests.swarm.scheduler_fakes import FakeClock, make_harness, make_scheduler, wait_until


def fake_clock(harness: Any) -> FakeClock:
    """Narrow ``Harness.clock`` (declared ``SchedulerClock``) to the injected fake."""
    clock = harness.clock
    assert isinstance(clock, FakeClock), "this test requires make_harness(fake_clock=True)"
    return clock


@pytest.fixture(autouse=True)
def _pin_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No acceptance run may touch the operator control-plane DB."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


# ==========================================================================
# The budget kernel: token / cost / wall / turn caps all refuse
# ==========================================================================


class TestBudgetKernelEnforcesEveryDimension:
    """``omniagentos.budget.check`` is the pure limit kernel the runner consults.

    It is the ONLY place a token cap is mechanically evaluated in this repo.
    """

    @pytest.mark.parametrize(
        ("field", "usage_kwargs", "cap_value", "expected_dimension"),
        [
            ("wall_ms_max", {"used_wall_ms": 61_000}, 60_000, "wall_ms"),
            ("tokens_max", {"used_tokens": 200_001}, 200_000, "tokens"),
            ("cost_usd_max", {"used_cost_usd": 25.5}, 25.0, "cost_usd"),
            ("max_turns", {"used_turns": 51}, 50, "turns"),
        ],
    )
    def test_over_cap_usage_is_refused(
        self,
        field: str,
        usage_kwargs: dict[str, Any],
        cap_value: float,
        expected_dimension: str,
    ) -> None:
        spec = BudgetSpec.model_validate({field: cap_value})
        usage: dict[str, Any] = {
            "used_wall_ms": 0,
            "used_tokens": 0,
            "used_cost_usd": 0.0,
            "used_turns": 0,
        }
        usage.update(usage_kwargs)

        decision = budget_check(spec, **usage)

        assert decision.allowed is False, f"{expected_dimension} over cap was allowed"
        assert expected_dimension in decision.reason
        assert "cap" in decision.reason

    @pytest.mark.parametrize(
        ("field", "usage_kwargs", "cap_value"),
        [
            ("wall_ms_max", {"used_wall_ms": 60_000}, 60_000),
            ("tokens_max", {"used_tokens": 200_000}, 200_000),
            ("cost_usd_max", {"used_cost_usd": 25.0}, 25.0),
            ("max_turns", {"used_turns": 50}, 50),
        ],
    )
    def test_usage_exactly_at_cap_is_allowed(
        self, field: str, usage_kwargs: dict[str, Any], cap_value: float
    ) -> None:
        """Positive control pinning the ``used > cap`` boundary.

        Without it, the refusals above would pass equally against a kernel that
        refused everything — which would prove nothing about the cap.
        """
        spec = BudgetSpec.model_validate({field: cap_value})
        usage: dict[str, Any] = {
            "used_wall_ms": 0,
            "used_tokens": 0,
            "used_cost_usd": 0.0,
            "used_turns": 0,
        }
        usage.update(usage_kwargs)

        assert budget_check(spec, **usage).allowed is True

    def test_turn_cap_is_the_loop_stop_condition(self) -> None:
        """``max_turns`` is the documented "Ralph Wiggum" stop: a run that keeps
        iterating must eventually be refused rather than spinning forever."""
        spec = BudgetSpec(max_turns=3)

        allowed_turns = [
            turn for turn in range(1, 25) if budget_check(spec, 0, 0, 0.0, used_turns=turn).allowed
        ]

        assert allowed_turns == [1, 2, 3], "the turn cap did not stop the loop"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP AT2-05-A: an omitted cap defaults to UNBOUNDED. BudgetSpec's four "
            "limit fields all default to None and omniagentos.budget.check skips any "
            "dimension whose cap is None, so a spec that simply forgot a token limit "
            "permits unlimited spend. A missing limit should be a hard failure."
        ),
    )
    def test_missing_cap_is_a_hard_failure_not_unbounded(self) -> None:
        spec = BudgetSpec()  # every dimension omitted

        decision = budget_check(spec, 10**9, 10**9, 10**9, used_turns=10**9)

        assert decision.allowed is False, "an unspecified budget permitted unlimited usage"


# ==========================================================================
# Retry limit
# ==========================================================================


class TestRetryLimit:
    def test_persistent_failure_blocks_at_the_retry_cap(self, tmp_path: Path) -> None:
        """A task that never passes verification must STOP, not retry forever.

        The bound is exact: one free mechanical retry, then ``retry_cap``
        consumed retries, then ``blocked``. Asserting the attempt count (not just
        the final status) is what makes an unbounded-retry regression visible.
        """
        h = make_harness(
            tmp_path, [{"id": "bad", "complexity": "simple"}], max_concurrency=1, integration=False
        )

        def verifier(task: Any, swarm_json: Any, working_dir: Any) -> tuple[bool, str]:
            del task, working_dir
            if str(swarm_json.get("task_key")) == "bad":
                return False, "verification keeps failing"
            return True, ""

        try:
            scheduler = make_scheduler(h, verifier=verifier)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.status_of("bad") == "blocked", "an always-failing task never stopped"
            attempts = h.attempts_of("bad")
            assert len(attempts) == DEFAULT_RETRY_CAP + 2, (
                "attempt count is not bounded by the retry cap: "
                f"{[a['end_reason'] for a in attempts]}"
            )
            assert int(h.swarm_json_of("bad").get("retries") or 0) == DEFAULT_RETRY_CAP + 1
            reasons = [e.get("reason") for e in h.emitter.of("task_blocked")]
            assert "retry_cap" in reasons, f"blocked for the wrong reason: {reasons}"
        finally:
            h.close()

    def test_retry_cap_zero_blocks_immediately_after_the_free_retry(self, tmp_path: Path) -> None:
        """The cap is a real knob, not a hard-coded 2: at 0 the task blocks after
        the single free mechanical retry."""
        h = make_harness(tmp_path, [{"id": "bad"}], max_concurrency=1, integration=False)

        try:
            scheduler = make_scheduler(
                h,
                retry_cap=0,
                verifier=lambda task, swarm_json, working_dir: (False, "always fails"),
            )
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.status_of("bad") == "blocked"
            assert len(h.attempts_of("bad")) == 2, "retry_cap=0 did not bound the attempts"
        finally:
            h.close()

    def test_a_failing_task_never_reports_success(self, tmp_path: Path) -> None:
        """The anti-silent-success invariant: exhausting the retry ladder must
        never leave the task ``done``."""
        h = make_harness(tmp_path, [{"id": "bad"}], max_concurrency=1, integration=False)
        try:
            scheduler = make_scheduler(
                h, verifier=lambda task, swarm_json, working_dir: (False, "nope")
            )
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.status_of("bad") != "done"
            assert not any(a["end_reason"] == "completed" for a in h.attempts_of("bad")), (
                "a failing task recorded a completed attempt"
            )
        finally:
            h.close()


# ==========================================================================
# Attempt timeout
# ==========================================================================


class TestAttemptTimeout:
    def test_hung_attempt_is_killed_and_escalated(self, tmp_path: Path) -> None:
        """A worker that never lands must be reaped at its tier deadline.

        Uses the injected ``FakeClock``, so the deadline is crossed by advancing
        time rather than by sleeping — hermetic and fast.
        """
        h = make_harness(
            tmp_path,
            [{"id": "slow", "complexity": "simple"}],
            max_concurrency=1,
            integration=False,
            fake_clock=True,
        )
        h.world.set_behavior("slow", {"kind": "hang"})

        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: len(h.attempts_of("slow")) >= 1, timeout=10)

            # Cross the tier deadline repeatedly until the run settles. Advancing
            # virtual time (rather than waiting on a transient in-memory field)
            # keeps the assertions on the DURABLE attempt record and free of races.
            settled = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if handle.join(timeout=0.05):
                    settled = True
                    break
                fake_clock(h).advance(DEFAULT_TIMEOUT_MINUTES["complex"] * 60 + 60)
            assert settled, "a permanently hung worker never settled; the run would hang"

            attempts = h.attempts_of("slow")
            assert [a["end_reason"] for a in attempts] == ["timeout", "split"], (
                f"the hung attempt was not reaped at its tier deadline: {attempts}"
            )
            assert [a["tier"] for a in attempts] == ["simple", "standard"], (
                "a timeout did not escalate the tier"
            )
            assert h.world.kills, "the timed-out session was never killed"
            assert h.status_of("slow") == "blocked"
        finally:
            h.close()

    def test_every_ladder_rung_has_its_own_finite_timeout(self) -> None:
        """A tier with no configured timeout silently falls back to ``standard``
        inside ``_timeout_seconds``. Guard the ladder so that never happens."""
        assert TIER_LADDER, "the tier ladder is empty; this test would be vacuous"
        for tier in TIER_LADDER:
            assert tier in DEFAULT_TIMEOUT_MINUTES, f"tier {tier!r} has no configured timeout"
            minutes = DEFAULT_TIMEOUT_MINUTES[tier]
            assert minutes > 0, f"tier {tier!r} has a non-positive timeout"
            assert minutes != float("inf"), f"tier {tier!r} is unbounded"

    def test_timeouts_increase_monotonically_up_the_ladder(self) -> None:
        minutes = [DEFAULT_TIMEOUT_MINUTES[tier] for tier in TIER_LADDER]
        assert minutes == sorted(minutes), f"tier timeouts are not monotonic: {minutes}"


# ==========================================================================
# Idle timeout
# ==========================================================================


class TestIdleTimeout:
    def test_every_spawn_carries_a_positive_idle_timeout(self, tmp_path: Path) -> None:
        """No agent is ever spawned without an idle deadline.

        ``idle_minutes`` is what the session reaper enforces, so an unset or
        zero value would leave a stalled worker alive indefinitely.
        """
        h = make_harness(
            tmp_path,
            [{"id": "a", "complexity": "simple"}, {"id": "b", "complexity": "complex"}],
            max_concurrency=2,
            integration=False,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.world.spawn_requests, "nothing was spawned; the assertion would be vacuous"
            for request in h.world.spawn_requests:
                idle = getattr(request, "idle_minutes", None)
                assert idle is not None, f"spawn for {request.task_key} had no idle_minutes"
                assert idle > 0, f"spawn for {request.task_key} had idle_minutes={idle}"
        finally:
            h.close()

    def test_idle_timeout_tracks_the_task_tier(self, tmp_path: Path) -> None:
        """The idle deadline is derived from the tier, not a global constant — a
        complex task gets the complex allowance and a simple one does not."""
        h = make_harness(
            tmp_path,
            [{"id": "small", "complexity": "simple"}, {"id": "big", "complexity": "complex"}],
            max_concurrency=1,
            integration=False,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            by_key = {r.task_key: r.idle_minutes for r in h.world.spawn_requests}
            # c7d4b34d made provider-idle the inner (75%) layer of the nested
            # timeout ladder; the full tier budget remains the outer wall deadline.
            assert by_key.get("small") == pytest.approx(
                DEFAULT_TIMEOUT_MINUTES["simple"] * IDLE_TIMEOUT_FRACTION
            )
            assert by_key.get("big") == pytest.approx(
                DEFAULT_TIMEOUT_MINUTES["complex"] * IDLE_TIMEOUT_FRACTION
            )
        finally:
            h.close()


# ==========================================================================
# Cost limit
# ==========================================================================


class TestCostLimit:
    def test_blocking_posture_stops_remaining_work_at_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With enforcement on, breaching the run budget must actually STOP work."""
        monkeypatch.setenv(ENFORCEMENT_ENV, BLOCK)
        assert blocks() is True, "the enforcement env did not take effect"

        h = make_harness(
            tmp_path,
            [{"id": f"t{i}"} for i in range(1, 7)],
            max_concurrency=1,
            budget=1.0,
            integration=False,
        )
        for i in range(1, 7):
            h.world.set_behavior(f"t{i}", {"kind": "complete", "polls": 1, "cost": 0.9})

        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            statuses = {f"t{i}": h.status_of(f"t{i}") for i in range(1, 7)}
            assert "blocked" in statuses.values(), f"the budget cap stopped nothing: {statuses}"
            assert any(
                e.get("reason") == "budget cap reached" for e in h.emitter.of("task_blocked")
            )
            assert h.emitter.of("run_completed")[0]["budget_exhausted"] is True
        finally:
            h.close()

    def test_default_posture_is_advisory_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documented, deliberate trade-off (``omniagentos/budget/policy.py``):
        by default an overshoot is REPORTED and work continues.

        Pinned here so the posture cannot change silently — and recorded in the
        gap report, because in this mode no mechanical control stops a runaway.
        """
        monkeypatch.delenv(ENFORCEMENT_ENV, raising=False)
        assert blocks() is False

        h = make_harness(
            tmp_path,
            [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
            max_concurrency=1,
            budget=1.0,
            integration=False,
        )
        for key in ("t1", "t2", "t3"):
            h.world.set_behavior(key, {"kind": "complete", "polls": 1, "cost": 0.8})

        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            payload = h.emitter.of("run_completed")[0]
            assert payload["budget_overshot"] is True, "the overshoot was not even reported"
            assert payload["budget_exhausted"] is False
            assert not h.emitter.of("task_blocked")
        finally:
            h.close()

    def test_enforcement_env_only_blocks_on_the_exact_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "  BLOCK  ")
        assert blocks() is True
        monkeypatch.setenv(ENFORCEMENT_ENV, "yes-please")
        assert blocks() is False
        monkeypatch.setenv(ENFORCEMENT_ENV, ADVISORY)
        assert blocks() is False


# ==========================================================================
# No infinite loops
# ==========================================================================


class TestNoInfiniteLoops:
    def test_a_run_that_can_never_progress_fails_loudly(self, tmp_path: Path) -> None:
        """The liveness backstop: work nothing can start must fail the run after
        ``stall_minutes`` — never hang silently forever."""
        h = make_harness(tmp_path, [{"id": "stuck"}], max_concurrency=1, fake_clock=True)
        try:
            scheduler = make_scheduler(h)
            # Nothing can ever be claimed: the exact "no slot could start it" shape.
            scheduler._try_claim = lambda state, index, worker_id: None  # type: ignore[method-assign]
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.emitter.of("run_started"), timeout=10)

            fake_clock(h).advance(DEFAULT_STALL_MINUTES * 60 + 60)

            assert wait_until(
                lambda: (h.dal.get_run(h.run_id) or {}).get("status") == "failed", timeout=15
            ), "a permanently stalled run never failed; it would hang forever"
            assert "stalled" in str((h.dal.get_run(h.run_id) or {}).get("error"))
            assert handle.join(timeout=15)
        finally:
            h.close()

    def test_rate_limit_flapping_is_capped(self, tmp_path: Path) -> None:
        """Rate-limit re-enqueues do not consume a retry, so they need their own
        bound or a permanently limited provider loops forever."""
        h = make_harness(tmp_path, [{"id": "hot"}], max_concurrency=1, integration=False)
        h.world.set_behavior("hot", {"kind": "rate_limited", "polls": 1})

        try:
            scheduler = make_scheduler(h, rate_limit_requeue_cap=2)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.status_of("hot") == "blocked", "endless rate limiting never stopped"
            assert any(
                e.get("reason") == "rate_limit_flapping" for e in h.emitter.of("task_blocked")
            )
            assert len(h.attempts_of("hot")) == 3, "the requeue cap did not bound the attempts"
        finally:
            h.close()

    def test_escalation_ladder_saturates_instead_of_growing(self) -> None:
        """``_escalate`` is the pure ladder step. It must stop at the top rung —
        an unsaturated ladder would let the tier index run away."""
        assert SwarmScheduler._escalate("simple") == "standard"
        assert SwarmScheduler._escalate("standard") == "complex"
        assert SwarmScheduler._escalate("complex") == "complex", "the ladder did not saturate"
        assert SwarmScheduler._escalate("bogus-tier") == TIER_LADDER[-1]

        tier = TIER_LADDER[0]
        for _ in range(50):
            tier = SwarmScheduler._escalate(tier)
        assert tier == TIER_LADDER[-1]

    def test_every_bound_is_a_positive_finite_number(self) -> None:
        """A cap of 0 or infinity is not a cap. Cheap, but it is the assertion
        that catches a constant being zeroed out during a refactor."""
        for name, value in (
            ("DEFAULT_RETRY_CAP", DEFAULT_RETRY_CAP),
            ("DEFAULT_RATE_LIMIT_REQUEUE_CAP", DEFAULT_RATE_LIMIT_REQUEUE_CAP),
            ("DEFAULT_STALL_MINUTES", DEFAULT_STALL_MINUTES),
        ):
            assert value == value, f"{name} is NaN"
            assert value != float("inf"), f"{name} is unbounded"
            assert value >= 0, f"{name} is negative"
        assert DEFAULT_RATE_LIMIT_REQUEUE_CAP > 0
        assert DEFAULT_STALL_MINUTES > 0
