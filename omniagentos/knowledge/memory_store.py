"""In-memory KnowledgeStore stand-in for isolated integrity tests.

No PostgreSQL, no network, no live embeddings provider. Use with FakeEmbedding.
Implements the consolidator-facing surface used by promotion / contradiction /
embedding-backfill integrity paths.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from omniagentos.knowledge.contracts import (
    EMBED_DIM,
    Edge,
    EmbeddingProvider,
    Episode,
    Fact,
    FactStatus,
    PromotionDenied,
    Provenance,
)
from omniagentos.knowledge.evidence import EvidenceLedger
from omniagentos.knowledge.promotion import gate as make_gate


@dataclass
class _FactRow:
    id: int
    statement: str
    episode_id: int
    discipline: str | None = None
    scope: str = "global"
    provenance: str = Provenance.EXTRACTED.value
    trust: float = 0.5
    confidence: float = 0.7
    importance: float = 0.5
    status: str = FactStatus.QUARANTINED.value
    valid_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    invalid_at: datetime | None = None
    superseded_by: int | None = None
    access_count: int = 0
    last_accessed: datetime = field(default_factory=lambda: datetime.now(UTC))
    helped_count: int = 0
    embedding: list[float] | None = None
    author_role: str = "knowledge_admin"
    capability_scope: str | None = None
    company_id: str | None = None
    domains: list[str] = field(default_factory=list)
    capability_kind: str | None = None
    capability_provenance: str | None = None
    last_verified: datetime | None = None
    promoted_from: int | None = None


@dataclass
class _EpisodeRow:
    id: int
    source: str
    content: str
    source_ref: str | None = None
    agent_id: str | None = None
    discipline: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    author_role: str = "knowledge_admin"


@dataclass
class _EdgeRow:
    id: int
    src_kind: str
    src_id: int
    dst_kind: str
    dst_id: int
    edge_type: str
    weight: float = 0.5
    valid_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    invalid_at: datetime | None = None


class InMemoryKnowledgeStore:
    """Isolated fake store for knowledge-integrity unit tests."""

    def __init__(
        self,
        *,
        role: str = "knowledge_admin",
        embedder: EmbeddingProvider | None = None,
        evidence: EvidenceLedger | None = None,
    ) -> None:
        self._lock = RLock()
        self._role = role
        self._embedder = embedder
        self.evidence = evidence if evidence is not None else EvidenceLedger()
        self._facts: dict[int, _FactRow] = {}
        self._episodes: dict[int, _EpisodeRow] = {}
        self._edges: dict[int, _EdgeRow] = {}
        self._next_fact = 1
        self._next_episode = 1
        self._next_edge = 1
        # Optional hooks for adversarial phase-failure injection in tests.
        self.fail_phases: set[str] = set()
        self.embed_return_count: int | None = None  # if set, truncate embed results

    # --- write ---

    def add_episode(
        self,
        *,
        source: str,
        content: str,
        source_ref: str | None = None,
        agent_id: str | None = None,
        discipline: str | None = None,
    ) -> int:
        with self._lock:
            # Mirror migration 002 clamp: agent may only write run/chat/web.
            src = source
            author = self._role
            if self._role == "knowledge_agent":
                if src not in ("run", "chat", "web"):
                    src = "run"
            eid = self._next_episode
            self._next_episode += 1
            self._episodes[eid] = _EpisodeRow(
                id=eid,
                source=src,
                content=content,
                source_ref=source_ref,
                agent_id=agent_id,
                discipline=discipline,
                author_role=author,
            )
            return eid

    def add_fact(
        self,
        *,
        statement: str,
        episode_id: int,
        discipline: str | None = None,
        scope: str = "global",
        provenance: str = "extracted",
        trust: float = 0.5,
        confidence: float = 0.7,
        importance: float = 0.5,
        embedding: list[float] | None = None,
        capability_scope: str | None = None,
        company_id: str | None = None,
        domains: list[str] | None = None,
        capability_kind: str | None = None,
        capability_provenance: str | None = None,
        last_verified: str | None = None,
        promoted_from: int | None = None,
    ) -> int:
        with self._lock:
            ep = self._episodes.get(episode_id)
            if ep is None:
                raise KeyError(f"episode {episode_id} not found")
            if self._role == "knowledge_agent" and ep.author_role != "knowledge_agent":
                raise PermissionError(
                    f"knowledge_agent fact may not reference an episode authored by {ep.author_role}"
                )
            status = FactStatus.QUARANTINED.value
            t = trust
            imp = importance
            conf = confidence
            if self._role == "knowledge_agent":
                t = min(t, 0.6)
                imp = min(imp, 0.6)
                conf = min(conf, 0.7)
            fid = self._next_fact
            self._next_fact += 1
            self._facts[fid] = _FactRow(
                id=fid,
                statement=statement,
                episode_id=episode_id,
                discipline=discipline,
                scope=scope,
                provenance=provenance,
                trust=t,
                confidence=conf,
                importance=imp,
                status=status,
                embedding=list(embedding) if embedding is not None else None,
                author_role=self._role,
                capability_scope=capability_scope,
                company_id=company_id,
                domains=list(domains or []),
                capability_kind=capability_kind,
                capability_provenance=capability_provenance,
                last_verified=(datetime.fromisoformat(last_verified) if last_verified else None),
                promoted_from=promoted_from,
            )
            return fid

    def add_edge(
        self,
        *,
        src_kind: str,
        src_id: int,
        dst_kind: str,
        dst_id: int,
        edge_type: str,
        weight: float = 0.5,
    ) -> int:
        with self._lock:
            for e in self._edges.values():
                if (
                    e.src_kind == src_kind
                    and e.src_id == src_id
                    and e.dst_kind == dst_kind
                    and e.dst_id == dst_id
                    and e.edge_type == edge_type
                ):
                    return e.id
            eid = self._next_edge
            self._next_edge += 1
            self._edges[eid] = _EdgeRow(
                id=eid,
                src_kind=src_kind,
                src_id=src_id,
                dst_kind=dst_kind,
                dst_id=dst_id,
                edge_type=edge_type,
                weight=weight,
            )
            return eid

    def supersede_fact(self, loser_id: int, winner_id: int) -> None:
        self._check_admin("supersede_fact")
        with self._lock:
            row = self._facts[loser_id]
            row.status = FactStatus.SUPERSEDED.value
            row.invalid_at = datetime.now(UTC)
            row.superseded_by = winner_id

    def promote_fact(self, fact_id: int, gate: Any) -> None:
        self._check_admin("promote_fact")
        if gate is None:
            raise PromotionDenied("PromotionGate required")
        with self._lock:
            row = self._facts[fact_id]
            row.status = FactStatus.ACTIVE.value

    def demote_fact(self, fact_id: int, gate: Any) -> None:
        self._check_admin("demote_fact")
        with self._lock:
            self._facts[fact_id].status = FactStatus.QUARANTINED.value

    def activate_capability(self, fact_id: int) -> None:
        self._check_admin("activate_capability")
        with self._lock:
            row = self._facts[fact_id]
            if row.capability_scope is None:
                raise ValueError("activate_capability requires a capability fact")
            row.status = FactStatus.ACTIVE.value

    def promote_capability_to_estate(
        self,
        source_fact_id: int,
        *,
        statement: str,
        actor: str,
        last_verified: str,
        embedding: list[float] | None = None,
    ) -> int:
        self._check_admin("promote_capability_to_estate")
        del actor
        with self._lock:
            source = self._facts[source_fact_id]
            if source.capability_scope != "company" or not source.company_id:
                raise ValueError("estate promotion requires a company capability note")
            fid = self._next_fact
            self._next_fact += 1
            self._facts[fid] = _FactRow(
                id=fid,
                statement=statement,
                episode_id=source.episode_id,
                discipline=source.discipline,
                scope="global",
                provenance=source.provenance,
                trust=source.trust,
                confidence=source.confidence,
                importance=source.importance,
                status=FactStatus.ACTIVE.value,
                embedding=list(embedding) if embedding is not None else None,
                author_role=self._role,
                capability_scope="estate",
                company_id=None,
                domains=list(source.domains),
                capability_kind=source.capability_kind,
                capability_provenance=source.capability_provenance,
                last_verified=datetime.fromisoformat(last_verified),
                promoted_from=source_fact_id,
            )
            return fid

    def set_embedding(self, fact_id: int, embedding: list[float]) -> None:
        self._check_admin("set_embedding")
        with self._lock:
            if len(embedding) != EMBED_DIM and self._embedder is not None:
                # Allow test embedders with matching dim; enforce store embedder dim when set.
                if len(embedding) != self._embedder.dim:
                    raise ValueError(
                        f"embedding length {len(embedding)} != embedder dim {self._embedder.dim}"
                    )
            self._facts[fact_id].embedding = list(embedding)

    # --- read / consolidator helpers ---

    def get_fact(self, fact_id: int) -> Fact | None:
        with self._lock:
            row = self._facts.get(fact_id)
            if row is None:
                return None
            return self._to_fact(row)

    def get_episode(self, episode_id: int) -> Episode | None:
        with self._lock:
            row = self._episodes.get(episode_id)
            if row is None:
                return None
            return Episode(
                id=row.id,
                source=row.source,  # type: ignore[arg-type]
                source_ref=row.source_ref,
                agent_id=row.agent_id,
                discipline=row.discipline,
                content=row.content,
                occurred_at=row.occurred_at,
            )

    def facts_missing_embedding(self, limit: int = 100) -> list[Fact]:
        with self._lock:
            missing = [
                self._to_fact(r)
                for r in self._facts.values()
                if r.embedding is None and r.invalid_at is None
            ]
            return missing[:limit]

    def list_quarantined_fact_ids(self) -> list[int]:
        with self._lock:
            return sorted(
                r.id
                for r in self._facts.values()
                if r.status == FactStatus.QUARANTINED.value
                and r.invalid_at is None
                and r.capability_scope is None
            )

    def count_non_agent_source_classes(self, fact_id: int) -> int:
        """Count distinct non-agent trust classes for a fact + same-statement peers."""
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None:
                return 0
            classes: set[str] = set()
            for r in self._facts.values():
                if r.invalid_at is not None:
                    continue
                if r.id != fact_id and r.statement != fact.statement:
                    continue
                if r.author_role == "knowledge_agent":
                    continue
                ep = self._episodes.get(r.episode_id)
                if ep is None:
                    continue
                classes.add(ep.source)
            return len(classes)

    def list_unresolved_contradiction_peers(self, fact_id: int) -> list[int]:
        """Peer fact ids linked by open CONTRADICTS edges that are still live."""
        with self._lock:
            peers: set[int] = set()
            for e in self._edges.values():
                if e.edge_type != "contradicts" or e.invalid_at is not None:
                    continue
                if e.src_kind != "fact" or e.dst_kind != "fact":
                    continue
                other: int | None = None
                if e.src_id == fact_id:
                    other = e.dst_id
                elif e.dst_id == fact_id:
                    other = e.src_id
                if other is None:
                    continue
                peer = self._facts.get(other)
                if peer is None or peer.invalid_at is not None:
                    continue
                if peer.status == FactStatus.SUPERSEDED.value:
                    continue
                if peer.capability_scope is not None:
                    continue
                peers.add(other)
            return sorted(peers)

    def list_contradicts_edges(self) -> list[tuple[int, int]]:
        with self._lock:
            out: list[tuple[int, int]] = []
            for e in self._edges.values():
                if (
                    e.edge_type == "contradicts"
                    and e.invalid_at is None
                    and e.src_kind == "fact"
                    and e.dst_kind == "fact"
                    and self._facts.get(e.src_id) is not None
                    and self._facts[e.src_id].capability_scope is None
                    and self._facts.get(e.dst_id) is not None
                    and self._facts[e.dst_id].capability_scope is None
                ):
                    out.append((e.src_id, e.dst_id))
            return sorted(out)

    def resolve_contradiction_edge(self, loser_id: int, winner_id: int, now: datetime) -> None:
        """Mark contradiction edges between loser and winner as resolved."""
        with self._lock:
            for edge in self._edges.values():
                if edge.edge_type != "contradicts":
                    continue
                if edge.invalid_at is not None:
                    continue
                if edge.src_kind != "fact" or edge.dst_kind != "fact":
                    continue
                if (edge.src_id == loser_id and edge.dst_id == winner_id) or (
                    edge.src_id == winner_id and edge.dst_id == loser_id
                ):
                    edge.invalid_at = now

    def list_active_embedded(self) -> list[tuple[int, list[float] | None, float]]:
        with self._lock:
            return [
                (r.id, r.embedding, r.trust)
                for r in sorted(self._facts.values(), key=lambda x: x.id)
                if r.status == FactStatus.ACTIVE.value
                and r.embedding is not None
                and r.invalid_at is None
                and r.capability_scope is None
            ]

    def recall_candidates(
        self,
        *,
        embedding: list[float] | None,
        query_text: str,
        discipline: str | None = None,
        agent_id: str | None = None,
        include_quarantined: bool = False,
        k: int = 50,
        mode: str = "full",
        capability_only: bool = False,
        company_id: str | None = None,
        domains: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Small deterministic parity seam for isolated semantic/scope tests."""
        del agent_id, mode
        query_terms = set(query_text.casefold().split())
        selected: list[_FactRow] = []
        for row in self._facts.values():
            if row.invalid_at is not None:
                continue
            if row.status != FactStatus.ACTIVE.value and not include_quarantined:
                continue
            if discipline and row.discipline != discipline:
                continue
            if capability_only and not (
                row.capability_scope == "estate"
                or (row.capability_scope == "company" and row.company_id == company_id)
            ):
                continue
            if not capability_only and row.capability_scope is not None:
                continue
            selected.append(row)

        def cosine(left: list[float], right: list[float]) -> float:
            dot = sum(a * b for a, b in zip(left, right, strict=False))
            left_norm = math.sqrt(sum(value * value for value in left))
            right_norm = math.sqrt(sum(value * value for value in right))
            return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

        vector_order = sorted(
            (
                (row.id, cosine(row.embedding, embedding))
                for row in selected
                if row.embedding is not None and embedding is not None
            ),
            key=lambda item: (-item[1], item[0]),
        )
        vector_ranks = {fact_id: rank for rank, (fact_id, _) in enumerate(vector_order, 1)}
        fts_order = sorted(
            (
                (row.id, len(query_terms & set(row.statement.casefold().split())))
                for row in selected
                if query_terms & set(row.statement.casefold().split())
            ),
            key=lambda item: (-item[1], item[0]),
        )
        fts_ranks = {fact_id: rank for rank, (fact_id, _) in enumerate(fts_order, 1)}
        domain_set = set(domains or [])
        output: list[dict[str, Any]] = []
        for row in selected:
            if row.id not in vector_ranks and row.id not in fts_ranks:
                continue
            fact = self._to_fact(row).model_dump()
            fact.update(
                {
                    "embedding": row.embedding,
                    "vector_rank": vector_ranks.get(row.id),
                    "fts_rank": fts_ranks.get(row.id),
                    "graph_activation": 0.0,
                    "domain_match": float(bool(domain_set & set(row.domains))),
                }
            )
            output.append(fact)
        output.sort(
            key=lambda item: (
                min(item.get("vector_rank") or 1_000_000, item.get("fts_rank") or 1_000_000),
                item["id"],
            )
        )
        return output[:k]

    def neighbors(self, node_kind: str, node_id: int, *, limit: int = 50) -> list[Edge]:
        with self._lock:
            edges: list[Edge] = []
            for e in self._edges.values():
                if e.invalid_at is not None:
                    continue
                if (e.src_kind == node_kind and e.src_id == node_id) or (
                    e.dst_kind == node_kind and e.dst_id == node_id
                ):
                    edges.append(
                        Edge(
                            id=e.id,
                            src_kind=e.src_kind,  # type: ignore[arg-type]
                            src_id=e.src_id,
                            dst_kind=e.dst_kind,  # type: ignore[arg-type]
                            dst_id=e.dst_id,
                            edge_type=e.edge_type,  # type: ignore[arg-type]
                            weight=e.weight,
                            valid_at=e.valid_at,
                            invalid_at=e.invalid_at,
                        )
                    )
                if len(edges) >= limit:
                    break
            return edges

    # --- promotion evidence (mirrors the KnowledgeStore/PostgreSQL surface) ---

    def has_valid_promotion_evidence(self, fact_id: int) -> bool:
        """Same predicate and same semantics as the PostgreSQL store."""
        return self.evidence.has_valid_verification(fact_id)

    def record_promotion_evidence(
        self,
        *,
        fact_id: int,
        provenance: str,
        source_identity: str,
        verifier_outcome: str,
        promotion_generation: int | None = None,
    ) -> int:
        """Admin-only, idempotent evidence write (PostgreSQL parity)."""
        self._check_admin("record_promotion_evidence")
        row = self.evidence.record(
            fact_id=fact_id,
            provenance=provenance,
            source_identity=source_identity,
            verifier_outcome=verifier_outcome,
            promotion_generation=promotion_generation,
            author_role=self._role,
        )
        return row.id

    def revoke_promotion_evidence_for_source(
        self, fact_id: int, source_identity: str, *, reason: str
    ) -> list[int]:
        """Admin-only revoke-by-source (PostgreSQL parity)."""
        self._check_admin("revoke_promotion_evidence_for_source")
        return self.evidence.revoke_for_source(fact_id, source_identity, reason=reason)

    def current_source_generation(self, source_identity: str) -> int:
        return self.evidence.current_source_generation(source_identity)

    def advance_source_generation(self, source_identity: str) -> int:
        """Admin-only source generation bump (PostgreSQL parity)."""
        self._check_admin("advance_source_generation")
        return self.evidence.advance_source_generation(source_identity)

    def force_promote_for_test(self, fact_id: int) -> None:
        """Test helper: promote without consolidator predicate."""
        g = make_gate()
        self.promote_fact(fact_id, g)

    def _check_admin(self, op: str) -> None:
        if self._role == "knowledge_agent":
            raise PromotionDenied(
                f"Operation '{op}' requires admin role, but store is connected as {self._role}"
            )

    def _to_fact(self, row: _FactRow) -> Fact:
        return Fact(
            id=row.id,
            statement=row.statement,
            discipline=row.discipline,
            scope=row.scope,
            episode_id=row.episode_id,
            provenance=row.provenance,  # type: ignore[arg-type]
            trust=row.trust,
            confidence=row.confidence,
            status=row.status,  # type: ignore[arg-type]
            valid_at=row.valid_at,
            recorded_at=row.recorded_at,
            invalid_at=row.invalid_at,
            superseded_by=row.superseded_by,
            importance=row.importance,
            access_count=row.access_count,
            last_accessed=row.last_accessed,
            helped_count=row.helped_count,
            capability_scope=row.capability_scope,
            company_id=row.company_id,
            domains=list(row.domains),
            capability_kind=row.capability_kind,
            capability_provenance=row.capability_provenance,
            last_verified=row.last_verified.date() if row.last_verified else None,
            promoted_from=row.promoted_from,
        )
