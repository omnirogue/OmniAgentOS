"""Write extracted episode knowledge to a :class:`KnowledgeStore`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from omniagentos.knowledge.contracts import (
    SOURCE_TRUST,
    CandidateEntity,
    EdgeType,
    EpisodeSource,
    Extractor,
    IngestResult,
    NodeKind,
)
from omniagentos.knowledge.extract import (
    DeterministicReflectionExtractor,
    FixtureExtractor,
    LLMExtractor,
)
from omniagentos.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

_CONTRADICTION_SIMILARITY = 0.85


def ingest_episode(
    store: KnowledgeStore,
    *,
    source: str,
    content: str,
    source_ref: str | None = None,
    agent_id: str | None = None,
    discipline: str | None = None,
    extractor: Extractor | None = None,
    trust: float | None = None,
) -> IngestResult:
    """Add an episode and persist its extracted facts, entities, and graph edges.

    Facts are deliberately left quarantined by the store's default/trigger.  Similar
    active facts are only flagged through a ``contradicts`` edge; promotion decisions
    belong to the consolidator.
    """
    try:
        episode_source = EpisodeSource(source)
    except ValueError as exc:
        raise ValueError(f"Unsupported episode source: {source!r}") from exc

    episode_id = store.add_episode(
        source=episode_source.value,
        content=content,
        source_ref=source_ref,
        agent_id=agent_id,
        discipline=discipline,
    )
    extraction = (extractor or LLMExtractor()).extract(content, discipline=discipline)
    effective_trust = SOURCE_TRUST[episode_source.value] if trust is None else trust

    entity_ids_by_name, entity_ids = _resolve_entities(store, extraction.entities)
    fact_ids: list[int] = []
    edge_ids: list[int] = []
    contradictions: list[dict[str, Any]] = []

    for fact in extraction.facts:
        embedding = _embed(store, fact.statement)
        fact_id = store.add_fact(
            statement=fact.statement,
            episode_id=episode_id,
            discipline=discipline,
            provenance=fact.provenance.value,
            trust=effective_trust,
            confidence=fact.confidence,
            importance=fact.importance,
            embedding=embedding,
        )
        fact_ids.append(fact_id)

        for entity_name in fact.entities:
            matched_entity_ids = entity_ids_by_name.get(entity_name)
            if matched_entity_ids is None:
                # Models sometimes mention an entity in a fact but omit its entity row.
                matched_entity_ids = [_upsert_implicit_entity(store, entity_name)]
                entity_ids_by_name[entity_name] = matched_entity_ids
            for entity_id in matched_entity_ids:
                if entity_id not in entity_ids:
                    entity_ids.append(entity_id)
                edge_ids.append(
                    store.add_edge(
                        src_kind=NodeKind.FACT.value,
                        src_id=fact_id,
                        dst_kind=NodeKind.ENTITY.value,
                        dst_id=entity_id,
                        edge_type=EdgeType.ABOUT.value,
                        weight=1.0,
                    )
                )

        if embedding is not None:
            match = _nearest_active_fact(store, embedding, fact_id)
            if match is not None:
                matched_fact_id, similarity = match
                edge_id = store.add_edge(
                    src_kind=NodeKind.FACT.value,
                    src_id=fact_id,
                    dst_kind=NodeKind.FACT.value,
                    dst_id=matched_fact_id,
                    edge_type=EdgeType.CONTRADICTS.value,
                    weight=similarity,
                )
                edge_ids.append(edge_id)
                contradictions.append(
                    {
                        "fact_id": fact_id,
                        "matched_fact_id": matched_fact_id,
                        "similarity": similarity,
                        "detected_at": datetime.now(UTC).isoformat(),
                    }
                )

    for edge in extraction.edges:
        if not (
            0 <= edge.src_statement_idx < len(fact_ids)
            and 0 <= edge.dst_statement_idx < len(fact_ids)
        ):
            logger.warning("Skipping extracted edge with out-of-range fact indexes: %s", edge)
            continue
        edge_ids.append(
            store.add_edge(
                src_kind=NodeKind.FACT.value,
                src_id=fact_ids[edge.src_statement_idx],
                dst_kind=NodeKind.FACT.value,
                dst_id=fact_ids[edge.dst_statement_idx],
                edge_type=edge.edge_type.value,
                weight=1.0,
            )
        )

    return IngestResult(
        episode_id=episode_id,
        fact_ids=fact_ids,
        entity_ids=entity_ids,
        edge_ids=edge_ids,
        contradictions=contradictions,
    )


def ingest_run_reflection(
    store: KnowledgeStore,
    run_row: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    extractor: Extractor | None = None,
) -> IngestResult | None:
    """Ingest a deterministic reflection for a run that has recorded steps."""
    if not run_row or not steps:
        return None

    reflection_extractor = extractor or DeterministicReflectionExtractor(run_row, steps)
    if isinstance(reflection_extractor, DeterministicReflectionExtractor):
        reflection_content = reflection_extractor.reflection_content()
    else:
        title = str(run_row.get("task_title") or run_row.get("id") or "unknown")
        state = str(run_row.get("state") or "unknown")
        completed = sum(step.get("status") == "completed" for step in steps)
        reflection_content = (
            f"Executed task '{title}' with outcome {state}. {len(steps)} steps; "
            f"completed={completed}. Lessons: No explicit lesson recorded."
        )
    return ingest_episode(
        store,
        source=EpisodeSource.RUN.value,
        source_ref=str(run_row["id"]),
        content=reflection_content,
        discipline=_optional_string(run_row.get("discipline")),
        extractor=reflection_extractor,
    )


def _resolve_entities(
    store: KnowledgeStore, entities: list[CandidateEntity]
) -> tuple[dict[str, list[int]], list[int]]:
    """Resolve extracted entities by their unique ``(name, kind)`` identity."""
    result: dict[str, list[int]] = {}
    entity_ids: list[int] = []
    for entity in entities:
        existing = store.get_entity(entity.name, entity.kind)
        entity_id = (
            existing.id
            if existing is not None
            else store.upsert_entity(
                name=entity.name,
                kind=entity.kind,
                summary=entity.summary,
                name_embedding=_embed(store, entity.name),
            )
        )
        result.setdefault(entity.name, []).append(entity_id)
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return result, entity_ids


def _upsert_implicit_entity(store: KnowledgeStore, name: str) -> int:
    """Resolve a fact-only entity reference as a concept when needed."""
    existing = store.get_entity(name, "concept")
    if existing is not None:
        return existing.id
    return store.upsert_entity(name=name, kind="concept", name_embedding=_embed(store, name))


def _embed(store: KnowledgeStore, text: str) -> list[float] | None:
    """Embed text when an optional provider is configured and available."""
    embedder = store._embedder
    if embedder is None:
        return None
    try:
        embeddings = embedder.embed([text])
        return embeddings[0] if embeddings else None
    except Exception as exc:  # An unavailable embedder must not prevent ingest.
        logger.warning("Knowledge embedding unavailable; storing unembedded content: %s", exc)
        return None


def _nearest_active_fact(
    store: KnowledgeStore, embedding: list[float], new_fact_id: int
) -> tuple[int, float] | None:
    """Find the closest active fact above the 0.85 cosine-similarity threshold."""
    # 0.85 intentionally flags only very-near neighbours; a consolidator judges them later.
    try:
        from pgvector import Vector

        with store._lock:
            connection = store._conn()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, 1 - (embedding <=> %s::halfvec) AS similarity
                    FROM facts
                    WHERE status = 'active'
                      AND invalid_at IS NULL
                      AND capability_scope IS NULL
                      AND embedding IS NOT NULL
                      AND id <> %s
                    ORDER BY embedding <=> %s::halfvec
                    LIMIT 1
                    """,
                    (Vector(embedding), new_fact_id, Vector(embedding)),
                )
                row = cursor.fetchone()
    except Exception as exc:  # The similarity optimization must remain best-effort.
        logger.warning("Could not check fact similarity for contradictions: %s", exc)
        return None
    if row is None:
        return None
    fact_id, similarity = int(row[0]), float(row[1])
    return (fact_id, similarity) if similarity > _CONTRADICTION_SIMILARITY else None


def _optional_string(value: object) -> str | None:
    """Convert a possibly missing mapping value to an optional string."""
    return str(value) if value is not None else None


__all__ = ["FixtureExtractor", "ingest_episode", "ingest_run_reflection"]
