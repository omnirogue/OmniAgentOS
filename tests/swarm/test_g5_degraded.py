"""Tests for G5 gate degradation handling and event recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.contracts import ACTION_GATE_DEGRADED
from tests.swarm.scheduler_fakes import (
    make_harness,
    make_scheduler,
)


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch in these tests must go through the harness fixtures."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


def test_g5_degraded_on_exception_and_mechanical_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that when GateService.g5_local_verify raises an exception:

    1. An ACTION_GATE_DEGRADED event is emitted with proper payload.
    2. The mechanical verdict 'pass' survives (does not fail closed).
    """
    h = make_harness(
        tmp_path,
        [{"id": "t1", "complexity": "simple"}],
        max_concurrency=1,
        integration=False,
    )

    # Monkeypatch g5_local_verify to raise an exception
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("G5 service simulated failure")

    monkeypatch.setattr("omniagentos.gates.service.GateService.g5_local_verify", boom)

    # Reviewer confirms the attempt
    h.reviewer.set_script("t1", "confirm")

    # Verifier always returns True (mechanical pass)
    def verifier(task: Any, swarm_json: Any, working_dir: Any) -> tuple[bool, str]:
        return True, "mechanical verification passed"

    try:
        scheduler = make_scheduler(h, verifier=verifier)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        # 1. Check that the event ACTION_GATE_DEGRADED was emitted
        degraded_events = h.emitter.of(ACTION_GATE_DEGRADED)
        assert len(degraded_events) == 1
        event = degraded_events[0]
        assert event["gate"] == "g5_local_verify"
        assert "ValueError: G5 service simulated failure" in event["reason"]
        assert event["mechanical_verdict"] == "pass"
        assert event["task_id"] == h.task_id("t1")

        # 2. Check that the mechanical pass stood (the task completed successfully)
        assert h.status_of("t1") == "done"
        attempts = h.attempts_of("t1")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "completed"

    finally:
        h.close()


def test_g5_degraded_on_exception_and_mechanical_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that when GateService.g5_local_verify raises an exception and mechanical verify fails:

    1. An ACTION_GATE_DEGRADED event is emitted with mechanical_verdict='fail'.
    2. The attempt is closed with 'review_denied'.
    3. The attempt row's detail field contains the g5_degraded marker.
    """
    h = make_harness(
        tmp_path,
        [{"id": "t2", "complexity": "simple"}],
        max_concurrency=1,
        integration=False,
    )

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("G5 service simulated failure")

    monkeypatch.setattr("omniagentos.gates.service.GateService.g5_local_verify", boom)

    # Verifier returns False (mechanical fail)
    def verifier(task: Any, swarm_json: Any, working_dir: Any) -> tuple[bool, str]:
        return False, "mechanical verification failed"

    try:
        scheduler = make_scheduler(h, verifier=verifier)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        # 1. Check that ACTION_GATE_DEGRADED was emitted with mechanical_verdict='fail'.
        #    A mechanical failure is retried (same-tier retry, then escalation), and each
        #    attempt runs its own G5 call — so every attempt contributes one degradation
        #    event. Assert on the shape of every event rather than pinning the retry count.
        degraded_events = h.emitter.of(ACTION_GATE_DEGRADED)
        assert len(degraded_events) >= 1
        for event in degraded_events:
            assert event["gate"] == "g5_local_verify"
            assert "ValueError: G5 service simulated failure" in event["reason"]
            assert event["mechanical_verdict"] == "fail"

        # 2. Check that the task is NOT done (gets mechanical retry / review denied)
        attempts = h.attempts_of("t2")
        # In scheduler.py, a mechanical verify failure gets ONE same-tier retry,
        # so it will run a second attempt. Let's inspect the first attempt.
        assert len(attempts) >= 1
        first_attempt = attempts[0]
        assert first_attempt["end_reason"] == "review_denied"

        # 3. Check that detail contains the g5_degraded marker
        detail = first_attempt["detail"]
        assert "g5_degraded: ValueError: G5 service simulated failure" in detail

    finally:
        h.close()


def test_g5_healthy_control(tmp_path: Path) -> None:
    """Test that when GateService is healthy, no ACTION_GATE_DEGRADED event is emitted."""
    h = make_harness(
        tmp_path,
        [{"id": "t3", "complexity": "simple"}],
        max_concurrency=1,
        integration=False,
    )
    h.reviewer.set_script("t3", "confirm")

    try:
        scheduler = make_scheduler(h)
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        # No degradation event emitted
        degraded_events = h.emitter.of(ACTION_GATE_DEGRADED)
        assert len(degraded_events) == 0

        assert h.status_of("t3") == "done"
    finally:
        h.close()
