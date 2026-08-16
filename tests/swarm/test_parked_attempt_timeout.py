"""Tier timeouts fire on APPROVAL-PARKED attempts (parked-approval hang, cause #3).

Measured on bench run swr_8474e958870543388267: attempt seq=2 opened at
18:54:07Z was still open 47.6 minutes later against
``configs/swarm.yaml tier_timeout_minutes.standard = 30``. Its session was
parked in ``awaiting_approval`` for an approval that was never recorded, and
parking took the attempt out of the await loop — which was also the only place
the tier deadline was ever checked. The ladder therefore never fired.

The ladder itself is unchanged: first timeout escalates a rung, a second one
splits. These tests pin that it now fires on a parked attempt too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.swarm.scheduler import DEFAULT_TIMEOUT_MINUTES
from tests.swarm.scheduler_fakes import make_harness, make_scheduler, wait_until

STANDARD_TIMEOUT_S = DEFAULT_TIMEOUT_MINUTES["standard"] * 60.0


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


def _harness(tmp_path: Path):
    return make_harness(
        tmp_path,
        [{"id": "appr", "complexity": "standard", "est": 30}],
        max_concurrency=1,
        fake_clock=True,
    )


def test_parked_attempt_past_its_tier_timeout_is_closed_and_escalated(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.world.set_behavior("appr", {"kind": "await_approval", "polls": 1})
    try:
        scheduler = make_scheduler(h)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 1, timeout=10)
        session_id = h.emitter.of("approval_parked")[0]["session_id"]
        assert h.swarm_json_of("appr").get("current_tier") in (None, "standard")

        # Below the deadline the park is untouched: this is the resume-by-design
        # window, and nothing may reap it early.
        h.clock.advance(STANDARD_TIMEOUT_S - 60)
        assert not wait_until(
            lambda: h.attempts_of("appr")[0]["end_reason"] is not None, timeout=0.5
        )
        assert h.world.sessions[session_id]["state"] == "awaiting_approval"

        # Past it, the ladder fires on the PARKED attempt.
        h.clock.advance(120)
        assert wait_until(
            lambda: h.swarm_json_of("appr").get("current_tier") == "complex", timeout=10
        )
        first = h.attempts_of("appr")[0]
        assert "parked" in str(first["detail"])
        assert h.swarm_json_of("appr").get("current_tier") == "complex"  # ONE rung
        assert int(h.swarm_json_of("appr").get("timeout_count") or 0) == 1
        assert session_id in h.world.kills  # the orphaned worker is not left behind

        # …and the task is genuinely back on the market (claim released).
        assert wait_until(lambda: len(h.attempts_of("appr")) == 2, timeout=10)
    finally:
        h.close()


def test_resumed_attempt_inherits_its_original_deadline(tmp_path: Path) -> None:
    """A park must not launder an attempt past its tier timeout: the re-attach
    after an approval resolution continues the ORIGINAL deadline."""
    h = _harness(tmp_path)
    h.world.set_behavior("appr", {"kind": "await_approval", "polls": 1})
    try:
        scheduler = make_scheduler(h)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 1, timeout=10)
        session_id = h.emitter.of("approval_parked")[0]["session_id"]

        h.clock.advance(STANDARD_TIMEOUT_S - 60)
        h.world.resolve(session_id, "running")  # approved: back to work, 1 min left
        assert wait_until(lambda: len(h.attempts_of("appr")) == 1, timeout=5)

        h.clock.advance(120)  # past the ORIGINAL deadline, not a fresh 30 minutes
        assert wait_until(lambda: h.attempts_of("appr")[0]["end_reason"] == "timeout", timeout=10)
        assert "parked" not in str(h.attempts_of("appr")[0]["detail"])  # it was running
    finally:
        h.close()


def test_second_parked_timeout_splits_per_the_existing_ladder(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.world.set_behavior("appr", {"kind": "await_approval", "polls": 1})
    splits: list[str] = []
    try:
        scheduler = make_scheduler(
            h,
            splitter=lambda task, swarm_json: splits.append(str(task["id"])) or None,
        )
        handle = scheduler.start_run(h.run_id)
        assert handle is not None

        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 1, timeout=10)
        h.clock.advance(STANDARD_TIMEOUT_S + 1)
        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 2, timeout=10)

        # Second timeout on the (now complex-tier) parked attempt → SPLIT.
        h.clock.advance(DEFAULT_TIMEOUT_MINUTES["complex"] * 60.0 + 1)
        assert wait_until(
            lambda: any(a["end_reason"] == "split" for a in h.attempts_of("appr")), timeout=10
        )
        # Eventual-consistency read: the scheduler thread commits the split and
        # the timeout_count bump as two ordered writes; wait for the second.
        assert wait_until(
            lambda: int(h.swarm_json_of("appr").get("timeout_count") or 0) == 2, timeout=10
        )
        assert splits == [h.task_id("appr")]  # the splitter WAS consulted
        # Splitter declined → blocked, never a silent third park.
        assert wait_until(lambda: h.status_of("appr") == "blocked", timeout=10)
    finally:
        h.close()


def test_regression_bench_shape_run_does_not_stay_running_forever(tmp_path: Path) -> None:
    """The measured failure, reproduced: standard tier (30m), session parked in
    awaiting_approval with the approval never delivered, attempt open 47 minutes."""
    h = _harness(tmp_path)
    h.world.set_behavior("appr", {"kind": "await_approval", "polls": 1})
    try:
        scheduler = make_scheduler(h)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 1, timeout=10)
        session_id = h.emitter.of("approval_parked")[0]["session_id"]
        assert h.world.sessions[session_id]["state"] == "awaiting_approval"

        # 47.6 minutes of wall clock against a 30-minute tier, exactly as measured.
        h.clock.advance(47.6 * 60.0)

        # The attempt does NOT stay open, and the run reaches a terminal status
        # instead of hanging with an orphaned worker.
        assert wait_until(lambda: h.attempts_of("appr")[0]["end_reason"] is not None, timeout=10)
        # The successor attempt parks the same way; the ladder spends its second
        # rung and the run terminalizes rather than idling forever.
        assert wait_until(lambda: len(h.emitter.of("approval_parked")) == 2, timeout=10)
        h.clock.advance(DEFAULT_TIMEOUT_MINUTES["complex"] * 60.0 + 1)
        assert handle.join(timeout=30)
        status = str(h.dal.get_run(h.run_id)["status"])
        assert status != "running"
        assert status in {"completed", "failed", "cancelled"}
    finally:
        h.close()
