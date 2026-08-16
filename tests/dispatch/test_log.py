"""record_decision writes one telemetry row and swallows every failure."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.collab.store import CollabStore
from omniagentos.dispatch.gate import GateDecision
from omniagentos.dispatch.log import record_decision


def _decision() -> GateDecision:
    return GateDecision(
        decision="solo_fast",
        risk_flagged=False,
        confidence=0.85,
        gate="rules",
        latency_ms=1.2,
        reason="short single-artifact brief",
    )


def test_record_decision_writes_row(tmp_path: Path) -> None:
    store = CollabStore(str(tmp_path / "dispatch.db"))._store
    conn = store._connection

    row_id = record_decision(
        conn,
        _decision(),
        brief="add a docstring to parse_config",
        applied=True,
        dispatch_kind="session",
        ref_id="ses_abc",
    )
    assert row_id is not None

    row = conn.execute("SELECT * FROM dispatch_decisions WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    assert row["decision"] == "solo_fast"
    assert row["gate"] == "rules"
    assert row["risk_flagged"] == 0
    assert row["applied"] == 1
    assert row["dispatch_kind"] == "session"
    assert row["ref_id"] == "ses_abc"
    assert row["brief_head"].startswith("add a docstring")
    assert row["confidence"] == 0.85


def test_record_decision_truncates_brief_head(tmp_path: Path) -> None:
    store = CollabStore(str(tmp_path / "dispatch.db"))._store
    conn = store._connection
    brief = "x" * 500
    row_id = record_decision(conn, _decision(), brief=brief)
    row = conn.execute(
        "SELECT brief_head FROM dispatch_decisions WHERE id = ?", (row_id,)
    ).fetchone()
    assert len(row["brief_head"]) == 160


def test_record_decision_none_connection_is_noop() -> None:
    assert record_decision(None, _decision(), brief="whatever") is None


def test_record_decision_swallows_missing_table() -> None:
    # A connection with NO dispatch_decisions table (migration not applied) must
    # NOT raise -- telemetry can never block dispatch.
    conn = sqlite3.connect(":memory:")
    assert record_decision(conn, _decision(), brief="whatever") is None
    conn.close()


def test_record_decision_swallows_closed_connection(tmp_path: Path) -> None:
    store = CollabStore(str(tmp_path / "dispatch.db"))._store
    conn = store._connection
    conn.close()
    assert record_decision(conn, _decision(), brief="whatever") is None
