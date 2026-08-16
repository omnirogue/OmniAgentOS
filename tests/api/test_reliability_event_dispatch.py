"""M-12: reliability vs SSE event dispatch must be name-disambiguated.

``insert_event`` is the frozen Store SSE/event-log API (events table, int id).
``insert_reliability_event`` writes reliability_events (string id). There is no
shape-sniffing dual-dispatch on insert_event.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from omniagentos.api.deps import _api_reliability_store
from omniagentos.contracts import Store
from omniagentos.db.store import SqliteStore
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.store import SqliteReliabilityStore
from tests.support.db_template import migrated_db


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "dispatch.db")
    return migrated_db(SqliteStore, db)


def _count(db: str, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_api_store_insert_event_writes_sse_events_only(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = _api_reliability_store(db)

    row_id = store.insert_event(
        "audit.event",
        "test",
        "dispatch.sse",
        target_type="run",
        target_id="run_1",
        payload={"n": 1},
    )
    assert isinstance(row_id, int)
    assert row_id >= 1
    assert _count(db, "events") == 1
    assert _count(db, "reliability_events") == 0


def test_api_store_insert_reliability_event_writes_reliability_table_only(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    store = _api_reliability_store(db)

    event_id = store.insert_reliability_event(  # type: ignore[attr-defined]
        failure_class="run_failed",
        severity="warning",
        signature="sig|dispatch",
        occurrence_key="occ|dispatch|1",
        source="test:dispatch",
        evidence_json={"k": "v"},
    )
    assert isinstance(event_id, str)
    assert event_id.startswith("evt_") or event_id  # new_id("evt") shape
    assert _count(db, "reliability_events") == 1
    assert _count(db, "events") == 0


def test_reliability_kwargs_cannot_land_in_sse_via_insert_event(tmp_path: Path) -> None:
    """Positional/keyword reliability shape must not silently write reliability_events.

    insert_event is the SSE method only. Passing reliability kwargs is a TypeError
    (unexpected keywords) or writes a mis-shaped SSE row — never reliability_events.
    """
    db = _db(tmp_path)
    store = _api_reliability_store(db)

    with pytest.raises(TypeError):
        store.insert_event(  # type: ignore[call-arg]
            failure_class="run_failed",
            severity="critical",
            signature="sig",
            occurrence_key="occ",
            source="bad",
        )

    assert _count(db, "reliability_events") == 0
    assert _count(db, "events") == 0

    # Five-arg positional reliability shape also must not dual-dispatch into
    # reliability_events (it binds as type/actor/action/... on the SSE method).
    sse_id = store.insert_event(
        "run_failed",
        "critical",
        "sig",
        "occ",
        "source_as_target_id",
    )
    assert isinstance(sse_id, int)
    assert _count(db, "events") == 1
    assert _count(db, "reliability_events") == 0


def test_sqlite_reliability_store_exposes_compatible_base_insert_event(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    store = SqliteReliabilityStore(db_path=db)

    # Base protocol method is still the SSE writer (inherited, not overridden).
    assert SqliteReliabilityStore.insert_event is SqliteStore.insert_event
    assert hasattr(store, "insert_reliability_event")
    assert not hasattr(SqliteReliabilityStore, "insert_event") or (
        SqliteReliabilityStore.insert_event is SqliteStore.insert_event
    )

    rel_id = store.insert_reliability_event(
        "timeout",
        "warning",
        "sig-t",
        "occ-t",
        "test",
    )
    sse_id = store.insert_event("audit.event", "api", "ping")
    assert isinstance(rel_id, str)
    assert isinstance(sse_id, int)
    assert _count(db, "reliability_events") == 1
    assert _count(db, "events") == 1


def test_protocol_method_names_are_disambiguated() -> None:
    """Structural contracts: ReliabilityStore and Store do not share insert_event shape."""
    rel_params = inspect.signature(ReliabilityStore.insert_reliability_event).parameters
    sse_params = inspect.signature(Store.insert_event).parameters
    assert "failure_class" in rel_params
    assert "failure_class" not in sse_params
    assert "actor" in sse_params
    assert "actor" not in rel_params
    assert not hasattr(ReliabilityStore, "insert_event") or not callable(
        getattr(ReliabilityStore, "insert_event", None)
    )
