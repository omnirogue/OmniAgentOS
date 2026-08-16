"""FastAPI dependencies for the API package."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Query

from omniagentos.contracts import Store
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.policy import PolicyConfig, load_policy


def _api_reliability_store(db_path: str) -> Store:
    """Build the reliability store without breaking the legacy API event seam.

    ``SqliteReliabilityStore`` used to *shadow* ``insert_event`` for rows in
    ``reliability_events``, which is incompatible with the frozen API ``Store``
    contract (different params, return type, and table). The concrete store now
    keeps the base SSE ``insert_event`` and exposes reliability writes only as
    ``insert_reliability_event``. No shape-sniffing or positional dual-dispatch.
    """
    from omniagentos.reliability.store import SqliteReliabilityStore

    return SqliteReliabilityStore(db_path=db_path)


@lru_cache(maxsize=1)
def get_store() -> Store:
    """Construct the process-wide persistence seam on first use."""
    # Keep both imports in the factory.  p01 owns the concrete implementation and
    # callers (including tests) may select a database path by changing the
    # contracts-level default before an application request is made.
    from omniagentos import contracts

    return _api_reliability_store(db_path=contracts.default_db_path())


StoreDep = Annotated[Store, Depends(get_store)]


@lru_cache(maxsize=1)
def get_policy_config() -> PolicyConfig:
    """Load and cache the process-wide policy used for enqueue validation."""
    return load_policy()


PolicyDep = Annotated[PolicyConfig, Depends(get_policy_config)]


@lru_cache(maxsize=1)
def get_knowledge_store() -> KnowledgeStore:
    """Return the agent-role store for knowledge reads and agent ingestion."""
    from omniagentos.knowledge.config import dsn as get_dsn
    from omniagentos.knowledge.embeddings import OllamaEmbedding
    from omniagentos.knowledge.store import KnowledgeStore

    return KnowledgeStore(dsn=get_dsn(), embedder=OllamaEmbedding())


@lru_cache(maxsize=1)
def get_knowledge_admin_store() -> KnowledgeStore:
    """Return the admin-role store for operator mutations.

    Fail loudly if the admin DSN is unconfigured: an empty DSN falls through
    KnowledgeStore's `dsn or default_dsn()` to the AGENT DSN, so an operator's
    high-trust vault/human ingest would silently run as knowledge_agent and get
    trigger-clamped to quarantined/trust≤0.6 (council finding). Never let the admin
    store degrade to the agent role.
    """
    from omniagentos.knowledge.config import admin_dsn as get_admin_dsn
    from omniagentos.knowledge.embeddings import OllamaEmbedding
    from omniagentos.knowledge.store import KnowledgeStore

    dsn = get_admin_dsn()
    if not dsn:
        raise RuntimeError(
            "OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN is not configured — the operator/admin "
            "knowledge store is unavailable. Refusing to fall back to the agent role."
        )
    return KnowledgeStore(dsn=dsn, embedder=OllamaEmbedding())


KnowledgeStoreDep = Annotated[KnowledgeStore, Depends(get_knowledge_store)]
KnowledgeAdminStoreDep = Annotated[KnowledgeStore, Depends(get_knowledge_admin_store)]


def get_pagination_limit(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> int:
    return limit
