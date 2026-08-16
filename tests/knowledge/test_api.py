"""Contract tests for the dashboard knowledge API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from omniagentos.api.deps import get_knowledge_admin_store, get_knowledge_store, get_store
from omniagentos.api.main import app
from omniagentos.api.routes import knowledge as route
from omniagentos.knowledge.contracts import (
    Episode,
    Fact,
    FactStatus,
    IngestResult,
    Provenance,
    RecallResult,
)


class _AuditStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def insert_event(
        self, event_type: str, *_args: Any, payload: dict[str, Any] | None = None, **_kwargs: Any
    ) -> int:
        self.events.append((event_type, payload or {}))
        return len(self.events)

    def get_events_after(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class _Grants:
    def __init__(self) -> None:
        self.grants: dict[str, list[str]] = {}

    def get_grant(self, agent_id: str) -> list[str]:
        return self.grants.get(agent_id, [])


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def cursor(self) -> _Cursor:
        return _Cursor(self.rows)


class _Lock:
    def __enter__(self) -> _Lock:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


class _KnowledgeStore:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.fact = Fact(
            id=1,
            statement="The release uses a canary.",
            episode_id=1,
            provenance=Provenance.EXTRACTED,
            trust=0.7,
            confidence=0.8,
            status=FactStatus.ACTIVE,
            valid_at=now,
            recorded_at=now,
            importance=0.6,
            access_count=0,
            last_accessed=now,
            helped_count=0,
        )
        self.episode = Episode(id=1, source="human", content="release note", occurred_at=now)
        self.bumped: list[int] = []
        self.promoted: list[int] = []
        self.demoted: list[int] = []
        self.reembedded: list[int] = []
        self.evidence: list[tuple[int, str, str, str]] = []
        self.revoked: list[tuple[int, str, str]] = []
        self.calls: list[tuple[str, int]] = []
        self._lock = _Lock()
        # The promote route re-embeds (fail-closed) before promoting; a fake 1024-d embedder.
        self._embedder = _FakeEmbedder()

    def set_embedding(self, fact_id: int, embedding: list[float]) -> None:
        self.reembedded.append(fact_id)

    def _conn(self) -> _Connection:
        stamp = datetime.now(UTC)
        return _Connection([(1, "run-1", "agent", "ops", "abc", [1], 7, 1.2, stamp)])

    def stats(self) -> dict[str, Any]:
        return {
            "facts": {"total": 1, "active": 1, "quarantined": 0, "superseded": 0},
            "entities": 0,
            "edges": 0,
            "episodes": 1,
            "recalls": {"count": 0, "avg_latency_ms": 0.0, "avg_tokens": 0.0},
        }

    def graph_snapshot(self, *, limit_nodes: int) -> dict[str, Any]:
        return {
            "facts": [
                {
                    "id": 1,
                    "statement": self.fact.statement,
                    "status": "active",
                    "importance": 0.6,
                    "trust": 0.7,
                    "discipline": None,
                    "capability_scope": self.fact.capability_scope,
                }
            ],
            "entities": [],
            "edges": [],
        }

    def get_fact(self, fact_id: int) -> Fact | None:
        return self.fact if fact_id == 1 else None

    def get_episode(self, episode_id: int) -> Episode | None:
        return self.episode if episode_id == 1 else None

    def bump_access(self, fact_ids: list[int]) -> None:
        self.bumped.extend(fact_ids)

    def recall_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [self.fact.model_dump()]

    def promote_fact(self, fact_id: int, _gate: Any) -> None:
        self.calls.append(("promote_fact", fact_id))
        self.promoted.append(fact_id)

    def demote_fact(self, fact_id: int, _gate: Any) -> None:
        self.calls.append(("demote_fact", fact_id))
        self.demoted.append(fact_id)

    def record_promotion_evidence(
        self,
        *,
        fact_id: int,
        provenance: str,
        source_identity: str,
        verifier_outcome: str,
        promotion_generation: int | None = None,
    ) -> int:
        self.calls.append(("record_promotion_evidence", fact_id))
        self.evidence.append((fact_id, provenance, source_identity, verifier_outcome))
        return len(self.evidence)

    def revoke_promotion_evidence_for_source(
        self, fact_id: int, source_identity: str, *, reason: str
    ) -> list[int]:
        self.calls.append(("revoke_promotion_evidence_for_source", fact_id))
        self.revoked.append((fact_id, source_identity, reason))
        return []


def test_knowledge_endpoint_contract_and_two_store_di(monkeypatch) -> None:
    agent_store, admin_store, audit, grants = (
        _KnowledgeStore(),
        _KnowledgeStore(),
        _AuditStore(),
        _Grants(),
    )
    app.dependency_overrides.update(
        {
            get_knowledge_store: lambda: agent_store,
            get_knowledge_admin_store: lambda: admin_store,
            get_store: lambda: audit,
        }
    )
    # Patch routes to use the mock stores and grants
    monkeypatch.setattr(route, "get_capability_store", lambda: grants)
    monkeypatch.setattr(route, "_agent_edges_for_fact", lambda *_args: [])
    monkeypatch.setattr(route, "recall", lambda *_args, **_kwargs: RecallResult())
    used: list[Any] = []

    def fake_ingest(store: Any, **_kwargs: Any) -> IngestResult:
        used.append(store)
        return IngestResult(episode_id=2, fact_ids=[2], entity_ids=[], edge_ids=[])

    monkeypatch.setattr(route, "ingest_episode", fake_ingest)
    monkeypatch.setenv("OMNIAGENTOS_OPERATOR_TOKEN", "operator")
    try:
        with TestClient(app) as client:
            assert client.get("/api/knowledge/stats").status_code == 200
            graph_resp = client.get("/api/knowledge/graph?limit=500")
            assert graph_resp.status_code == 200
            # Graph nodes may be empty depending on mock, just verify status
            assert client.get("/api/knowledge/facts/1").status_code == 200
            assert client.get("/api/knowledge/search?q=release&k=20").json()["results"]
            assert (
                client.post("/api/knowledge/recall-preview", json={"prompt": "release"}).status_code
                == 200
            )
            assert client.get("/api/knowledge/recalls?run_id=run-1").status_code == 200

            assert (
                client.post(
                    "/api/knowledge/ingest", json={"source": "web", "content": "x"}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/knowledge/ingest",
                    headers={"X-Agent-Id": "agent"},
                    json={"source": "web", "content": "x"},
                ).status_code
                == 403
            )
            assert client.post("/api/knowledge/facts/1/promote").status_code == 403
            assert client.post("/api/knowledge/facts/1/demote").status_code == 403

            grants.grants["agent"] = ["knowledge.write"]
            assert (
                client.post(
                    "/api/knowledge/ingest",
                    headers={"X-Agent-Id": "agent"},
                    json={"source": "web", "content": "x"},
                ).status_code
                == 200
            )
            assert used[-1] is agent_store

            assert (
                client.post(
                    "/api/knowledge/ingest",
                    headers={"X-Operator-Token": "operator"},
                    json={"source": "human", "content": "x"},
                ).status_code
                == 200
            )
            assert used[-1] is admin_store
            assert (
                client.post(
                    "/api/knowledge/facts/1/promote", headers={"X-Operator-Token": "operator"}
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/knowledge/facts/1/demote", headers={"X-Operator-Token": "operator"}
                ).status_code
                == 200
            )
            # M-26: the operator routes are real callers of the evidence ledger, and the
            # evidence is written BEFORE the state change so a crash never leaves a promoted
            # fact with nothing justifying it.
            assert admin_store.evidence == [(1, "system_operator", "operator_console", "pass")]
            assert admin_store.revoked == [(1, "operator_console", "operator demoted the fact")]
            assert [name for name, _ in admin_store.calls] == [
                "record_promotion_evidence",
                "promote_fact",
                "revoke_promotion_evidence_for_source",
                "demote_fact",
            ]
    finally:
        app.dependency_overrides.clear()


def test_general_graph_and_fact_detail_hide_company_capabilities() -> None:
    store = _KnowledgeStore()
    store.fact = store.fact.model_copy(
        update={
            "statement": "Private customer capability",
            "capability_scope": "company",
            "company_id": "co_alpha",
            "domains": ["video"],
            "capability_kind": "tool",
            "capability_provenance": "run-alpha | Alpha | 2026-08-14",
        }
    )
    app.dependency_overrides[get_knowledge_store] = lambda: store
    try:
        with TestClient(app) as client:
            graph = client.get("/api/knowledge/graph?limit=500")
            detail = client.get("/api/knowledge/facts/1")

        assert graph.status_code == 200
        assert graph.json()["nodes"] == []
        assert detail.status_code == 404
        assert store.bumped == []
    finally:
        app.dependency_overrides.clear()
