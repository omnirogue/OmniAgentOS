"""The two-sided usage join: process-side spend meets scheduler-side effort.

Usage is written in two places because neither writer can see the other's half.
The spawned process knows what it SPENT (tokens, cost, wall-clock) and writes to
the session it owns; the scheduler knows what it CHOSE (effort, tier) and writes
to the attempt. `attempts_with_usage` is where the two meet, and this file pins
that seam — a silent failure there gives cost-to-green a dataset with an
independent variable and no dependent one, or vice versa.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.costgreen import summarize_run
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "join.db")
    CollabStore(db)  # migrates the shared schema
    return db


def _card(collab: CollabStore, dal: SwarmDal, title: str, run_id: str) -> str:
    task = BoardTask(title=title, status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    dal.assign_task_to_run(task.id, run_id)  # run membership is the DAL's to set
    return task.id


def _session(sessions: SessionsDal, session_id: str) -> None:
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": "/tmp/ws",
            "provider": "claude",
            "state": "starting",
            "model": "opus",
            "created_at": "2026-07-23T10:00:00Z",
            "updated_at": "2026-07-23T10:00:00Z",
            "cost_usd": 0.0,
        }
    )


def test_session_spend_joins_onto_scheduler_effort(db_path: str) -> None:
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    sessions = SessionsDal(db_path)
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
    task_id = _card(collab, dal, "package A", run_id)
    _session(sessions, "ses_a")

    attempt = dal.open_attempt(run_id, task_id, provider="claude", model="opus", session_id="ses_a", source="test")
    # Scheduler side: what we CHOSE.
    dal.record_attempt_usage(str(attempt["id"]), effort="xhigh")
    # Process side: what it SPENT.
    sessions.record_session_usage(
        "ses_a",
        cost_usd=2.5,
        input_tokens=1_000,
        output_tokens=500,
        wall_ms=30_000,
        usage_source=SOURCE_CLI_REPORT,
    )
    dal.close_attempt(str(attempt["id"]), "completed")

    joined = dal.attempts_with_usage(run_id)

    assert len(joined) == 1
    row = joined[0]
    assert row["effort"] == "xhigh"  # from the attempt
    assert row["cost_usd"] == 2.5  # from the session
    assert row["input_tokens"] == 1_000
    assert row["wall_ms"] == 30_000
    assert row["usage_source"] == SOURCE_CLI_REPORT


def test_attempt_value_wins_over_the_session_backfill(db_path: str) -> None:
    """A caller that wrote directly onto the attempt meant it."""
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    sessions = SessionsDal(db_path)
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
    task_id = _card(collab, dal, "package A", run_id)
    _session(sessions, "ses_a")
    sessions.record_session_usage("ses_a", cost_usd=9.9, effort="low")

    attempt = dal.open_attempt(run_id, task_id, provider="claude", model="opus", session_id="ses_a", source="test")
    dal.record_attempt_usage(str(attempt["id"]), effort="xhigh", cost_usd=1.0)

    row = dal.attempts_with_usage(run_id)[0]

    assert row["effort"] == "xhigh"
    assert row["cost_usd"] == 1.0


def test_attempt_without_a_session_keeps_nulls_not_zeros(db_path: str) -> None:
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
    task_id = _card(collab, dal, "package A", run_id)

    dal.open_attempt(run_id, task_id, provider="grok", model="grok-4.5", source="test")

    row = dal.attempts_with_usage(run_id)[0]

    assert row["cost_usd"] is None  # not 0.0 — grok reports nothing
    assert row["input_tokens"] is None
    assert row["usage_source"] is None


def test_cost_to_green_reads_the_joined_view_end_to_end(db_path: str) -> None:
    """The whole point: a retry chain priced across both writers."""
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    sessions = SessionsDal(db_path)
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
    task_id = _card(collab, dal, "package A", run_id)

    for index, (session_id, cost, reason, model) in enumerate(
        [("ses_1", 1.0, "crashed", "sonnet"), ("ses_2", 4.0, "completed", "opus")]
    ):
        _session(sessions, session_id)
        attempt = dal.open_attempt(
            run_id, task_id, provider="claude", model=model, session_id=session_id, source="test")
        # Both attempts started from the medium policy; the second escalated.
        dal.record_attempt_usage(str(attempt["id"]), effort="medium" if index == 0 else "xhigh")
        sessions.record_session_usage(
            session_id,
            cost_usd=cost,
            input_tokens=100,
            output_tokens=100,
            wall_ms=1_000,
            usage_source=SOURCE_CLI_REPORT,
        )
        dal.close_attempt(str(attempt["id"]), reason)

    stats = summarize_run(dal, run_id)

    assert len(stats) == 1
    assert stats[0].effort == "medium"  # charged to where the chain started
    assert stats[0].total_cost_usd == 5.0  # the failure is included
    assert stats[0].total_escalations == 1
    assert stats[0].green == 1
    assert stats[0].cost_per_green == 5.0
    assert stats[0].confidence == "measured"
