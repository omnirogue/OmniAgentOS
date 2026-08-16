"""Write-pipeline coverage using deterministic extraction fixtures."""

from __future__ import annotations

import shutil

import pytest

from omniagentos.knowledge.contracts import EdgeType, EpisodeSource, FactStatus, Provenance
from omniagentos.knowledge.extract import FixtureExtractor, LLMExtractor
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.testing import make_test_gate


def test_fixture_pipeline_creates_quarantined_graph(knowledge_store: object) -> None:
    store = knowledge_store
    result = ingest_episode(
        store,
        source=EpisodeSource.RUN.value,
        content=(
            "ENTITY: Synapse | system\n"
            "FACT: Synapse stores episode facts. | Synapse\n"
            "FACT: Facts start quarantined. | Synapse\n"
            "EDGE: 0, 1, follows"
        ),
        extractor=FixtureExtractor(),
    )

    assert len(result.fact_ids) == 2
    assert result.entity_ids
    assert result.edge_ids
    assert all(
        store.get_fact(fact_id).status is FactStatus.QUARANTINED for fact_id in result.fact_ids
    )
    assert any(edge["type"] == EdgeType.FOLLOWS.value for edge in store.graph_snapshot()["edges"])


def test_near_active_fact_gets_contradiction_edge(knowledge_store_admin: object) -> None:
    store = knowledge_store_admin
    episode_id = store.add_episode(source="run", content="seed")
    statement = "The deployment completed successfully."
    seed_id = store.add_fact(
        statement=statement,
        episode_id=episode_id,
        provenance=Provenance.EXTRACTED.value,
        embedding=store._embedder.embed([statement])[0],
    )
    store.promote_fact(seed_id, make_test_gate())

    result = ingest_episode(
        store,
        source="run",
        content=f"FACT: {statement}",
        extractor=FixtureExtractor(),
    )

    assert result.contradictions
    assert any(
        edge["type"] == EdgeType.CONTRADICTS.value for edge in store.graph_snapshot()["edges"]
    )
    assert store.get_fact(result.fact_ids[0]).status is FactStatus.QUARANTINED


@pytest.mark.live
def test_live_claude_extraction() -> None:
    """A real adapter smoke test; skip when Claude CLI authentication is absent."""
    if shutil.which("claude") is None:
        pytest.skip("cli-claude is not installed/authenticated")
    extracted = LLMExtractor().extract("The Apollo program landed humans on the Moon in 1969.")
    if not extracted.facts:
        pytest.skip("cli-claude is not authenticated or returned no extraction")
    assert extracted.facts[0].statement
