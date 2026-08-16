from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.northstar.collectors import (
    AllocationMeasurement,
    record_allocation_frontier_report,
    record_phase_baseline,
    record_provisioning_latency,
    record_rediscovery_event,
    record_tool_set_digest,
)
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


@pytest.fixture
def migrated_connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = migrated_db(CollabStore, tmp_path / "northstar-collectors.sqlite3")
    connection = sqlite3.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.nsc_writer_proof("writer:rediscovery-event-trace-collector")
def test_rediscovery_event_trace_collector_persists_specific_carrier(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection

    trace_id = record_rediscovery_event(
        connection,
        attempt_id="swa_rediscovery",
        context_key="decision:database-owner",
        first_seen_at="2026-08-12T10:00:00Z",
        rediscovered_at="2026-08-12T10:03:00Z",
        source_digest="a" * 64,
        trace={"trigger": "context_miss", "query": "database owner"},
    )

    row = connection.execute(
        "SELECT attempt_id, context_key, source_digest, trace_json "
        "FROM rediscovery_event_traces WHERE id = ?",
        (trace_id,),
    ).fetchone()
    assert row == (
        "swa_rediscovery",
        "decision:database-owner",
        "a" * 64,
        '{"query":"database owner","trigger":"context_miss"}',
    )


@pytest.mark.nsc_writer_proof("writer:provisioning-latency-collector")
def test_provisioning_latency_collector_persists_measured_interval(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection

    event_id = record_provisioning_latency(
        connection,
        attempt_id="swa_provision",
        resource_kind="worktree",
        requested_at="2026-08-12T10:00:00.000Z",
        ready_at="2026-08-12T10:00:01.250Z",
        outcome="ready",
        detail={"path_class": "private-worktree"},
    )

    assert connection.execute(
        "SELECT resource_kind, latency_ms, outcome, detail_json "
        "FROM provisioning_latency_events WHERE id = ?",
        (event_id,),
    ).fetchone() == ("worktree", 1250, "ready", '{"path_class":"private-worktree"}')


@pytest.mark.nsc_writer_proof("writer:phase-baseline")
def test_phase_baseline_writer_persists_complete_phase_distribution(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection
    anchors = {
        "accepted": 0,
        "planned": 2,
        "first_useful_work": 5,
        "lanes_complete": 9,
        "integrated": 11,
        "gated": 12,
        "reviewed": 14,
        "completed": 15,
    }

    baseline_id = record_phase_baseline(
        connection, run_id="swr_phase", baseline_key="default", anchors=anchors
    )

    total, phases = connection.execute(
        "SELECT total_seconds, phase_seconds_json FROM northstar_phase_baselines WHERE id = ?",
        (baseline_id,),
    ).fetchone()
    assert total == 15
    assert '"planned_to_first_useful_work":3.0' in phases


@pytest.mark.nsc_writer_proof("writer:allocation-frontier-report")
def test_allocation_frontier_report_writer_persists_every_cost_axis(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection
    frontier = AllocationMeasurement("frontier", 1, 2, 0.10, 8, 0.9)
    dominated = AllocationMeasurement("dominated", 2, 3, 0.20, 9, 0.8)
    tradeoff = AllocationMeasurement("tradeoff", 1, 1, 0.05, 12, 0.95)

    report_id = record_allocation_frontier_report(
        connection,
        report_key="portfolio-2026-08-12",
        configurations=[frontier, dominated, tradeoff],
        created_at="2026-08-12T12:00:00Z",
    )

    assert connection.execute(
        "SELECT config_id, formation_selections, swarm_attempts, session_cost_usd, "
        "completion_seconds, quality_score, dominated FROM allocation_frontier_points "
        "WHERE report_id = ? ORDER BY config_id",
        (report_id,),
    ).fetchall() == [
        ("dominated", 2, 3, 0.2, 9.0, 0.8, 1),
        ("frontier", 1, 2, 0.1, 8.0, 0.9, 0),
        ("tradeoff", 1, 1, 0.05, 12.0, 0.95, 0),
    ]


def test_allocation_frontier_report_rolls_back_all_rows_on_point_failure(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection
    connection.execute(
        "CREATE TEMP TRIGGER reject_second_frontier_point "
        "BEFORE INSERT ON allocation_frontier_points "
        "WHEN NEW.config_id = 'rejected' "
        "BEGIN SELECT RAISE(ABORT, 'forced point failure'); END"
    )
    configurations = [
        AllocationMeasurement("accepted", 1, 1, 0.1, 1, 0.9),
        AllocationMeasurement("rejected", 2, 2, 0.2, 2, 0.8),
    ]

    with pytest.raises(sqlite3.IntegrityError, match="forced point failure"):
        record_allocation_frontier_report(
            connection,
            report_key="must-rollback",
            configurations=configurations,
            created_at="2026-08-12T12:00:00Z",
        )

    assert connection.execute(
        "SELECT COUNT(*) FROM allocation_frontier_reports WHERE report_key = 'must-rollback'"
    ).fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM allocation_frontier_points").fetchone() == (0,)


@pytest.mark.nsc_writer_proof("writer:tool-set-digest")
def test_tool_set_digest_writer_persists_canonical_per_attempt_field(
    migrated_connection: sqlite3.Connection,
) -> None:
    connection = migrated_connection
    connection.execute(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, provider, model, source, started_at, detail) "
        "VALUES ('swa_tools', 'swr_tools', 'btk_tools', 0, 'test', 'test', 'test', "
        "'2026-08-12T12:00:00Z', '')"
    )

    digest = record_tool_set_digest(
        connection,
        attempt_id="swa_tools",
        tool_names=["shell.exec", "files.read", "shell.exec"],
    )

    assert len(digest) == 64
    assert connection.execute(
        "SELECT tool_set_digest FROM swarm_attempts WHERE id = 'swa_tools'"
    ).fetchone() == (digest,)
    assert digest == record_tool_set_digest(
        connection,
        attempt_id="swa_tools",
        tool_names=["files.read", "shell.exec"],
    )


@pytest.mark.nsc_writer_proof("writer:tool-set-digest-live-open-attempt")
def test_open_attempt_persists_canonical_tool_set_digest(tmp_path: Path) -> None:
    db_path = migrated_db(CollabStore, tmp_path / "open-attempt.sqlite3")
    collab = CollabStore(db_path)
    dal = SwarmDal(db_path)
    try:
        run_id = str(
            dal.create_run(working_dir="/tmp/ws", goal="digest proof", source="test")["id"]
        )
        task = BoardTask(title="digest proof", status=BoardTaskStatus.OPEN)
        collab.create_board_task(task)
        assert dal.assign_task_to_run(task.id, run_id) is True

        attempt = dal.open_attempt(
            run_id,
            task.id,
            provider="test",
            model="test",
            source="test",
            tool_names=["shell.exec", "files.read", "shell.exec"],
        )

        row = dal._connection.execute(
            "SELECT tool_set_digest FROM swarm_attempts WHERE id = ?", (attempt["id"],)
        ).fetchone()
        assert row is not None
        expected_digest = hashlib.sha256(b'["files.read","shell.exec"]').hexdigest()
        assert attempt["tool_set_digest"] == expected_digest
        assert row["tool_set_digest"] == expected_digest
    finally:
        dal.close()
