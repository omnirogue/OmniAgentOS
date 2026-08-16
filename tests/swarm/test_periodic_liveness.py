"""Liveness is classified on a cadence, not only once the deadline has passed.

``is_making_progress`` used to have exactly one call site, inside
``if self._clock.monotonic() >= deadline``. So "is this agent stuck?" was only
ever answered after the attempt had already burned its whole tier budget, even
though the freshness signal it reads (``sessions.last_activity_at``, written per
output chunk) was available the entire time.

These tests pin the periodic tick that closes that gap, on both carriers (the
running await loop AND the approval-parked poll, which has no worker thread at
all), and pin the two properties that keep it affordable and safe: change-only
emission, and observe-only (a pre-deadline tick never kills).

Sibling suites: ``test_scheduler_liveness.py`` covers the DEADLINE-time kill
decision, which this change deliberately leaves alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import omniagentos.swarm.scheduler as scheduler_module
from omniagentos.swarm.scheduler import SwarmScheduler, _RunState
from tests.swarm.scheduler_fakes import Harness, make_harness, make_scheduler


class _StubDal:
    """Minimal session-store stand-in: one row (or none) for any id."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        del session_id
        return self._row


def _script_classifier(
    monkeypatch: pytest.MonkeyPatch, statuses: list[str]
) -> list[tuple[str, str]]:
    """Drive the scheduler's classifier from a script, recording every call.

    The scheduler's CADENCE and EMISSION are what these tests pin; the
    classification itself is pinned separately (see
    ``test_classify_liveness_vocabulary``) so a cadence regression cannot hide
    behind a classifier change or vice versa.
    """
    calls: list[tuple[str, str]] = []
    queue = list(statuses)

    def classify(
        session_id: str,
        idle_threshold_seconds: float,
        *,
        dal: Any = None,
        session_state: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del dal, kwargs
        status = queue.pop(0) if queue else statuses[-1]
        calls.append((session_id, status))
        return {
            "session_id": session_id,
            "status": status,
            "is_making_progress": status in ("active", "slow", "approval_waiting"),
            "last_activity_seconds_ago": 1.0,
            "idle_threshold_seconds": idle_threshold_seconds,
            "details": f"scripted {status}",
        }

    monkeypatch.setattr(scheduler_module, "classify_liveness", classify, raising=False)
    return calls


def _scheduler_with_tick(h: Harness) -> SwarmScheduler:
    """Scheduler with the liveness cadence forced to every await poll.

    The ``TypeError`` fallback is deliberate and load-bearing for RED-FIRST
    reproduction: run against pre-fix sources (no ``liveness_poll_seconds``
    knob) these tests must fail on the MISSING BEHAVIOUR — no classification,
    no events — not on a constructor signature. A structural failure would not
    prove the defect.
    """
    try:
        return make_scheduler(h, await_poll_seconds=0.0, liveness_poll_seconds=0.0)
    except TypeError:  # pragma: no cover -- only reachable on pre-fix sources
        scheduler = make_scheduler(h, await_poll_seconds=0.0)
        scheduler._liveness_poll_seconds = 0.0  # type: ignore[attr-defined]
        return scheduler


def test_liveness_is_classified_before_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy in-flight attempt is classified while it still has budget."""
    h = make_harness(tmp_path, [{"id": "live"}], max_concurrency=1)
    session_id = h.world.add_session(behavior={"kind": "complete", "polls": 5})
    h.world.sessions[session_id]["last_activity_at"] = datetime.now(UTC).isoformat()
    calls = _script_classifier(monkeypatch, ["active"] * 8)
    scheduler = _scheduler_with_tick(h)
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
    monkeypatch.setattr(scheduler, "_settle_terminal", lambda *a, **k: "settled")

    def unexpected_timeout(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        pytest.fail("a pre-deadline tick must never reach the timeout ladder")

    monkeypatch.setattr(scheduler, "_handle_timeout", unexpected_timeout)

    try:
        result = scheduler._await_and_settle(
            state,
            dict(h.task_row("live")),
            "attempt-live",
            session_id,
            "simple",
            "snapshot",
            # Far-future deadline: nothing here may depend on it being reached.
            deadline=scheduler._clock.monotonic() + 10_000.0,
        )

        assert result == "settled"
        assert len(calls) >= 1, "no pre-deadline liveness classification ran"
        assert {sid for sid, _ in calls} == {session_id}
        assert h.world.kills == [], "a pre-deadline tick is observe-only"
    finally:
        h.close()


def test_liveness_events_are_emitted_once_per_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change-only emission: six ticks over two states emit two events.

    The whole point of a bounded-sampling classifier is that a long healthy
    attempt costs ONE event, not one per tick.
    """
    h = make_harness(tmp_path, [{"id": "flip"}], max_concurrency=1)
    session_id = h.world.add_session(behavior={"kind": "complete", "polls": 6})
    h.world.sessions[session_id]["last_activity_at"] = datetime.now(UTC).isoformat()
    _script_classifier(monkeypatch, ["active", "active", "active", "stalled", "stalled", "stalled"])
    scheduler = _scheduler_with_tick(h)
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
    monkeypatch.setattr(scheduler, "_settle_terminal", lambda *a, **k: "settled")

    try:
        scheduler._await_and_settle(
            state,
            dict(h.task_row("flip")),
            "attempt-flip",
            session_id,
            "simple",
            "snapshot",
            deadline=scheduler._clock.monotonic() + 10_000.0,
        )

        changed = h.emitter.of("liveness_changed")
        assert [p["status"] for p in changed] == ["active", "stalled"], changed
        assert changed[0]["previous"] is None
        assert changed[1]["previous"] == "active"
        # Recorded, NOT acted on: the tick says what a pre-deadline reaper
        # would have done, and the wall deadline still owns every kill.
        assert changed[0]["would_reap"] is False
        assert changed[1]["would_reap"] is True
        assert h.world.kills == [], "a stalled classification must not kill"

        # The false-reap instrument: numerator and denominator, keyed by the
        # attempt id so the join to the attempt's end_reason is exact.
        summary = h.emitter.of("liveness_summary")
        assert len(summary) == 1, summary
        assert summary[0]["attempt_id"] == "attempt-flip"
        assert summary[0]["stalled_ticks"] >= 1
        assert summary[0]["ticks"] >= 4
        assert summary[0]["ticks"] == sum(summary[0]["counts"].values())
    finally:
        h.close()


def test_parked_attempt_is_classified_before_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parked carrier: an approval-parked attempt has no worker thread.

    Nothing awaits it, so without a tick on the reconcile pass its state is
    invisible until its deadline fires — the same defect as the running case,
    one loop over. Its silence is EXPECTED, so it classifies as
    ``approval_waiting`` and never as a stall.
    """
    del monkeypatch  # the real classifier runs here, unscripted
    h = make_harness(tmp_path, [{"id": "parked"}], max_concurrency=1)
    session_id = h.world.add_session(state="awaiting_approval")
    scheduler = _scheduler_with_tick(h)
    state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
    task_id = h.task_id("parked")
    state.parked[task_id] = {
        "task_id": task_id,
        "attempt_id": "attempt-parked",
        "session_id": session_id,
        "tier": "simple",
        "deadline": scheduler._clock.monotonic() + 10_000.0,
    }

    try:
        scheduler._reconcile(state)
        scheduler._reconcile(state)  # a second pass must NOT re-emit

        changed = h.emitter.of("liveness_changed")
        assert [p["status"] for p in changed] == ["approval_waiting"], changed
        assert changed[0]["would_reap"] is False
        assert changed[0]["attempt_id"] == "attempt-parked"
        assert task_id in state.parked, "an observation must not unpark anything"
        assert h.world.kills == []
    finally:
        h.close()


def test_classify_liveness_vocabulary() -> None:
    """active / slow / stalled / unknown / approval_waiting, off one signal."""
    # Imported HERE, not at module scope, on purpose: a module-level import of
    # symbols this change introduces would turn the three scheduler tests above
    # into collection errors on pre-fix sources, and a red that is an
    # ImportError proves nothing about behaviour. This test is the one that
    # legitimately pins the new API, so it is the one that carries the import.
    from omniagentos.swarm.provider_exec import (
        LIVENESS_ACTIVE_FRACTION,
        LIVENESS_STATUSES,
        classify_liveness,
    )

    now = datetime.now(UTC)
    threshold = 100.0
    boundary = threshold * LIVENESS_ACTIVE_FRACTION

    def status_for(seconds_ago: float, **kwargs: Any) -> str:
        row = {
            "id": "s",
            "last_activity_at": (now - timedelta(seconds=seconds_ago)).isoformat(),
        }
        return str(
            classify_liveness("s", threshold, dal=_StubDal(row), now_iso=now.isoformat(), **kwargs)[
                "status"
            ]
        )

    assert status_for(1.0) == "active"
    assert status_for(boundary - 1.0) == "active"
    # At the active boundary it is "slow": still alive, but worth watching.
    assert status_for(boundary + 1.0) == "slow"
    assert status_for(threshold - 1.0) == "slow"
    assert status_for(threshold + 1.0) == "stalled"
    # A parked session is silent BY DESIGN — never read that as a stall, no
    # matter how stale the activity mark is.
    assert status_for(10_000.0, session_state="awaiting_approval") == "approval_waiting"

    missing = classify_liveness("s", threshold, dal=_StubDal(None), now_iso=now.isoformat())
    assert missing["status"] == "unknown"
    # Fail-safe: an unusable threshold is an unknown, never an exception into
    # the poll loop and never a stall.
    assert classify_liveness("s", 0.0, dal=_StubDal(None))["status"] == "unknown"
    assert set(LIVENESS_STATUSES) == {
        "active",
        "slow",
        "approval_waiting",
        "stalled",
        "unknown",
    }
