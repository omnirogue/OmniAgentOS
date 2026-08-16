"""Pgvector persistence coverage using the knowledge suite's owned test database."""

from __future__ import annotations

import time
from importlib import import_module

import psycopg
import pytest

from omniagentos.knowledge.contracts import EMBED_DIM, ENV_DSN
from omniagentos.knowledge.embeddings import FakeEmbedding
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.semsearch.search import SemHit
from omniagentos.semsearch.store import EmbeddedDocument, SemsearchStore, content_hash

search_module = import_module("omniagentos.semsearch.search")


def _unit_vector(axis: int) -> list[float]:
    vector = [0.0] * EMBED_DIM
    vector[axis] = 1.0
    return vector


def test_pg_upsert_is_idempotent_and_production_ranking_works(
    knowledge_store: KnowledgeStore,
) -> None:
    embedder = FakeEmbedding()
    prefix = "semsearch-pg-test-"
    model_id = prefix + "model"
    farther_text = "Prepare vegetables"
    nearer_text = "Ship a release to production"
    tool_text = "chop_food: Prepare vegetables"
    rows = [
        # Insert the worse same-kind match first so physical order cannot satisfy
        # the ranking assertion accidentally.
        EmbeddedDocument(
            "skill",
            prefix + "skill-farther",
            farther_text,
            content_hash(farther_text),
            _unit_vector(1),
            model_id,
        ),
        EmbeddedDocument(
            "skill",
            prefix + "skill-nearer",
            nearer_text,
            content_hash(nearer_text),
            _unit_vector(0),
            model_id,
        ),
        EmbeddedDocument(
            "tool",
            prefix + "tool",
            tool_text,
            content_hash(tool_text),
            embedder.embed([tool_text])[0],
            model_id,
        ),
    ]
    store = SemsearchStore(knowledge_store._dsn)
    try:
        with store._conn().cursor() as cursor:
            cursor.execute("DELETE FROM semsearch_embeddings WHERE ref_id LIKE %s", (prefix + "%",))
        assert store.upsert_many(rows) == 3
        assert store.upsert_many(rows) == 0

        hits = store.query(
            _unit_vector(0),
            model_id=model_id,
            kind="skill",
            limit=10,
        )
        owned_hits = [hit for hit in hits if hit.ref_id.startswith(prefix)]
        assert [hit.ref_id for hit in owned_hits] == [
            prefix + "skill-nearer",
            prefix + "skill-farther",
        ]
        assert [hit.score for hit in owned_hits] == pytest.approx([1.0, 0.0], abs=0.001)
        assert all(hit.kind == "skill" for hit in hits)
    finally:
        with store._conn().cursor() as cursor:
            cursor.execute("DELETE FROM semsearch_embeddings WHERE ref_id LIKE %s", (prefix + "%",))
        store.close()


def test_pg_lock_timeout_completes_with_lexical_fallback(
    knowledge_store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ControlledEmbedding:
        dim = EMBED_DIM
        model_id = "lock-timeout-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            assert texts == ["release"]
            return [_unit_vector(0)]

    monkeypatch.setenv(ENV_DSN, knowledge_store._dsn)
    monkeypatch.setattr(search_module, "build_embedder", ControlledEmbedding)
    monkeypatch.setattr(
        "omniagentos.skills.search",
        lambda query, limit: [{"id": "release", "title": "Release", "score": 0.9}],
    )

    with psycopg.connect(knowledge_store._dsn) as blocker:
        with blocker.cursor() as cursor:
            cursor.execute("LOCK TABLE semsearch_embeddings IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        hits = search_module.search("release", kind="skill", limit=1)
        elapsed = time.monotonic() - started

    assert elapsed < 7.0
    assert hits == [SemHit("skill", "release", "Release", 0.9, "lexical-fallback")]
