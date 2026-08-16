"""Regression coverage for capacity-deferred swarm worker hot loops."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.router import DurableRouterLimits, SwarmRouter
from omniagentos.swarm.scheduler import SwarmScheduler, _RunState
from tests.swarm.scheduler_fakes import FakeClock, make_harness, make_scheduler


def test_worker_poll_notification_does_not_advance_pass(tmp_path: Path) -> None:
    """An early condition notification is not a completed worker poll pass."""

    class NotifiedCondition:
        def __enter__(self) -> NotifiedCondition:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> bool:
            assert timeout == 0.5
            return True

    state = _RunState(run_id="run-notified", working_dir=str(tmp_path))
    state.cond = NotifiedCondition()  # type: ignore[assignment]

    SwarmScheduler._wait_for_worker_poll(state, 0.5)

    assert state.worker_poll_pass == 0


def test_ultra_cap_idle_poll_reuses_connection_and_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capped Ultra task gets one limits connection and one full wait per pass."""
    iterations = 6
    poll_interval = 0.5
    harness = make_harness(
        tmp_path,
        [{"id": f"task-{index}"} for index in range(4)],
        integration=False,
        target_n=4,
        max_concurrency=4,
    )
    task_ids = [harness.task_id(f"task-{index}") for index in range(4)]
    for task_id in task_ids:
        swarm_json = harness.dal.get_swarm_json(task_id) or {}
        swarm_json.update({"complexity": "standard", "speed": "ultra"})
        harness.dal.set_swarm_json(task_id, swarm_json)
    for index, task_id in enumerate(task_ids[:3]):
        assert harness.collab.claim_task(task_id, f"dead-worker-{index}", 0)
        harness.dal.open_attempt(
            harness.run_id,
            task_id,
            provider="claude",
            model="sonnet",
            tier="standard",
            session_id=f"ses_idle_{index}", source="test")

    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def counting_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    limits = DurableRouterLimits(harness.db_path)
    router = SwarmRouter(
        db_path=harness.db_path,
        limits=limits,
        rankings_loader=lambda: {},
        samples_loader=lambda: [],
        semantic_pins=False,
        lane_floors={},
        model_ladder=[],
        category_pins={},
        overflow=({}, 0.8),
        extra_candidates=[],
    )
    clock = FakeClock()
    scheduler = make_scheduler(
        harness,
        router=router,
        clock=clock,
        worker_poll_seconds=poll_interval,
    )
    state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
    state.target_n = 1
    waits: list[float] = []
    completed_waits = 0
    route_calls = 0
    real_route = router.route

    def bounded_route(task: dict[str, Any], tier: str) -> Any:
        nonlocal route_calls
        route_calls += 1
        decision = real_route(task, tier)
        # Negative-control guard: an unfixed tight loop still terminates
        # deterministically instead of hanging the test process.
        if route_calls > iterations:
            state.stopping = True
        return decision

    def fake_wait(wait_state: _RunState, timeout: float) -> None:
        nonlocal completed_waits
        waits.append(timeout)
        # A condition notification is not a completed poll interval. Simulate
        # one early notify and prove it cannot reopen the requeued task.
        if len(waits) == 1:
            return
        with wait_state.cond:
            wait_state.worker_poll_pass += 1
        completed_waits += 1
        clock.advance(timeout)
        if completed_waits >= iterations:
            wait_state.stopping = True

    monkeypatch.setattr(router, "route", bounded_route)
    monkeypatch.setattr(scheduler, "_wait_for_worker_poll", fake_wait)

    try:
        scheduler._worker(state, 0)

        assert route_calls == iterations
        assert len(opened) == 1, (
            f"router opened {len(opened)} SQLite connections across "
            f"{iterations} capacity-deferred poll passes"
        )
        assert waits == [poll_interval] * (iterations + 1)

        scheduler.shutdown()
        limits.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        scheduler.shutdown()
        limits.close()
        harness.dal.close()
        harness.collab._store.close()
