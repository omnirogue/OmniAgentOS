"""Scheduler timeout decisions are gated by provider-session liveness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import omniagentos.swarm.scheduler as scheduler_module
from omniagentos.swarm.provider_exec import is_making_progress as read_liveness
from omniagentos.swarm.scheduler import IDLE_TIMEOUT_FRACTION, _RunState
from tests.swarm.scheduler_fakes import make_harness, make_scheduler


def _capture_real_liveness(
    monkeypatch: pytest.MonkeyPatch,
    session_store: Any,
    now: datetime,
) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    def check(
        session_id: str,
        idle_threshold_seconds: float,
        *,
        dal: Any,
    ) -> dict[str, Any]:
        assert dal is session_store
        calls.append((session_id, idle_threshold_seconds))
        return read_liveness(
            session_id,
            idle_threshold_seconds,
            dal=session_store,
            now_iso=now.isoformat(),
        )

    monkeypatch.setattr(scheduler_module, "is_making_progress", check)
    return calls


def test_recent_activity_skips_expired_deadline_and_polls_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(tmp_path, [{"id": "slow"}], max_concurrency=1)
    now = datetime.now(UTC)
    session_id = h.world.add_session(
        behavior={"kind": "complete", "polls": 3, "output": "finished after deadline"}
    )
    h.world.sessions[session_id]["last_activity_at"] = (now - timedelta(seconds=5)).isoformat()
    calls = _capture_real_liveness(monkeypatch, h.world, now)
    scheduler = make_scheduler(h, await_poll_seconds=0.0)
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
    settled: list[str] = []

    def settle(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        settled.append(session_id)
        return "settled"

    def unexpected_timeout(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        pytest.fail("a recently active session must not enter _handle_timeout")

    monkeypatch.setattr(scheduler, "_settle_terminal", settle)
    monkeypatch.setattr(scheduler, "_handle_timeout", unexpected_timeout)

    try:
        result = scheduler._await_and_settle(
            state,
            dict(h.task_row("slow")),
            "attempt-slow",
            session_id,
            "simple",
            "snapshot",
            deadline=-1.0,
        )

        expected_threshold = scheduler._timeout_seconds("simple") * IDLE_TIMEOUT_FRACTION
        assert result == "settled"
        assert calls == [(session_id, expected_threshold)]
        assert settled == [session_id]
        assert h.world.kills == []
    finally:
        h.close()


def test_stale_activity_uses_normal_timeout_kill_and_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(tmp_path, [{"id": "stalled"}], max_concurrency=1)
    now = datetime.now(UTC)
    session_id = h.world.add_session(behavior={"kind": "hang"})
    scheduler = make_scheduler(h, await_poll_seconds=0.0)
    expected_threshold = scheduler._timeout_seconds("simple") * IDLE_TIMEOUT_FRACTION
    h.world.sessions[session_id]["last_activity_at"] = (
        now - timedelta(seconds=expected_threshold + 1)
    ).isoformat()
    calls = _capture_real_liveness(monkeypatch, h.world, now)
    task_id = h.task_id("stalled")
    attempt = h.dal.open_attempt(
        h.run_id,
        task_id,
        provider="claude",
        model="sonnet",
        tier="simple",
        session_id=session_id,
    )
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))

    try:
        result = scheduler._await_and_settle(
            state,
            dict(h.task_row("stalled")),
            str(attempt["id"]),
            session_id,
            "simple",
            "snapshot",
            deadline=-1.0,
        )

        assert result == "requeue"
        assert calls == [(session_id, expected_threshold)]
        assert h.world.kills == [session_id]
        assert h.attempts_of("stalled")[0]["end_reason"] == "timeout"
        assert h.swarm_json_of("stalled")["current_tier"] == "standard"
    finally:
        h.close()
