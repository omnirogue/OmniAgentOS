"""Database-backed tests for hybrid ranking and recall feedback."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omniagentos.knowledge.recall import clear_run_state, recall
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder, make_test_gate


def _active_fact(
    store: KnowledgeStore,
    statement: str,
    *,
    discipline: str | None = "code",
    scope: str = "global",
    embedding: bool = True,
    trust: float = 0.8,
    importance: float = 0.7,
) -> int:
    episode_id = store.add_episode(source="run", content=statement)
    vector = make_fake_embedder().embed([statement])[0] if embedding else None
    fact_id = store.add_fact(
        statement=statement,
        episode_id=episode_id,
        discipline=discipline,
        scope=scope,
        trust=trust,
        importance=importance,
        embedding=vector,
    )
    store.promote_fact(fact_id, make_test_gate())
    return fact_id


def _edge_weight(store: KnowledgeStore, first: int, second: int) -> float:
    with store._conn().cursor() as cur:
        cur.execute(
            "SELECT weight FROM edges WHERE src_id = %s AND dst_id = %s "
            "AND edge_type = 'co_occurs'",
            (min(first, second), max(first, second)),
        )
        row = cur.fetchone()
    assert row is not None
    return float(row[0])


def test_full_mode_reaches_fact_only_through_two_graph_hops(
    knowledge_store_admin: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = _active_fact(knowledge_store_admin, "starlight deployment checksum")
    middle = _active_fact(
        knowledge_store_admin,
        "unrelated bridge node zephyr",
        embedding=False,
    )
    target = _active_fact(
        knowledge_store_admin,
        "private archival retention is ninety days",
        embedding=False,
    )
    # The recursive CTE spreads backwards from dst to src.
    knowledge_store_admin.add_edge(
        src_kind="fact",
        src_id=middle,
        dst_kind="fact",
        dst_id=seed,
        edge_type="about",
        weight=1.0,
    )
    knowledge_store_admin.add_edge(
        src_kind="fact",
        src_id=target,
        dst_kind="fact",
        dst_id=middle,
        edge_type="about",
        weight=1.0,
    )

    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_RECALL_MODE", "full")
    full = recall(knowledge_store_admin, prompt="starlight deployment checksum")
    target_hit = next(item for item in full.facts if item.fact.id == target)
    assert target_hit.signals["graph"] > 0
    assert target_hit.signals["graph_activation"] == pytest.approx(0.25)
    assert "vector" not in target_hit.signals
    assert "fts" not in target_hit.signals

    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_RECALL_MODE", "lean")
    lean = recall(knowledge_store_admin, prompt="starlight deployment checksum")
    assert target not in {item.fact.id for item in lean.facts}


def test_scope_filter_fails_closed_without_leaking_agent_or_discipline_facts(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    visible = _active_fact(knowledge_store_admin, "scope sentinel visible", discipline="ops")
    other_agent = _active_fact(
        knowledge_store_admin,
        "scope sentinel agent secret",
        discipline="ops",
        scope="agent:other",
    )
    discipline_only = _active_fact(
        knowledge_store_admin,
        "scope sentinel discipline secret",
        discipline="finance",
        scope="discipline",
    )
    invalid_scope = _active_fact(
        knowledge_store_admin,
        "scope sentinel malformed secret",
        scope="team:any",
    )

    result = recall(
        knowledge_store_admin,
        prompt="scope sentinel",
        discipline="ops",
        agent_id="worker-3",
    )
    ids = {item.fact.id for item in result.facts}
    assert visible in ids
    assert ids.isdisjoint({other_agent, discipline_only, invalid_scope})

    authorized = recall(
        knowledge_store_admin,
        prompt="scope sentinel",
        discipline="finance",
        agent_id="other",
    )
    authorized_ids = {item.fact.id for item in authorized.facts}
    assert {other_agent, discipline_only}.issubset(authorized_ids)


def test_multi_signal_bonus_and_hebbian_feedback_are_real_and_deduplicated(
    knowledge_store_admin: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_RECALL_MODE", "full")
    first = _active_fact(knowledge_store_admin, "copper raven release procedure")
    second = _active_fact(knowledge_store_admin, "copper raven rollback procedure")
    run_id = "run-hebbian-dedup"
    try:
        initial_first = knowledge_store_admin.get_fact(first)
        initial_second = knowledge_store_admin.get_fact(second)
        assert initial_first is not None and initial_second is not None

        one = recall(
            knowledge_store_admin,
            prompt="copper raven procedure",
            run_id=run_id,
        )
        hit = next(item for item in one.facts if item.fact.id == first)
        assert hit.signals["vector"] > 0
        assert hit.signals["fts"] > 0
        assert hit.signals["multi_signal_bonus"] >= 1.25
        expected_rrf = 1.0 / (60.0 + hit.signals["vector"]) + 1.0 / (60.0 + hit.signals["fts"])
        assert hit.signals["rrf"] == pytest.approx(expected_rrf)
        first_weight = _edge_weight(knowledge_store_admin, first, second)
        assert first_weight == pytest.approx(0.05)

        two = recall(
            knowledge_store_admin,
            prompt="copper raven procedure",
            run_id=run_id,
        )
        assert two.recall_id is not None and one.recall_id is not None
        assert _edge_weight(knowledge_store_admin, first, second) == pytest.approx(first_weight)

        final_first = knowledge_store_admin.get_fact(first)
        final_second = knowledge_store_admin.get_fact(second)
        assert final_first is not None and final_second is not None
        assert final_first.access_count == initial_first.access_count + 2
        assert final_second.access_count == initial_second.access_count + 2
    finally:
        clear_run_state(run_id)


def test_quarantine_requires_opt_in_and_receives_steep_discount(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    active = _active_fact(knowledge_store_admin, "quarantine comparison phrase")
    episode_id = knowledge_store_admin.add_episode(source="web", content="unverified")
    statement = "quarantine comparison phrase unverified"
    quarantined = knowledge_store_admin.add_fact(
        statement=statement,
        episode_id=episode_id,
        trust=0.8,
        importance=0.7,
        embedding=make_fake_embedder().embed([statement])[0],
    )

    default = recall(knowledge_store_admin, prompt="quarantine comparison phrase")
    assert quarantined not in {item.fact.id for item in default.facts}
    opted_in = recall(
        knowledge_store_admin,
        prompt="quarantine comparison phrase",
        include_quarantined=True,
    )
    scores: dict[int, float] = {item.fact.id: item.score for item in opted_in.facts}
    assert quarantined in scores
    assert scores[quarantined] < scores[active] * 0.25


def test_candidate_hydration_performs_no_get_fact_queries() -> None:
    """A fake store makes an accidental N+1 hydration regression immediately fail."""

    class StubStore:
        _embedder = None

        def recall_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
            now = datetime.now(UTC)
            return [
                {
                    "id": 73,
                    "statement": "hydrated directly from the candidate CTE",
                    "discipline": None,
                    "scope": "global",
                    "episode_id": 4,
                    "provenance": "extracted",
                    "trust": 0.8,
                    "confidence": 0.8,
                    "status": "active",
                    "valid_at": now,
                    "recorded_at": now,
                    "invalid_at": None,
                    "superseded_by": None,
                    "importance": 0.7,
                    "access_count": 0,
                    "last_accessed": now,
                    "helped_count": 0,
                    "embedding": None,
                    "search_tsv": "ignored extra column",
                    "vector_rank": None,
                    "fts_rank": 1,
                    "graph_activation": 0.0,
                }
            ]

        def get_fact(self, _fact_id: int) -> None:
            pytest.fail("candidate hydration must not issue get_fact queries")

    # The runtime type is intentionally duck-typed so any accidental N+1 call fails.
    result = recall(StubStore(), prompt="nothing")  # type: ignore[arg-type]
    assert [item.fact.id for item in result.facts] == [73]
