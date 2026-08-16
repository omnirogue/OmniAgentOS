"""Test: KnowledgeStore round-trip operations."""

from __future__ import annotations

import pytest

from omniagentos.knowledge.contracts import (
    EdgeType,
    EpisodeSource,
    FactStatus,
    NodeKind,
    PromotionDenied,
    Provenance,
)
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_test_gate, seed_facts


def test_add_episode(knowledge_store: KnowledgeStore) -> None:
    """add_episode returns episode ID."""
    eid = knowledge_store.add_episode(source=EpisodeSource.RUN.value, content="test content")
    assert isinstance(eid, int) and eid > 0


def test_get_episode(knowledge_store: KnowledgeStore) -> None:
    """get_episode retrieves an episode."""
    eid = knowledge_store.add_episode(
        source=EpisodeSource.RUN.value,
        content="test content",
        agent_id="agent-1",
        discipline="test",
    )
    ep = knowledge_store.get_episode(eid)
    assert ep is not None
    assert ep.source == EpisodeSource.RUN.value
    assert ep.content == "test content"
    assert ep.agent_id == "agent-1"


def test_add_fact(knowledge_store: KnowledgeStore) -> None:
    """add_fact returns fact ID."""
    eid = knowledge_store.add_episode(source=EpisodeSource.RUN.value, content="test")
    fid = knowledge_store.add_fact(
        statement="The sky is blue",
        episode_id=eid,
        provenance=Provenance.EXTRACTED.value,
    )
    assert isinstance(fid, int) and fid > 0


def test_get_fact(knowledge_store: KnowledgeStore) -> None:
    """get_fact retrieves a fact."""
    eid = knowledge_store.add_episode(source=EpisodeSource.RUN.value, content="test")
    # Insert values that survive the agent-role clamp (trust<=0.6, confidence<=0.7) so this
    # test isolates get_fact round-trip fidelity from the trigger (the clamp itself is
    # asserted by test_trigger_clamps_agent_insert).
    fid = knowledge_store.add_fact(
        statement="The sky is blue",
        episode_id=eid,
        discipline="science",
        trust=0.55,
        confidence=0.65,
    )
    fact = knowledge_store.get_fact(fid)
    assert fact is not None
    assert fact.statement == "The sky is blue"
    assert fact.status == FactStatus.QUARANTINED.value
    assert fact.trust == pytest.approx(0.55, abs=1e-4)
    assert fact.confidence == pytest.approx(0.65, abs=1e-4)


def test_trigger_clamps_agent_insert(knowledge_store: KnowledgeStore) -> None:
    """Agent-inserted facts are quarantined and trust is clamped."""
    eid = knowledge_store.add_episode(source=EpisodeSource.RUN.value, content="test")
    # Try to insert with status=active and trust=0.9 (both should be clamped)
    fid = knowledge_store.add_fact(
        statement="High trust fact",
        episode_id=eid,
        provenance=Provenance.EXTRACTED.value,
        trust=0.9,
    )
    fact = knowledge_store.get_fact(fid)
    assert fact.status == FactStatus.QUARANTINED.value
    assert fact.trust <= 0.6, "Agent trust must be clamped to 0.6"


def test_upsert_entity(knowledge_store: KnowledgeStore) -> None:
    """upsert_entity inserts or ignores due to UNIQUE(name, kind)."""
    eid1 = knowledge_store.upsert_entity(name="Alice", kind="person")
    eid2 = knowledge_store.upsert_entity(name="Alice", kind="person")
    assert eid1 == eid2, "Same entity should return same ID"

    entity = knowledge_store.get_entity("Alice", "person")
    assert entity is not None
    assert entity.name == "Alice"


def test_add_edge(knowledge_store: KnowledgeStore) -> None:
    """add_edge adds edges between entities."""
    eid1 = knowledge_store.upsert_entity(name="Alice", kind="person")
    eid2 = knowledge_store.upsert_entity(name="Bob", kind="person")

    edge_id = knowledge_store.add_edge(
        src_kind=NodeKind.ENTITY.value,
        src_id=eid1,
        dst_kind=NodeKind.ENTITY.value,
        dst_id=eid2,
        edge_type=EdgeType.ABOUT.value,
        weight=0.8,
    )
    assert isinstance(edge_id, int) and edge_id > 0


def test_admin_promote_fact(knowledge_store_admin: KnowledgeStore) -> None:
    """Admin role can promote facts to active."""
    eid = knowledge_store_admin.add_episode(source=EpisodeSource.HUMAN.value, content="test")
    fid = knowledge_store_admin.add_fact(statement="A promoted fact", episode_id=eid, trust=0.8)
    # Initially quarantined
    fact = knowledge_store_admin.get_fact(fid)
    assert fact.status == FactStatus.QUARANTINED.value

    # Promote (using test gate)
    gate_obj = make_test_gate()
    knowledge_store_admin.promote_fact(fid, gate_obj)

    fact = knowledge_store_admin.get_fact(fid)
    assert fact.status == FactStatus.ACTIVE.value


def test_agent_promote_denied(knowledge_store: KnowledgeStore) -> None:
    """Agent role cannot promote facts."""
    eid = knowledge_store.add_episode(source=EpisodeSource.RUN.value, content="test")
    fid = knowledge_store.add_fact(statement="A fact", episode_id=eid)

    # Try to promote on agent store — should raise PromotionDenied
    with pytest.raises(PromotionDenied):
        gate_obj = make_test_gate()
        knowledge_store.promote_fact(fid, gate_obj)


def test_bump_access(knowledge_store: KnowledgeStore) -> None:
    """bump_access increments access_count."""
    fact_ids = seed_facts(knowledge_store, n=2)
    fact = knowledge_store.get_fact(fact_ids[0])
    initial_count = fact.access_count

    knowledge_store.bump_access([fact_ids[0]])

    fact = knowledge_store.get_fact(fact_ids[0])
    assert fact.access_count == initial_count + 1


def test_strengthen_co_recall(knowledge_store: KnowledgeStore) -> None:
    """strengthen_co_recall creates/updates edges between facts."""
    fact_ids = seed_facts(knowledge_store, n=3)

    knowledge_store.strengthen_co_recall(fact_ids[:2], delta=0.1)

    # Check that edge was created
    import psycopg

    from omniagentos.knowledge.config import test_dsn

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT weight FROM edges
                WHERE src_kind = 'fact' AND src_id = %s AND dst_kind = 'fact' AND dst_id = %s AND edge_type = 'co_occurs'
                """,
                (fact_ids[0], fact_ids[1]),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] >= 0.1


def test_record_recall(knowledge_store: KnowledgeStore) -> None:
    """record_recall logs a recall event."""
    fact_ids = seed_facts(knowledge_store, n=2)
    recall_id = knowledge_store.record_recall(
        run_id="run-1",
        agent_id="agent-1",
        discipline="test",
        query_digest="abc123",
        fact_ids=fact_ids,
        tokens=10,
        latency_ms=50.0,
    )
    assert isinstance(recall_id, int) and recall_id > 0


def test_recall_candidates(knowledge_store: KnowledgeStore) -> None:
    """recall_candidates returns matching facts."""
    seed_facts(knowledge_store, n=5)

    # Seeded facts land quarantined (agent role), so this recall opts into quarantined to
    # exercise the vector+FTS+graph SQL. Active-only filtering + graph multi-hop over
    # promoted facts is covered in p2's test_recall.py.
    candidates = knowledge_store.recall_candidates(
        embedding=None,
        query_text="quick brown fox",
        include_quarantined=True,
        k=10,
    )
    assert len(candidates) > 0
    # All returned dicts should have the required keys
    for cand in candidates:
        assert "id" in cand
        assert "statement" in cand
        assert "vector_rank" in cand
        assert "fts_rank" in cand
        assert "graph_activation" in cand


def test_stats(knowledge_store: KnowledgeStore) -> None:
    """stats returns knowledge base statistics."""
    seed_facts(knowledge_store, n=3)
    stats = knowledge_store.stats()
    assert stats["facts"]["total"] >= 3
    assert stats["facts"]["quarantined"] == 3  # All seeded facts are quarantined


def test_ping(knowledge_store: KnowledgeStore) -> None:
    """ping returns True when DB is reachable."""
    assert knowledge_store.ping() is True
