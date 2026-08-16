"""Test: degraded operation when knowledge DB is unavailable."""

from __future__ import annotations

import pytest

from omniagentos.knowledge.contracts import KnowledgeUnavailable
from omniagentos.knowledge.store import KnowledgeStore

# Only the *deadness* matters to these tests, so the database is named
# test-only. It used to be spelled ``omniagentos_knowledge`` — the operator's
# LIVE knowledge database — which was dead solely because nothing happens to
# listen on 9999; anything that ever bound that port would have turned these
# degraded-path tests into production traffic. tests/conftest.py's
# ``_refuse_production_knowledge_db`` now rejects that name outright.
DEAD_DSN = "postgresql://localhost:9999/omniagentos_knowledge_test_dead"


def test_store_dead_dsn_ping_false() -> None:
    """Construction with a dead DSN must NOT raise (G3-1.5 graceful absence): the store
    is a long-lived process-wide singleton that has to survive a down/absent KB. It
    constructs dormant and reports ping()=False."""
    store = KnowledgeStore(dsn=DEAD_DSN)
    assert store.ping() is False
    store.close()


def test_store_operation_on_dead_store_raises_unavailable() -> None:
    """An OPERATION against a dead store raises KnowledgeUnavailable — which the runner's
    safe_recall_block catches and turns into a no-op recall. (Construction does not raise;
    use — which needs a live connection — does.)"""
    store = KnowledgeStore(dsn=DEAD_DSN)
    with pytest.raises(KnowledgeUnavailable):
        store.recall_candidates(embedding=None, query_text="anything", k=5)
    store.close()


def test_recall_candidates_on_dead_store() -> None:
    """recall_candidates on a down store surfaces KnowledgeUnavailable (never hangs, never
    returns silently-wrong data). connect_timeout bounds the failure to a few seconds."""
    store = KnowledgeStore(dsn=DEAD_DSN)
    assert store.ping() is False
    with pytest.raises(KnowledgeUnavailable):
        store.recall_candidates(embedding=None, query_text="q", discipline=None, k=10)
    store.close()
