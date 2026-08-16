"""Tests for GET /api/board/{task_id}/eta (§3.10): all three bases + null."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.intake.service import compute_board_eta


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _card(collab_store: CollabStore, title: str, **fields: Any) -> str:
    card = BoardTask(title=title, description=title)
    collab_store.create_board_task(card)
    if fields:
        collab_store.update_board_task(card.id, fields)
    return card.id


def _seed_run_with_steps(
    store: SqliteStore,
    run_id: str,
    durations: list[float],
    pending: int,
) -> None:
    store._connection.execute(
        "INSERT INTO tasks (id, title, state, created_at, updated_at) "
        "VALUES (?, 'ETA task', 'running', ?, ?)",
        (f"tsk_{run_id}", _iso(3600), _iso(60)),
    )
    store._connection.execute(
        "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
        "created_at, updated_at) VALUES (?, ?, 'agent', ?, 'running', ?, ?, ?)",
        (run_id, f"tsk_{run_id}", f"trace_{run_id}", _iso(3600), _iso(3600), _iso(60)),
    )
    for i, duration in enumerate(durations):
        store._connection.execute(
            "INSERT INTO steps (run_id, seq, name, status, started_at, finished_at) "
            "VALUES (?, ?, ?, 'completed', ?, ?)",
            (run_id, i, f"step {i}", _iso(duration + 100), _iso(100)),
        )
    for j in range(pending):
        store._connection.execute(
            "INSERT INTO steps (run_id, seq, name, status) VALUES (?, ?, ?, 'pending')",
            (run_id, len(durations) + j, f"pending {j}"),
        )
    store._connection.commit()


class _FakeSessionsDal:
    def __init__(self, session: dict[str, Any] | None) -> None:
        self.session = session

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.session


class TestRunStepsBasis:
    def test_median_times_remaining_floor_30(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        _seed_run_with_steps(store, "run_eta", durations=[10.0, 20.0, 30.0], pending=2)
        card_id = _card(collab_store, "eta card", run_id="run_eta")
        task = collab_store.get_board_task(card_id)
        result = compute_board_eta(store, None, task)
        assert result["basis"] == "run_steps"
        # median(10,20,30)=20s x 2 remaining = 40s
        assert result["estimate_seconds"] == 40
        assert result["sample_size"] == 3
        assert result["confidence"] == "medium"  # <5 completed
        assert result["computed_at"]

    def test_high_confidence_at_five_completed(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        _seed_run_with_steps(
            store, "run_eta5", durations=[5.0] * 5, pending=1
        )
        card_id = _card(collab_store, "eta card 5", run_id="run_eta5")
        task = collab_store.get_board_task(card_id)
        result = compute_board_eta(store, None, task)
        assert result["basis"] == "run_steps"
        assert result["estimate_seconds"] == 30  # floor: 5s x 1 = 5 → 30
        assert result["confidence"] == "high"

    def test_fewer_than_two_completed_falls_through(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        _seed_run_with_steps(store, "run_eta1", durations=[10.0], pending=3)
        card_id = _card(collab_store, "eta card 1", run_id="run_eta1")
        task = collab_store.get_board_task(card_id)
        result = compute_board_eta(store, None, task)
        assert result["estimate_seconds"] is None
        assert result["basis"] is None


class TestSessionProgressBasis:
    def test_elapsed_rate_projection(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        session = {
            "id": "ses_eta",
            "state": "running",
            "created_at": _iso(120),
            # _session_progress reads todos_json
            "todos_json": (
                '[{"content": "a", "status": "completed"},'
                '{"content": "b", "status": "completed"},'
                '{"content": "c", "status": "in_progress"},'
                '{"content": "d", "status": "pending"}]'
            ),
        }
        card_id = _card(collab_store, "session card", result_ref="ses_eta")
        task = collab_store.get_board_task(card_id)
        result = compute_board_eta(store, _FakeSessionsDal(session), task)
        assert result["basis"] == "session_progress"
        # 120s elapsed / 2 done = 60s per step x 2 remaining = 120s
        assert result["estimate_seconds"] == 120
        assert result["confidence"] == "low"
        assert result["sample_size"] == 2

    def test_terminal_session_falls_through(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        session = {
            "id": "ses_done",
            "state": "completed",
            "created_at": _iso(120),
            "todos_json": '[{"content": "a", "status": "completed"}]',
        }
        card_id = _card(collab_store, "done session card", result_ref="ses_done")
        task = collab_store.get_board_task(card_id)
        result = compute_board_eta(store, _FakeSessionsDal(session), task)
        assert result["estimate_seconds"] is None


class TestDisciplineHistoryBasis:
    def test_median_wall_minus_elapsed(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        # 3 done cards in the same discipline: wall times 100s, 200s, 300s
        for i, wall in enumerate((100, 200, 300)):
            card_id = _card(collab_store, f"done {i}", discipline="backend")
            store._connection.execute(
                "UPDATE board_tasks SET status = 'done', created_at = ?, updated_at = ? "
                "WHERE id = ?",
                (_iso(wall + 50), _iso(50), card_id),
            )
        store._connection.commit()
        current = _card(collab_store, "current", discipline="backend")
        store._connection.execute(
            "UPDATE board_tasks SET created_at = ? WHERE id = ?", (_iso(20), current)
        )
        store._connection.commit()
        task = collab_store.get_board_task(current)
        result = compute_board_eta(store, None, task)
        assert result["basis"] == "discipline_history"
        # median(100,200,300)=200s − 20s elapsed ≈ 180s (second-truncation slack)
        assert 175 <= result["estimate_seconds"] <= 181
        assert result["confidence"] == "low"
        assert result["sample_size"] == 3

    def test_fewer_than_three_samples_null(
        self, store: SqliteStore, collab_store: CollabStore
    ) -> None:
        for i in range(2):
            card_id = _card(collab_store, f"done {i}", discipline="design")
            store._connection.execute(
                "UPDATE board_tasks SET status = 'done', created_at = ?, updated_at = ? "
                "WHERE id = ?",
                (_iso(150), _iso(50), card_id),
            )
        store._connection.commit()
        current = _card(collab_store, "current design", discipline="design")
        task = collab_store.get_board_task(current)
        result = compute_board_eta(store, None, task)
        assert result["estimate_seconds"] is None
        assert result["basis"] is None


class TestEtaRoute:
    def test_route_shape(
        self, asgi_client: httpx.AsyncClient, collab_store: CollabStore
    ) -> None:
        card_id = _card(collab_store, "plain card")
        resp = _run(asgi_client.get(f"/api/board/{card_id}/eta"))
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "estimate_seconds": None,
            "basis": None,
            "sample_size": 0,
            "confidence": None,
            "computed_at": body["computed_at"],
        }

    def test_route_404(self, asgi_client: httpx.AsyncClient) -> None:
        resp = _run(asgi_client.get("/api/board/btk_nope/eta"))
        assert resp.status_code == 404
