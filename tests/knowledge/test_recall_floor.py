"""Recall relevance-floor behavior and attribution boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omniagentos.knowledge import config
from omniagentos.knowledge.contracts import RECALL_FOOTER, RECALL_HEADER
from omniagentos.knowledge.recall import (
    _render,
    last_recall_metadata,
    recall,
    render_recall_block,
    safe_recall_block,
)

_FIXED_NOW = datetime(2026, 8, 12, tzinfo=UTC)
_FIXED_NOW_S = _FIXED_NOW.timestamp()


def _row(
    fact_id: int,
    *,
    fts_rank: int,
    status: str = "active",
    last_accessed: datetime = _FIXED_NOW,
    trust: float = 0.8,
    importance: float = 0.7,
) -> dict[str, Any]:
    """One candidate row shaped exactly like recall_candidates() returns."""
    return {
        "id": fact_id,
        "statement": f"floor fact {fact_id}",
        "discipline": None,
        "scope": "global",
        "episode_id": 1,
        "provenance": "extracted",
        "trust": trust,
        "confidence": 0.8,
        "status": status,
        "valid_at": _FIXED_NOW,
        "recorded_at": _FIXED_NOW,
        "invalid_at": None,
        "superseded_by": None,
        "importance": importance,
        "access_count": 0,
        "last_accessed": last_accessed,
        "helped_count": 0,
        "embedding": None,
        "search_tsv": "ignored",
        "vector_rank": None,
        "fts_rank": fts_rank,
        "graph_activation": 0.0,
    }


class _StubStore:
    _embedder = None

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.bumped: list[list[int]] = []
        self.strengthened: list[list[int]] = []
        self.recorded: list[dict[str, Any]] = []

    def recall_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self._rows)

    def bump_access(self, fact_ids: list[int]) -> None:
        self.bumped.append(fact_ids)

    def strengthen_co_recall(self, fact_ids: list[int]) -> None:
        self.strengthened.append(fact_ids)

    def record_recall(self, **kwargs: Any) -> int:
        self.recorded.append(kwargs)
        return 1


def _recall(
    store: _StubStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str | None = None,
    score_floor: float | None = None,
    floor_fraction: float | None = None,
    include_quarantined: bool = False,
) -> Any:
    monkeypatch.setattr("omniagentos.knowledge.recall.time.time", lambda: _FIXED_NOW_S)
    return recall(  # type: ignore[arg-type]
        store,
        prompt="floor query",
        run_id=run_id,
        score_floor=score_floor,
        floor_fraction=floor_fraction,
        include_quarantined=include_quarantined,
    )


def test_floor_includes_the_boundary_and_higher_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _StubStore([_row(1, fts_rank=1), _row(2, fts_rank=2), _row(3, fts_rank=3)])
    scores = {item.fact.id: item.score for item in _recall(store, monkeypatch).facts}

    result = _recall(store, monkeypatch, score_floor=scores[2])

    # The comparison is inclusive: scores exactly at the floor remain injectable.
    assert [item.fact.id for item in result.facts] == [1, 2]


def test_all_floor_filtered_is_recorded_and_keeps_candidates_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StubStore([_row(1, fts_rank=1), _row(2, fts_rank=2)])
    result = _recall(store, monkeypatch, run_id="run-floor-filtered", score_floor=1.0)

    assert result.facts == []
    assert result.suppressed_count == 2
    assert _render(result) == ("", [])
    assert store.bumped == [[1, 2]]
    assert store.strengthened == [[1, 2]]
    assert store.recorded[0]["fact_ids"] == [1, 2]
    assert store.recorded[0]["surfaced_fact_ids"] == []


def test_disabled_floor_preserves_the_existing_rendered_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StubStore([_row(1, fts_rank=1), _row(2, fts_rank=2)])
    result = _recall(store, monkeypatch)

    assert render_recall_block(result) == (
        RECALL_HEADER
        + "[extracted|0.80|active] floor fact 1\n"
        + "[extracted|0.80|active] floor fact 2\n"
        + RECALL_FOOTER
    )


def test_preview_remains_side_effect_free_with_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _StubStore([_row(1, fts_rank=1), _row(2, fts_rank=2)])

    result = _recall(store, monkeypatch, score_floor=1.0)

    assert result.facts == []
    assert store.bumped == []
    assert store.strengthened == []
    assert store.recorded == []


def test_safe_recall_reports_floor_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _StubStore([_row(1, fts_rank=1)])
    monkeypatch.setattr("omniagentos.knowledge.recall.time.time", lambda: _FIXED_NOW_S)
    monkeypatch.setattr("omniagentos.knowledge.recall._get_store", lambda: store)
    monkeypatch.setattr("omniagentos.knowledge.config.recall_score_floor", lambda: 1.0)
    monkeypatch.setattr("omniagentos.knowledge.config.recall_floor_fraction", lambda: 0.0)

    assert (
        safe_recall_block(prompt="floor query", discipline=None, agent_id=None, run_id="run-floor")
        is None
    )
    assert last_recall_metadata("run-floor") == {
        "status": "floor_suppressed",
        "recall_id": 1,
        "fact_count": 0,
        "suppressed_count": 1,
    }


def test_shipped_defaults_keep_a_30_day_quarantined_rank_one_fact_injectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StubStore(
        [
            _row(
                1,
                fts_rank=1,
                status="quarantined",
                last_accessed=_FIXED_NOW - timedelta(days=30),
                importance=0.5,
            )
        ]
    )

    assert config.recall_score_floor() == 0.0
    assert config.recall_floor_fraction() == 0.15
    result = _recall(
        store,
        monkeypatch,
        score_floor=config.recall_score_floor(),
        floor_fraction=config.recall_floor_fraction(),
        include_quarantined=True,
    )

    assert [item.fact.id for item in result.facts] == [1]
    assert result.suppressed_count == 0
