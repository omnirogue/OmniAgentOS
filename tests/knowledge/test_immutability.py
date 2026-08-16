"""Test: immutability of facts (no DELETE code path, supersede sets flags)."""

from __future__ import annotations

from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import seed_facts


def test_no_delete_in_store_code() -> None:
    """Verify no DELETE statement in store.py code."""
    import inspect

    from omniagentos.knowledge import store

    source = inspect.getsource(store)
    # Check that no DELETE statement appears (case-insensitive)
    assert "DELETE" not in source.upper(), "store.py must not contain DELETE statements"


def test_supersede_sets_flags(knowledge_store_admin: KnowledgeStore) -> None:
    """supersede_fact sets invalid_at and superseded_by, never deletes."""
    fact_ids = seed_facts(knowledge_store_admin, n=2)
    loser_id, winner_id = fact_ids[0], fact_ids[1]

    knowledge_store_admin.supersede_fact(loser_id, winner_id)

    loser = knowledge_store_admin.get_fact(loser_id)
    assert loser is not None, "Superseded fact must not be deleted"
    assert loser.status == FactStatus.SUPERSEDED.value
    assert loser.invalid_at is not None
    assert loser.superseded_by == winner_id


def test_invalidated_edges_not_deleted(knowledge_store_admin: KnowledgeStore) -> None:
    """invalidate_edge sets invalid_at, never deletes."""
    from omniagentos.knowledge.contracts import EdgeType, NodeKind

    eid1 = knowledge_store_admin.upsert_entity(name="Alice", kind="person")
    eid2 = knowledge_store_admin.upsert_entity(name="Bob", kind="person")

    edge_id = knowledge_store_admin.add_edge(
        src_kind=NodeKind.ENTITY.value,
        src_id=eid1,
        dst_kind=NodeKind.ENTITY.value,
        dst_id=eid2,
        edge_type=EdgeType.ABOUT.value,
    )

    knowledge_store_admin.invalidate_edge(edge_id)

    # Verify edge still exists but is marked invalid
    import psycopg

    from omniagentos.knowledge.config import test_dsn

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT invalid_at FROM edges WHERE id = %s", (edge_id,))
            row = cur.fetchone()
            assert row is not None, "Edge must not be deleted"
            assert row[0] is not None, "Edge invalid_at must be set"
