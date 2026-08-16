"""Unit tests for :func:`default_memory_recaller` (metacog memory bridge).

The bridge must never raise and must degrade to ``[]`` when metacog is off,
query is empty, or any store fault occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.memory.memory_bridge import default_memory_recaller


def test_default_memory_recaller_empty_query() -> None:
    assert default_memory_recaller("", 5) == []
    assert default_memory_recaller("   ", 5) == []
    assert default_memory_recaller("something", 0) == []
    assert default_memory_recaller("something", -1) == []


def test_default_memory_recaller_metacog_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_MODE", "off")
    # Clear config cache so the env override is observed.
    from omniagentos.metacog import config as metacog_config

    metacog_config.clear_metacog_config_cache()
    assert default_memory_recaller("any lesson query", 5) == []


def test_default_memory_recaller_catches_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store fault degrades to [], and the patch must hit the REAL dependency.

    This test used to patch ``metacog.service.MetacogService`` — a symbol
    ``memory_bridge`` never imports (it uses ``metacog.store.MetacogStore``), so
    nothing was substituted, no exception was ever raised, and the assertion
    passed on an empty store instead of on the error path. The double is now
    installed where the bridge actually looks, and the test asserts the fault
    was really triggered.
    """
    monkeypatch.setenv("OMNIAGENTOS_METACOG_MODE", "enforce")
    from omniagentos.metacog import config as metacog_config

    metacog_config.clear_metacog_config_cache()

    calls: list[str] = []

    class _ExplodingStore:
        def search_memory(self, *_args: Any, **_kwargs: Any) -> Any:
            calls.append("search_memory")
            raise RuntimeError("store unavailable")

    from omniagentos.memory import memory_bridge

    monkeypatch.setattr(memory_bridge, "_get_store", lambda: _ExplodingStore())

    assert default_memory_recaller("query that would hit the store", 3) == []
    assert calls == ["search_memory"], (
        "the fault path was never entered, so this test proved nothing about it"
    )


def test_a_telemetry_failure_cannot_suppress_a_lesson(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording that a lesson was shown must never decide whether it is shown.

    ``memory_bridge`` swallows retrieval-event failures on purpose; the comment
    said "telemetry cannot suppress a lesson" and nothing tested it. If that
    ``except`` ever narrows, or the record call moves above the append, an
    unwritable telemetry table silently stops agents receiving their lessons.
    """
    monkeypatch.setenv("OMNIAGENTOS_METACOG_MODE", "enforce")
    from omniagentos.metacog import config as metacog_config

    metacog_config.clear_metacog_config_cache()

    class _Memory:
        def __init__(self, mem_id: str, statement: str) -> None:
            self.id = mem_id
            self.statement = statement
            self.confidence = 0.9
            self.success_count = 1
            self.sample_count = 1

    attempts: list[str] = []

    class _StoreWithBrokenTelemetry:
        def search_memory(self, *_args: Any, **_kwargs: Any) -> list[_Memory]:
            return [_Memory("mem_a", "always rebase before pushing")]

        def record_memory_retrieval_event(self, **kwargs: Any) -> None:
            attempts.append(str(kwargs.get("memory_id")))
            raise RuntimeError("retrieval telemetry table is locked")

    from omniagentos.memory import memory_bridge

    monkeypatch.setattr(memory_bridge, "_get_store", lambda: _StoreWithBrokenTelemetry())

    lines = default_memory_recaller("rebase", 3, task_id="tsk_1", run_id="run_1")

    assert lines == ["always rebase before pushing"]
    assert attempts == ["mem_a"], "the telemetry write must actually have been attempted"


def test_recalled_lessons_write_one_fk_safe_event_per_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner seam records every injected promoted lesson once for its run."""
    from omniagentos.db.store import SqliteStore
    from omniagentos.memory import memory_bridge
    from omniagentos.memory.runner_hook import safe_memory_block
    from omniagentos.memory.store import ConversationStore
    from omniagentos.metacog import config as metacog_config
    from omniagentos.metacog.contracts import MemoryRecord
    from omniagentos.metacog.store import MetacogStore

    monkeypatch.setenv("OMNIAGENTOS_METACOG_MODE", "enforce")
    monkeypatch.setenv("OMNIAGENTOS_MEMORY_LESSONS", "1")
    metacog_config.clear_metacog_config_cache()
    db_path = tmp_path / "telemetry.db"
    task_store = SqliteStore(str(db_path))
    metacog_store = MetacogStore(str(db_path))
    monkeypatch.setattr(memory_bridge, "_metacog_store", metacog_store)

    memories = [
        metacog_store.upsert_memory(
            MemoryRecord(
                statement=f"Retry HTTP requests with lesson {number}",
                confidence=0.9 - (number / 100),
                promotion_status="promoted",
            )
        )
        for number in range(1, 4)
    ]
    task_id = "task_retrieval_telemetry"
    run_id = "run_retrieval_telemetry"
    ConversationStore(task_store).append_turn(
        "task", task_id, "user", "Retry HTTP requests safely"
    )

    block, _context = safe_memory_block(
        task_store,
        task_id=task_id,
        run_id=run_id,
        budget_tokens=4000,
    )
    assert block is not None
    assert all(memory.statement in block for memory in memories)

    rows = metacog_store._connection.execute(
        "SELECT memory_id, task_id, run_id, query, rank, selected "
        "FROM metacog_memory_retrieval_events ORDER BY rank"
    ).fetchall()
    assert len(rows) == 3
    assert [row["memory_id"] for row in rows] == [memory.id for memory in memories]
    assert {(row["task_id"], row["run_id"]) for row in rows} == {(task_id, run_id)}
    assert {row["query"] for row in rows} == {"Retry HTTP requests safely"}
    assert [row["rank"] for row in rows] == [0, 1, 2]
    assert {row["selected"] for row in rows} == {1}

    # The store's INSERT .. SELECT refuses a non-existent memory id even before
    # SQLite's foreign-key check could allow an orphan on a misconfigured connection.
    assert (
        metacog_store.record_memory_retrieval_event(
            memory_id="mem_not_present",
            task_id=task_id,
            run_id=run_id,
            query="Retry HTTP requests safely",
            rank=99,
        )
        is None
    )

    # A duplicate prompt assembly must not duplicate a memory/run telemetry event.
    safe_memory_block(task_store, task_id=task_id, run_id=run_id, budget_tokens=4000)
    assert (
        metacog_store._connection.execute(
            "SELECT COUNT(*) FROM metacog_memory_retrieval_events"
        ).fetchone()[0]
        == 3
    )
