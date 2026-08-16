"""StoreSwarmEmitter (WP6a): swarm.event rows via Store.insert_event, best-effort."""

from __future__ import annotations

import json

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.swarm.activity import StoreSwarmEmitter
from omniagentos.swarm.contracts import ACTION_TASK_ASSIGNED, SWARM_EVENT_KIND


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


def test_emit_writes_swarm_event_row(store: SqliteStore) -> None:
    emitter = StoreSwarmEmitter(store)
    emitter.emit(
        "swr_1",
        ACTION_TASK_ASSIGNED,
        {"task_id": "btk_1", "provider": "codex", "model": "gpt-5.6-sol", "account": "codex-2"},
    )

    rows = store.get_events_for_target("swarm_run", "swr_1")
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == SWARM_EVENT_KIND
    assert row["actor"] == "swarm"
    assert row["action"] == ACTION_TASK_ASSIGNED
    assert row["target_type"] == "swarm_run"
    assert row["target_id"] == "swr_1"
    payload = json.loads(row["payload_json"])
    assert payload == {
        "task_id": "btk_1",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "account": "codex-2",
    }


def test_emit_defaults_payload_to_empty_dict(store: SqliteStore) -> None:
    emitter = StoreSwarmEmitter(store)
    emitter.emit("swr_1", ACTION_TASK_ASSIGNED)

    rows = store.get_events_for_target("swarm_run", "swr_1")
    assert json.loads(rows[0]["payload_json"]) == {}


def test_emit_unrecognized_action_still_written(store: SqliteStore) -> None:
    """A future action landing ahead of this module's constants must still be
    observable rather than silently dropped."""
    emitter = StoreSwarmEmitter(store)
    emitter.emit("swr_1", "totally_new_action", {"x": 1})

    rows = store.get_events_for_target("swarm_run", "swr_1")
    assert rows[0]["action"] == "totally_new_action"


def test_emit_never_raises_when_store_fails(store: SqliteStore) -> None:
    """Emission is best-effort observability, never control flow (the
    `intake.service._emit_board_event` idiom) -- a slot worker mid-spawn must
    never be taken down by a broken events write."""

    class BrokenStore:
        def insert_event(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise RuntimeError("boom")

    emitter = StoreSwarmEmitter(BrokenStore())
    emitter.emit("swr_1", ACTION_TASK_ASSIGNED, {"task_id": "btk_1"})  # must not raise


def test_emit_scoped_to_run_id(store: SqliteStore) -> None:
    emitter = StoreSwarmEmitter(store)
    emitter.emit("swr_1", ACTION_TASK_ASSIGNED, {"i": 0})
    emitter.emit("swr_2", ACTION_TASK_ASSIGNED, {"i": 1})

    rows = store.get_events_for_target("swarm_run", "swr_1")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {"i": 0}


def test_emit_populates_execution_id_and_gap_free_sequence(store: SqliteStore) -> None:
    """W2.6 (086): a swarm run IS the execution unit for this lane, so
    execution_id = run_id, with a dense per-run sequence -- and a second run's
    events must never steal or skip a sequence number from the first."""
    emitter = StoreSwarmEmitter(store)
    emitter.emit("swr_1", ACTION_TASK_ASSIGNED, {"i": 0})
    emitter.emit("swr_2", ACTION_TASK_ASSIGNED, {"i": 1})
    emitter.emit("swr_1", ACTION_TASK_ASSIGNED, {"i": 2})

    swr1_rows = store._connection.execute(
        "SELECT execution_id, sequence FROM events WHERE target_id = 'swr_1' ORDER BY id ASC"
    ).fetchall()
    swr2_rows = store._connection.execute(
        "SELECT execution_id, sequence FROM events WHERE target_id = 'swr_2' ORDER BY id ASC"
    ).fetchall()
    assert [row["execution_id"] for row in swr1_rows] == ["swr_1", "swr_1"]
    assert [row["sequence"] for row in swr1_rows] == [1, 2]
    assert [row["execution_id"] for row in swr2_rows] == ["swr_2"]
    assert [row["sequence"] for row in swr2_rows] == [1]
