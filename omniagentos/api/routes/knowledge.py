"""Dashboard-facing HTTP surface for the PostgreSQL knowledge subsystem."""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.deps import (
    KnowledgeAdminStoreDep,
    KnowledgeStoreDep,
    StoreDep,
)
from omniagentos.api.routes.access import get_capability_store
from omniagentos.api.routes.control import ApiError
from omniagentos.knowledge.contracts import (
    SOURCE_TRUST,
    EpisodeSource,
    Fact,
    FactStatus,
    KnowledgeUnavailable,
)
from omniagentos.knowledge.evidence import OPERATOR_SOURCE_IDENTITY
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.metrics import knowledge_stats
from omniagentos.knowledge.promotion import gate
from omniagentos.knowledge.recall import recall, render_recall_block
from omniagentos.knowledge.store import KnowledgeStore

router = APIRouter(prefix="/api/knowledge")

_OPERATOR_TOKEN_ENV = "OMNIAGENTOS_OPERATOR_TOKEN"
_AGENT_SOURCES = {EpisodeSource.WEB, EpisodeSource.CHAT}
_OPERATOR_SOURCES = {EpisodeSource.VAULT, EpisodeSource.HUMAN, EpisodeSource.CURATOR}


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class IngestRequest(KnowledgeModel):
    source: EpisodeSource
    content: str = Field(min_length=1)
    source_ref: str | None = None
    agent_id: str | None = None
    discipline: str | None = None


class RecallPreviewRequest(KnowledgeModel):
    prompt: str
    discipline: str | None = None
    agent_id: str | None = None


def fail(status: int, code: str, message: str, detail: Any = None) -> NoReturn:
    raise ApiError(status, code, message, detail)


def _model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _emit(audit_store: Any, event_type: str, action: str, payload: dict[str, Any]) -> None:
    """Publish through the existing durable event/SSE feed, best effort."""
    try:
        audit_store.insert_event(
            event_type,
            "api",
            action,
            target_type="knowledge",
            target_id=str(payload.get("fact_id", payload.get("episode_id", ""))),
            payload=payload,
        )
    except Exception:
        # A local event-feed issue must never make a completed knowledge mutation lie.
        return


def _require_operator(token: str | None) -> None:
    expected = os.environ.get(_OPERATOR_TOKEN_ENV)
    if not expected or not token or not hmac.compare_digest(token, expected):
        fail(403, "forbidden", "a valid operator token is required")


def _require_agent_write(agent_id: str | None) -> str:
    if not agent_id:
        fail(403, "forbidden", "an operator token or an agent write grant is required")
    if "knowledge.write" not in get_capability_store().get_grant(agent_id):
        fail(403, "forbidden", "agent does not hold knowledge.write", {"agent_id": agent_id})
    return agent_id


def _as_search_result(item: Any, fallback_rank: int = 1) -> dict[str, Any]:
    """Normalize the store's efficient raw candidate rows to the p6 result shape."""
    if hasattr(item, "fact"):
        fact = item.fact
        score = float(getattr(item, "score", 0.0))
        raw_signals = getattr(item, "signals", {})
        signals = list(raw_signals) if isinstance(raw_signals, dict) else list(raw_signals)
    else:
        row = dict(item)
        fact = Fact.model_validate(row)
        ranks = [float(row[key]) for key in ("vector_rank", "fts_rank") if row.get(key)]
        graph = float(row.get("graph_activation") or 0.0)
        # The store intentionally returns rank components rather than a presentation
        # score.  A bounded monotonic score is sufficient for the dashboard search.
        score = max([1.0 / rank for rank in ranks] + [min(graph, 1.0), 1.0 / fallback_rank])
        signals = [key for key in ("vector", "fts") if row.get(f"{key}_rank")]
        if graph > 0:
            signals.append("graph")
    return {"fact": _model(fact), "score": max(0.0, min(1.0, score)), "signals": signals}


def _agent_edges_for_fact(store: KnowledgeStore, fact_id: int) -> list[dict[str, Any]]:
    """Fetch fact edges without exposing inactive fact endpoints to the agent role."""
    # KnowledgeStore deliberately keeps the generic graph primitive compact.  This
    # endpoint needs one additional status join so a relation cannot reveal a
    # quarantined/superseded fact ID through an otherwise visible fact.
    with store._lock:
        with store._conn().cursor() as cursor:
            cursor.execute(
                """
                SELECT e.id, e.src_kind, e.src_id, e.dst_kind, e.dst_id, e.edge_type,
                       e.weight, e.valid_at, e.invalid_at
                FROM edges e
                LEFT JOIN facts src ON e.src_kind = 'fact' AND src.id = e.src_id
                LEFT JOIN facts dst ON e.dst_kind = 'fact' AND dst.id = e.dst_id
                WHERE ((e.src_kind = 'fact' AND e.src_id = %s)
                    OR (e.dst_kind = 'fact' AND e.dst_id = %s))
                  AND e.invalid_at IS NULL
                  AND (src.id IS NULL OR (src.status = 'active' AND src.capability_scope IS NULL))
                  AND (dst.id IS NULL OR (dst.status = 'active' AND dst.capability_scope IS NULL))
                """,
                (fact_id, fact_id),
            )
            return [
                {
                    "id": row[0],
                    "source": f"{row[1]}:{row[2]}",
                    "target": f"{row[3]}:{row[4]}",
                    "type": row[5],
                    "weight": float(row[6]),
                }
                for row in cursor.fetchall()
            ]


@router.get("/stats")
async def get_stats(store: KnowledgeStoreDep, audit_store: StoreDep) -> dict[str, Any]:
    return knowledge_stats(store, audit_store)


@router.get("/graph")
async def get_graph(
    store: KnowledgeStoreDep, limit: Annotated[int, Query(ge=1, le=500)] = 500
) -> dict[str, Any]:
    try:
        snapshot = store.graph_snapshot(limit_nodes=limit)
    except KnowledgeUnavailable:
        return {"nodes": [], "edges": []}
    fact_nodes = [
        fact
        for fact in snapshot["facts"]
        if fact["status"] == FactStatus.ACTIVE.value and fact.get("capability_scope") is None
    ]
    nodes: list[dict[str, Any]] = [
        {
            "id": f"fact:{fact['id']}",
            "kind": "fact",
            "label": fact["statement"],
            "status": fact["status"],
            "importance": float(fact["importance"]),
            "trust": float(fact["trust"]),
            "discipline": fact["discipline"],
        }
        for fact in fact_nodes[:limit]
    ]
    remaining = limit - len(nodes)
    nodes.extend(
        {
            "id": f"entity:{entity['id']}",
            "kind": "entity",
            "label": entity["name"],
            "entityKind": entity["kind"],
        }
        for entity in snapshot["entities"][:remaining]
    )
    visible = {node["id"] for node in nodes}
    edges = [
        {
            "id": edge["id"],
            "source": f"{edge['src_kind']}:{edge['src_id']}",
            "target": f"{edge['dst_kind']}:{edge['dst_id']}",
            "type": edge["type"],
            "weight": float(edge["weight"]),
        }
        for edge in snapshot["edges"]
        if f"{edge['src_kind']}:{edge['src_id']}" in visible
        and f"{edge['dst_kind']}:{edge['dst_id']}" in visible
    ]
    return {"nodes": nodes, "edges": edges}


@router.get("/facts/{fact_id}")
async def get_fact(fact_id: int, store: KnowledgeStoreDep) -> dict[str, Any]:
    fact = store.get_fact(fact_id)
    if fact is None or fact.status != FactStatus.ACTIVE or fact.capability_scope is not None:
        fail(404, "not_found", "knowledge fact not found", {"id": fact_id})
    episode = store.get_episode(fact.episode_id)
    replacement = store.get_fact(fact.superseded_by) if fact.superseded_by else None
    # A replacement can only be reached through a non-active source fact, but retain
    # the check as a future-proof no-leak guard.
    if replacement is not None and (
        replacement.status != FactStatus.ACTIVE or replacement.capability_scope is not None
    ):
        replacement = None
    store.bump_access([fact.id])
    return {
        "fact": _model(fact),
        "episode": _model(episode) if episode is not None else None,
        "edges": _agent_edges_for_fact(store, fact.id),
        "superseded_by": _model(replacement) if replacement is not None else None,
    }


@router.get("/search")
async def search(
    store: KnowledgeStoreDep,
    q: str = "",
    k: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    try:
        candidates = store.recall_candidates(
            embedding=None,
            query_text=q,
            include_quarantined=False,
            k=k,
        )
    except KnowledgeUnavailable:
        return {"results": []}
    results = [
        _as_search_result(candidate, index + 1) for index, candidate in enumerate(candidates[:k])
    ]
    # Defense in depth: the SQL already filters, but normalizing a mocked/store
    # adapter response must never accidentally reintroduce quarantined facts.
    return {
        "results": [
            item
            for item in results
            if item["fact"]["status"] == "active" and item["fact"].get("capability_scope") is None
        ]
    }


@router.post("/recall-preview")
async def recall_preview(body: RecallPreviewRequest, store: KnowledgeStoreDep) -> dict[str, Any]:
    try:
        result = recall(
            store,
            prompt=body.prompt,
            discipline=body.discipline,
            agent_id=body.agent_id,
            include_quarantined=False,
            run_id=None,
        )
    except KnowledgeUnavailable:
        return {"facts": [], "rendered": "", "tokens": 0, "latency_ms": 0.0}
    rendered = render_recall_block(result)
    return {
        "facts": [_model(item) for item in result.facts],
        "rendered": rendered,
        "tokens": result.rendered_tokens,
        "latency_ms": result.latency_ms,
    }


@router.get("/recalls")
async def get_recalls(run_id: str, store: KnowledgeStoreDep) -> list[dict[str, Any]]:
    try:
        with store._lock:
            with store._conn().cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, run_id, agent_id, discipline, query_digest, fact_ids,
                           tokens, latency_ms, created_at
                    FROM recall_log WHERE run_id = %s ORDER BY created_at DESC, id DESC
                    """,
                    (run_id,),
                )
                rows = cursor.fetchall()
                active_fact_ids: set[int] = set()
                recalled_ids = sorted({fact_id for row in rows for fact_id in (row[5] or [])})
                if recalled_ids:
                    cursor.execute(
                        "SELECT id FROM facts WHERE id = ANY(%s) AND status = 'active' "
                        "AND capability_scope IS NULL",
                        (recalled_ids,),
                    )
                    active_fact_ids = {fact_row[0] for fact_row in cursor.fetchall()}
                else:
                    active_fact_ids = set()
    except KnowledgeUnavailable:
        return []
    return [
        {
            "id": row[0],
            "run_id": row[1] or "",
            "agent_id": row[2] or "",
            "discipline": row[3] or "",
            "query_digest": str(row[4])[:16],
            "fact_ids": [fact_id for fact_id in row[5] or [] if fact_id in active_fact_ids],
            "tokens": row[6],
            "latency_ms": float(row[7]),
            "created_at": row[8].isoformat() if row[8] is not None else None,
        }
        for row in rows
    ]


@router.post("/ingest")
async def ingest(
    body: IngestRequest,
    audit_store: StoreDep,
    store: KnowledgeStoreDep,
    admin_store: KnowledgeAdminStoreDep,
    x_operator_token: Annotated[str | None, Header()] = None,
    x_agent_id: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    # Token presence takes precedence.  In particular, an invalid operator header
    # must never fall through to the agent store, even if an agent header is present.
    if x_operator_token is not None:
        _require_operator(x_operator_token)
        if body.source not in _OPERATOR_SOURCES:
            fail(403, "forbidden", "operator ingest only permits vault, human, or curator sources")
        result = ingest_episode(
            admin_store,
            source=body.source.value,
            content=body.content,
            source_ref=body.source_ref,
            agent_id=body.agent_id,
            discipline=body.discipline,
        )
    else:
        agent_id = _require_agent_write(x_agent_id)
        if body.source not in _AGENT_SOURCES:
            fail(403, "forbidden", "agent ingest only permits web or chat sources")
        result = ingest_episode(
            store,
            source=body.source.value,
            content=body.content,
            source_ref=body.source_ref,
            agent_id=agent_id,
            discipline=body.discipline,
            trust=min(0.6, SOURCE_TRUST[body.source.value]),
        )
    _emit(
        audit_store,
        "knowledge_ingested",
        "knowledge.ingested",
        {
            "episode_id": result.episode_id,
            "fact_count": len(result.fact_ids),
        },
    )
    return {
        "episode_id": result.episode_id,
        "fact_ids": result.fact_ids,
        "entity_ids": result.entity_ids,
        "edge_ids": result.edge_ids,
    }


@router.post("/facts/{fact_id}/promote")
async def promote(
    fact_id: int,
    audit_store: StoreDep,
    admin_store: KnowledgeAdminStoreDep,
    x_operator_token: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    _require_operator(x_operator_token)
    # Re-embed from the statement before promoting, FAIL-CLOSED: the fact may be
    # agent-authored with an agent-crafted embedding; overwrite it with the true statement
    # vector so no agent-chosen embedding reaches active recall. If the embedder is
    # unavailable, refuse (503) rather than promote an unverified vector.
    pf = admin_store.get_fact(fact_id)
    if pf is None:
        fail(404, "not_found", f"fact {fact_id} not found")
    if admin_store._embedder is None:
        fail(
            503,
            "embedder_unavailable",
            "cannot re-embed at promotion; retry when embeddings are available",
        )
    try:
        vec = admin_store._embedder.embed([pf.statement])[0]
        admin_store.set_embedding(fact_id, vec)
    except Exception:
        fail(503, "embedder_unavailable", "re-embed failed; retry when embeddings are available")
    # H-10: record the system-authored evidence BEFORE the state change. An operator
    # holding the operator token IS a system verifier, and this is the durable record of
    # that verdict — without it the next consolidation run has no evidence that this fact
    # was ever reviewed. Ordering is deliberate: a crash between the two leaves evidence
    # without promotion (safe), never promotion without evidence.
    admin_store.record_promotion_evidence(
        fact_id=fact_id,
        provenance="system_operator",
        source_identity=OPERATOR_SOURCE_IDENTITY,
        verifier_outcome="pass",
    )
    admin_store.promote_fact(fact_id, gate())
    _emit(audit_store, "knowledge_promoted", "knowledge.promoted", {"fact_id": fact_id})
    return {"fact_id": fact_id}


@router.post("/facts/{fact_id}/demote")
async def demote(
    fact_id: int,
    audit_store: StoreDep,
    admin_store: KnowledgeAdminStoreDep,
    x_operator_token: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    _require_operator(x_operator_token)
    # Withdraw the operator's own verdict first. Leaving it active would let the next
    # consolidation run re-promote the fact on the single-source branch using evidence the
    # operator has just contradicted.
    admin_store.revoke_promotion_evidence_for_source(
        fact_id, OPERATOR_SOURCE_IDENTITY, reason="operator demoted the fact"
    )
    admin_store.demote_fact(fact_id, gate())
    _emit(audit_store, "knowledge_demoted", "knowledge.demoted", {"fact_id": fact_id})
    return {"fact_id": fact_id}


__all__ = ["router"]
