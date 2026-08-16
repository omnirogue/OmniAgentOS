from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.employee_transcripts.store import TranscriptStore


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    database = SqliteStore(str(tmp_path / "store.db"))
    database._connection.execute(
        "INSERT INTO employees (id, name, status, created_at) VALUES (?,?,?,?)",
        ("emp_store", "Store Employee", "active", utc_now_iso()),
    )
    return database


def test_crud_and_bounded_list(database: SqliteStore) -> None:
    store = TranscriptStore(database)
    created = store.create(
        employee_id="emp_store",
        filename="notes.txt",
        content_hash="a" * 64,
        size_bytes=12,
        storage_path="tu_one.bin",
        source="manual",
        transcript_id="tu_one",
    )
    assert created["id"] == "tu_one"
    assert store.get("tu_one") == created
    assert store.list(employee_id="emp_store", status="uploaded", limit=1) == [created]
    updated = store.update("tu_one", {"status": "analyzed", "analyzed_at": utc_now_iso()})
    assert updated is not None and updated["status"] == "analyzed"
    assert updated["analyzed_at"] is not None
    assert store.delete("tu_one") is True
    assert store.delete("tu_one") is False
    assert store.get("tu_one") is None


def test_validation_and_connection_are_live(database: SqliteStore) -> None:
    store = TranscriptStore(database)
    assert store._connection is database._connection
    assert "_connection" not in store.__dict__, "per-thread connection must never be cached"
    with pytest.raises(ValueError, match="source"):
        store.create(
            employee_id="emp_store",
            filename="x",
            content_hash="b" * 64,
            size_bytes=1,
            storage_path="x",
            source="email",
        )
    with pytest.raises(ValueError, match="limit"):
        store.list(limit=0)
    with pytest.raises(ValueError, match="status"):
        store.update("missing", {"status": "invented"})


def test_reads_are_serialized_by_the_composed_store_lock(database: SqliteStore) -> None:
    class LockWitness:
        def __init__(self) -> None:
            self.entries = 0

        def __enter__(self) -> None:
            self.entries += 1

        def __exit__(self, *_: object) -> None:
            return None

    witness = LockWitness()
    database._lock = witness  # type: ignore[assignment]
    store = TranscriptStore(database)
    assert store.get("tu_missing") is None
    assert witness.entries == 1, "removing @_serialized must make this witness RED"
