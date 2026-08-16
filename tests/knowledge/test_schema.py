"""Test: schema introspection and role separation."""

from __future__ import annotations

from omniagentos.knowledge.config import test_dsn
from omniagentos.knowledge.store import KnowledgeStore


def test_kb_meta_seeded(knowledge_store: KnowledgeStore) -> None:
    """kb_meta table is seeded with model/dim at migration time."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kb_meta WHERE key = 'embed_model'")
            model = cur.fetchone()[0]
            assert model == "ollama:bge-m3"

            cur.execute("SELECT value FROM kb_meta WHERE key = 'embed_dim'")
            dim = cur.fetchone()[0]
            assert int(dim) == 1024


def test_roles_exist(knowledge_store: KnowledgeStore) -> None:
    """knowledge_agent and knowledge_admin roles exist."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_roles WHERE rolname IN ('knowledge_agent', 'knowledge_admin')
                GROUP BY rolname ORDER BY rolname
                """
            )
            rows = cur.fetchall()
            assert len(rows) == 2, "Both knowledge_agent and knowledge_admin roles must exist"


def test_trigger_exists(knowledge_store: KnowledgeStore) -> None:
    """The enforce_agent_insert_floor trigger exists on facts."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'facts_agent_insert_floor' AND tgrelid::regclass::text = 'facts'
                """
            )
            assert cur.fetchone() is not None, "Trigger must exist"


def test_hnsw_index_exists(knowledge_store: KnowledgeStore) -> None:
    """HNSW index on facts.embedding exists."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'facts' AND indexname = 'facts_embedding_idx'
                """
            )
            assert cur.fetchone() is not None, "HNSW index must exist"


def test_gin_index_exists(knowledge_store: KnowledgeStore) -> None:
    """GIN index on facts.search_tsv exists."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'facts' AND indexname = 'facts_tsv_idx'
                """
            )
            assert cur.fetchone() is not None, "GIN index must exist"


def test_check_constraints_exist(knowledge_store: KnowledgeStore) -> None:
    """CHECK constraints are in place for enums and ranges."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            # Check trust constraint
            cur.execute(
                """
                SELECT 1 FROM pg_constraint
                WHERE conrelid::regclass::text = 'facts' AND conname LIKE '%trust%'
                """
            )
            assert cur.fetchone() is not None, "trust CHECK constraint must exist"


def test_not_null_constraints(knowledge_store: KnowledgeStore) -> None:
    """NOT NULL constraints are on required columns."""
    import psycopg

    with psycopg.connect(test_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT attname FROM pg_attribute
                WHERE attrelid::regclass::text = 'facts' AND attnotnull = true
                """
            )
            not_null_cols = {row[0] for row in cur.fetchall()}
            required = {"id", "statement", "episode_id", "status", "trust", "confidence"}
            assert required.issubset(not_null_cols), (
                f"Missing NOT NULL on {required - not_null_cols}"
            )
