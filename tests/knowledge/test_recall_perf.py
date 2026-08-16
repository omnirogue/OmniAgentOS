"""Performance evidence for one-round-trip hybrid candidate retrieval."""

from __future__ import annotations

import math
import time
from collections.abc import Generator

import psycopg
import pytest
from pgvector import Vector

from omniagentos.knowledge.config import test_dsn as _test_dsn
from omniagentos.knowledge.migrate import migrate
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder
from tests.knowledge.db_ownership import parse_dbname_from_dsn, recreate_owned_test_db


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


@pytest.fixture(scope="session", autouse=True)
def setup_test_db() -> Generator[None, None, None]:
    """Make the exact perf command run-or-fail, never green-by-parent-fixture skip.

    This module-local fixture deliberately shadows the frozen parent autouse fixture,
    whose opt-in skip policy is suitable for the default suite but would make the brief's
    exact ``pytest ... -m perf`` command skip unconditionally.

    Shadowing the parent fixture must not also shadow the H-31 ownership guard: the
    drop/create goes through the shared ``recreate_owned_test_db`` helper, so this
    module can only ever destroy the database this pytest process itself claimed.
    """
    database = parse_dbname_from_dsn(_test_dsn())
    with psycopg.connect("postgresql://localhost/postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            recreate_owned_test_db(cur, database)
    migrate(_test_dsn())
    yield


@pytest.mark.perf
def test_recall_10k_p95(
    knowledge_store_admin: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed exactly 10k active facts, excluding setup and embedding from retrieval p95."""
    embedder = make_fake_embedder()
    episode_id = knowledge_store_admin.add_episode(
        source="run",
        content="10k synthetic recall benchmark corpus",
    )
    common_embedding = Vector(embedder.embed(["synthetic benchmark fact"])[0])
    rows = [
        (
            f"Synthetic benchmark fact {index}: component-{index % 100} release procedure",
            episode_id,
            "perf",
            common_embedding,
        )
        for index in range(10_000)
    ]
    conn = knowledge_store_admin._conn()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO facts "
            "(statement, episode_id, discipline, scope, provenance, trust, confidence, "
            "status, importance, embedding) "
            "VALUES (%s, %s, %s, 'global', 'extracted', 0.8, 0.8, 'active', 0.7, %s)",
            rows,
        )
    conn.commit()
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_RECALL_MODE", "lean")
    prompt = "component-42 release procedure"

    embedding_ms: list[float] = []
    retrieval_ms: list[float] = []
    totals_ms: list[float] = []
    for _ in range(20):
        total_start = time.perf_counter()
        embed_start = time.perf_counter()
        embedding = embedder.embed([prompt])[0]
        embedding_ms.append((time.perf_counter() - embed_start) * 1000.0)
        retrieve_start = time.perf_counter()
        candidates = knowledge_store_admin.recall_candidates(
            embedding=embedding,
            query_text=prompt,
            discipline=None,
            mode="lean",
        )
        retrieval_ms.append((time.perf_counter() - retrieve_start) * 1000.0)
        totals_ms.append((time.perf_counter() - total_start) * 1000.0)
        assert candidates

    retrieval_p95 = _p95(retrieval_ms)
    embedding_p95 = _p95(embedding_ms)
    total_p95 = _p95(totals_ms)
    print(
        f"recall perf (10k): retrieval_p95={retrieval_p95:.2f}ms "
        f"embedding_p95={embedding_p95:.2f}ms total_p95={total_p95:.2f}ms"
    )
    assert retrieval_p95 <= 250.0
    assert total_p95 <= 800.0
