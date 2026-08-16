"""M-23 — bounded scale and backpressure gates (token-free)."""

from __future__ import annotations

import pytest

from omniagentos.testpolicy.policy_load import clear_policy_cache
from omniagentos.testpolicy.scale_gates import (
    ProjectContentionGate,
    SessionPool,
    run_bounded_scale_gate,
)


def setup_function() -> None:
    clear_policy_cache()


def test_session_pool_backpressure_rejects_over_capacity() -> None:
    pool = SessionPool(capacity=3)
    assert pool.try_admit("a").admitted
    assert pool.try_admit("b").admitted
    assert pool.try_admit("c").admitted
    denied = pool.try_admit("d")
    assert denied.admitted is False
    assert denied.reason == "backpressure_capacity"
    assert denied.active == 3
    pool.release()
    assert pool.try_admit("e").admitted is True


def test_project_contention_gate_serializes_writers() -> None:
    gate = ProjectContentionGate(writer_slots=1)
    first = gate.try_acquire("proj_1")
    second = gate.try_acquire("proj_1")
    other = gate.try_acquire("proj_2")
    assert first.admitted is True
    assert second.admitted is False
    assert second.reason == "project_writer_backpressure"
    assert other.admitted is True
    gate.release("proj_1")
    assert gate.try_acquire("proj_1").admitted is True


@pytest.mark.perf
@pytest.mark.s19b_load_contention
def test_bounded_scale_gate_certifies_100_sessions_and_10_projects() -> None:
    result = run_bounded_scale_gate(
        min_sessions=100,
        min_projects=10,
        admit_capacity=32,
        project_writer_slots=1,
        max_gate_seconds=30,
    )
    assert result.ok is True, result.reasons
    assert result.sessions_admitted + result.sessions_rejected >= 100
    assert result.projects == 10
    assert result.project_exclusive_ok is True
    assert result.details["overflow_rejected"] >= 32
    assert result.details["fill_admitted"] == 32
    assert result.elapsed_seconds < 30
