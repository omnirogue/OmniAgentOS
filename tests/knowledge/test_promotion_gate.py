"""Test the PromotionGate F1/R2 boundary — the REAL database boundary (not the Python guard).

This test attacks BOTH layers but FOCUSES on the PostgreSQL GRANT boundary:
1. Raw psycopg connections as knowledge_agent that assert database denials
2. Python-layer PromotionGate + store checks (defense-in-depth)

The F1 boundary is the PostgreSQL grants, not the Python code. We prove this
by opening a raw psycopg connection as knowledge_agent and asserting each
mutation that store.promote_fact/demote_fact/set_embedding would perform
is DENIED by the database, raising psycopg.errors.InsufficientPrivilege.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from omniagentos.knowledge.config import require_pg
from omniagentos.knowledge.contracts import PromotionDenied
from omniagentos.knowledge.store import KnowledgeStore

pytestmark = pytest.mark.skipif(
    not require_pg(),
    reason="OMNIAGENTOS_REQUIRE_PG not set; skipping live DB tests",
)


# Get the isolated test DB name from conftest
_ISOLATED_TESTDB = (
    "omniagentos_knowledge_test_" + os.path.basename(os.getcwd()).replace("-", "_").lower()
)
AGENT_DSN = f"postgresql://knowledge_agent@localhost/{_ISOLATED_TESTDB}"


class TestPromotionGatePython:
    """Python-level gate construction tests (defense-in-depth)."""

    def test_gate_cannot_be_constructed_without_sentinel(self) -> None:
        """PromotionGate.__init__ must reject construction without _GATE_TOKEN."""
        from omniagentos.knowledge.contracts import PromotionGate

        # Pass an invalid token (not the module-private sentinel)
        with pytest.raises(PromotionDenied, match="only be constructed"):
            PromotionGate(holder="test", _token=object())  # type: ignore

    def test_gate_can_only_be_constructed_via_module(self) -> None:
        """gate() is the ONLY way to mint a PromotionGate."""
        from omniagentos.knowledge.promotion import gate

        g = gate()
        assert g.holder == "consolidator"
        assert g is not None


class TestPromotionGateDatabaseBoundary:
    """The REAL F1/R2 boundary: PostgreSQL role-separation tests (raw psycopg).

    These tests connect as knowledge_agent and assert the database itself
    denies mutations that store.promote_fact/demote_fact would make.
    ZERO skips — these MUST pass when PG is up.
    """

    def test_agent_cannot_update_facts_status(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent role cannot UPDATE facts.status (psycopg.errors.InsufficientPrivilege)."""
        # Seed a test fact via admin role

        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        # Now connect as agent and try to UPDATE status
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET status = 'active' WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_trust(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot UPDATE facts.trust (permission denied)."""
        # Seed a fact
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET trust = 1.0 WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_importance(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """knowledge_agent cannot UPDATE facts.importance (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET importance = 1.0 WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_confidence(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """knowledge_agent cannot UPDATE facts.confidence (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET confidence = 1.0 WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_embedding(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """knowledge_agent cannot UPDATE facts.embedding (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET embedding = NULL WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_invalid_at(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """knowledge_agent cannot UPDATE facts.invalid_at (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE facts SET invalid_at = now() WHERE id = %s", (fact_id,))

    def test_agent_cannot_update_facts_superseded_by(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """knowledge_agent cannot UPDATE facts.superseded_by (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "UPDATE facts SET superseded_by = %s WHERE id = %s", (fact_id + 1, fact_id)
                    )

    def test_agent_cannot_delete_facts(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot DELETE from facts (permission denied)."""
        episode_id = knowledge_store_admin.add_episode(
            source="run", content="test", source_ref="test-ref"
        )
        fact_id = knowledge_store_admin.add_fact(
            statement="Test fact",
            episode_id=episode_id,
            provenance="extracted",
            confidence=0.7,
            importance=0.5,
        )

        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("DELETE FROM facts WHERE id = %s", (fact_id,))

    def test_agent_cannot_delete_edges(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot DELETE from edges (permission denied)."""
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("DELETE FROM edges WHERE id = 1")

    def test_agent_cannot_delete_episodes(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot DELETE from episodes (permission denied)."""
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("DELETE FROM episodes WHERE id = 1")

    def test_agent_cannot_update_entities(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot UPDATE entities (permission denied)."""
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE entities SET summary = 'test' WHERE id = 1")

    def test_agent_cannot_update_kb_meta(self, knowledge_store_admin: KnowledgeStore) -> None:
        """knowledge_agent cannot UPDATE kb_meta (permission denied)."""
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("UPDATE kb_meta SET value = 'test' WHERE key = 'test'")

    def test_agent_insert_active_status_quarantined_by_trigger(
        self, knowledge_store_admin: KnowledgeStore
    ) -> None:
        """Agent INSERT with status='active' is clamped to 'quarantined' by trigger.

        The agent authors its OWN episode first (migration 003 forbids an agent fact from
        referencing an episode authored by another role) — this is the real ingest path.
        """
        with psycopg.connect(AGENT_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO episodes (source, content, source_ref) VALUES ('run','test','test-ref') RETURNING id"
                )
                episode_id = cur.fetchone()[0]
                # Try to INSERT with status='active'
                cur.execute(
                    """
                    INSERT INTO facts
                    (statement, discipline, scope, episode_id, provenance, trust, confidence,
                     status, valid_at, recorded_at, importance, access_count, last_accessed, helped_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s, now(), %s)
                    RETURNING status
                    """,
                    (
                        "Inserted fact",
                        None,
                        "global",
                        episode_id,
                        "extracted",
                        0.6,
                        0.7,
                        "active",  # Try to INSERT active
                        0.5,
                        0,
                        0,
                    ),
                )
                result = cur.fetchone()
                # The trigger should have clamped it to 'quarantined'
                assert result[0] == "quarantined", f"Expected 'quarantined' but got '{result[0]}'"


class TestPromotionGatePythonLayer:
    """Python-layer defense-in-depth tests."""

    def test_store_promote_fact_checks_admin_role(self, knowledge_store: KnowledgeStore) -> None:
        """KnowledgeStore.promote_fact rejects agent-role store."""
        from omniagentos.knowledge.promotion import gate

        # knowledge_store fixture is agent-role
        assert knowledge_store._role == "knowledge_agent"

        g = gate()
        with pytest.raises(PromotionDenied):
            knowledge_store.promote_fact(fact_id=1, gate=g)

    def test_store_demote_fact_checks_admin_role(self, knowledge_store: KnowledgeStore) -> None:
        """KnowledgeStore.demote_fact rejects agent-role store."""
        from omniagentos.knowledge.promotion import gate

        assert knowledge_store._role == "knowledge_agent"

        g = gate()
        with pytest.raises(PromotionDenied):
            knowledge_store.demote_fact(fact_id=1, gate=g)

    def test_store_set_embedding_checks_admin_role(self, knowledge_store: KnowledgeStore) -> None:
        """KnowledgeStore.set_embedding rejects agent-role store."""
        assert knowledge_store._role == "knowledge_agent"

        with pytest.raises(PromotionDenied):
            knowledge_store.set_embedding(fact_id=1, embedding=[0.1] * 1024)

    def test_store_supersede_fact_checks_admin_role(self, knowledge_store: KnowledgeStore) -> None:
        """KnowledgeStore.supersede_fact rejects agent-role store."""
        assert knowledge_store._role == "knowledge_agent"

        with pytest.raises(PromotionDenied):
            knowledge_store.supersede_fact(loser_id=1, winner_id=2)

    def test_store_invalidate_edge_checks_admin_role(self, knowledge_store: KnowledgeStore) -> None:
        """KnowledgeStore.invalidate_edge rejects agent-role store."""
        assert knowledge_store._role == "knowledge_agent"

        with pytest.raises(PromotionDenied):
            knowledge_store.invalidate_edge(edge_id=1)
