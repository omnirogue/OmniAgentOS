"""Tests for the task-shape routing decisions log persistence layer."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from omniagentos.allocation.characterize import characterize
from omniagentos.allocation.decisions import record_decision
from omniagentos.db.store import SqliteStore


def test_record_decision_writes_row() -> None:
    # 1. Writes a row — call record_decision with a real TaskCharacterization
    # built via characterize, then SELECT the row back and assert.
    store = SqliteStore(":memory:")
    conn = store._connection

    char = characterize(
        {
            "D": 0.5,
            "I": 0.6,
            "sequential": True,  # S
            "uncertainty": 0.8,  # U
            "verifiable": 0.9,  # V
            "G": 0.4,
            "critical": 0.3,  # C
            "multi_specialty": 0.2,  # M
            "risk": 0.1,  # R
            "knowledge_heavy": 0.05,  # K
            "work_volume": 10.0,  # W
            "urgency": 5.0,  # P
        }
    )

    brief = "test brief for shape routing decisions persistence"
    row_id = record_decision(
        conn,
        brief=brief,
        route="solo_strong",
        topology="sequential",
        worker_count=2,
        rationale="straightforward tasks",
        applied=True,
        char=char,
        tool_density=0.15,
        context_breadth=0.5,
        merge_cost=1.2,
        shared_state_coupling=0.8,
        latency_ms=120.5,
        cbm_parallel_candidates=3,
    )

    assert row_id is not None

    row = conn.execute("SELECT * FROM task_shape_decisions WHERE id = ?", (row_id,)).fetchone()

    assert row is not None
    assert row["route"] == "solo_strong"
    assert row["topology"] == "sequential"
    assert row["worker_count"] == 2
    assert row["rationale"] == "straightforward tasks"
    assert row["applied"] == 1
    assert row["latency_ms"] == 120.5
    assert row["cbm_parallel_candidates"] == 3

    # Assert 12 feature columns are populated and match char attributes
    assert row["feat_d"] == char.D
    assert row["feat_i"] == char.I
    assert row["feat_s"] == char.S
    assert row["feat_u"] == char.U
    assert row["feat_v"] == char.V
    assert row["feat_g"] == char.G
    assert row["feat_c"] == char.C
    assert row["feat_m"] == char.M
    assert row["feat_r"] == char.R
    assert row["feat_k"] == char.K
    assert row["feat_w"] == char.W
    assert row["feat_p"] == char.P

    # Assert confidence and task_class
    assert row["confidence"] == char.confidence
    assert json.loads(row["task_class"]) == char.task_class

    # Assert brief_hash equals sha1 of the brief
    expected_hash = hashlib.sha1(brief.encode("utf-8")).hexdigest()
    assert row["brief_hash"] == expected_hash


def test_record_decision_refs_stored_separately() -> None:
    # 2. Both refs are stored separately — pass board_task_id="btk_x" and
    # run_id="swr_y" and assert each column holds its own value.
    store = SqliteStore(":memory:")
    conn = store._connection

    row_id = record_decision(
        conn,
        brief="separate refs check",
        route="centralized_team",
        board_task_id="btk_x",
        run_id="swr_y",
    )
    assert row_id is not None

    row = conn.execute(
        "SELECT board_task_id, run_id FROM task_shape_decisions WHERE id = ?",
        (row_id,),
    ).fetchone()

    assert row is not None
    assert row["board_task_id"] == "btk_x"
    assert row["run_id"] == "swr_y"


def test_record_decision_none_connection_is_noop() -> None:
    # 3. conn=None is a no-op returning None.
    row_id = record_decision(
        None,
        brief="none connection",
        route="solo_strong",
    )
    assert row_id is None


def test_record_decision_swallows_missing_table() -> None:
    # 4. Missing table is swallowed — a bare sqlite3.connect(":memory:") with
    # no migrations must return None, not raise.
    conn = sqlite3.connect(":memory:")
    row_id = record_decision(
        conn,
        brief="missing table",
        route="solo_strong",
    )
    assert row_id is None
    conn.close()


def test_record_decision_swallows_closed_connection() -> None:
    # 5. Closed connection is swallowed — returns None, not raise.
    store = SqliteStore(":memory:")
    conn = store._connection
    conn.close()

    row_id = record_decision(
        conn,
        brief="closed connection",
        route="solo_strong",
    )
    assert row_id is None


def test_record_decision_derived_features_are_nullable() -> None:
    # 6. Derived features are nullable — omitting them leaves those columns NULL.
    store = SqliteStore(":memory:")
    conn = store._connection

    row_id = record_decision(
        conn,
        brief="nullable derived features",
        route="parallel_review",
    )
    assert row_id is not None

    row = conn.execute(
        """
        SELECT
            tool_density,
            context_breadth,
            merge_cost,
            shared_state_coupling,
            latency_ms,
            cbm_parallel_candidates
        FROM task_shape_decisions
        WHERE id = ?
        """,
        (row_id,),
    ).fetchone()

    assert row is not None
    assert row["tool_density"] is None
    assert row["context_breadth"] is None
    assert row["merge_cost"] is None
    assert row["shared_state_coupling"] is None
    assert row["latency_ms"] is None
    assert row["cbm_parallel_candidates"] is None
