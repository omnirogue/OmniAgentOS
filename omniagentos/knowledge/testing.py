"""Test helpers for the knowledge subsystem."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omniagentos.knowledge.contracts import (
    _GATE_TOKEN,
    EMBED_DIM,
    EpisodeSource,
    PromotionGate,
    Provenance,
)
from omniagentos.knowledge.embeddings import FakeEmbedding
from omniagentos.knowledge.evidence import EvidenceLedger
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore

if TYPE_CHECKING:
    from omniagentos.knowledge.store import KnowledgeStore


def make_test_gate(holder: str = "test") -> PromotionGate:
    """Create a PromotionGate for tests. Uses the real frozen sentinel from contracts —
    a legitimate test-only capability (this module is not agent-reachable). The enforced
    boundary is the DB grant (knowledge_agent cannot UPDATE facts.status even holding a
    gate); the gate is defense-in-depth above it."""
    return PromotionGate(holder=holder, _token=_GATE_TOKEN)


def make_fake_embedder() -> FakeEmbedding:
    """Create a FakeEmbedding instance for tests."""
    return FakeEmbedding(dim=EMBED_DIM)


def make_memory_store(
    *,
    role: str = "knowledge_admin",
    evidence: EvidenceLedger | None = None,
) -> InMemoryKnowledgeStore:
    """Isolated in-memory store + fake embedder for knowledge-integrity tests."""
    return InMemoryKnowledgeStore(
        role=role,
        embedder=make_fake_embedder(),
        evidence=evidence if evidence is not None else EvidenceLedger(),
    )


def seed_facts(store: KnowledgeStore, n: int = 10) -> list[int]:
    """Seed the store with n test facts."""
    fact_ids = []
    embedder = make_fake_embedder()

    for i in range(n):
        # Create an episode
        episode_id = store.add_episode(
            source=EpisodeSource.RUN.value,
            content=f"Test episode {i}",
            source_ref=f"test-{i}",
        )

        # Create a fact
        text = f"Test fact {i}: The quick brown fox jumps over the lazy dog"
        embedding = embedder.embed([text])[0]

        fact_id = store.add_fact(
            statement=text,
            episode_id=episode_id,
            discipline="test",
            provenance=Provenance.EXTRACTED.value,
            trust=0.5,
            embedding=embedding,
        )
        fact_ids.append(fact_id)

    return fact_ids
