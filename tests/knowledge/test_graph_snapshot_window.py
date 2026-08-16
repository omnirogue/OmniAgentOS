"""The graph_snapshot window must be drawn from the rows its callers actually keep.

Both production callers of ``KnowledgeStore.graph_snapshot`` discard everything that is
not ``status='active'`` — ``api/routes/knowledge.py:get_graph`` and
``briefing/gather.py:_promoted_facts``. When the store applies its ``LIMIT`` before that
predicate, a KB whose oldest rows are quarantined (the designed steady state: migration
001 defaults facts to quarantined and the 002 trigger forces it for agent writes) fills
the whole window with rows the callers throw away, and both surfaces report nothing.
"""

from __future__ import annotations

from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_test_gate, seed_facts


def test_graph_snapshot_window_is_drawn_from_active_facts(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """Active facts beyond the first `limit_nodes` rows must still be returned."""
    seed_facts(knowledge_store_admin, 12)  # quarantined: the DB default status
    active_ids = seed_facts(knowledge_store_admin, 3)
    gate = make_test_gate()
    for fact_id in active_ids:
        knowledge_store_admin.promote_fact(fact_id, gate)

    snapshot = knowledge_store_admin.graph_snapshot(limit_nodes=10)

    returned_active = {
        fact["id"] for fact in snapshot["facts"] if fact["status"] == FactStatus.ACTIVE.value
    }
    assert returned_active == set(active_ids)


def test_graph_snapshot_can_still_include_quarantined_rows(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """The active-only window is a default, not a capability removal."""
    quarantined_ids = seed_facts(knowledge_store_admin, 3)

    snapshot = knowledge_store_admin.graph_snapshot(limit_nodes=10, include_quarantined=True)

    returned = {fact["id"] for fact in snapshot["facts"]}
    assert set(quarantined_ids) <= returned
