"""Hermetic indexing/search tests with controlled fake vectors."""

from __future__ import annotations

import math
from collections.abc import Iterable
from importlib import import_module

import pytest

from omniagentos.knowledge.contracts import EmbeddingUnavailable
from omniagentos.knowledge.embeddings import FakeEmbedding
from omniagentos.semsearch import index as sem_index
from omniagentos.semsearch import search as public_search
from omniagentos.semsearch import search as search_function
from omniagentos.semsearch import search as unified_search
from omniagentos.semsearch.constants import MAX_QUERY_LENGTH
from omniagentos.semsearch.index import CorpusDocument, reindex
from omniagentos.semsearch.search import SemHit
from omniagentos.semsearch.store import EmbeddedDocument, SemKind, StoredHit, content_hash

search_module = import_module("omniagentos.semsearch.search")


class ControlledFakeEmbedding(FakeEmbedding):
    """Fake provider whose tiny vectors encode the test's declared concepts."""

    def __init__(self) -> None:
        super().__init__(dim=2)
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.casefold()
            if any(word in lowered for word in ("deploy", "ship", "release", "prod")):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


class MemoryStore:
    """The SemsearchStore contract in memory, including cosine ranking."""

    def __init__(self) -> None:
        self.rows: dict[tuple[SemKind, str], EmbeddedDocument] = {}
        self.upsert_batches: list[int] = []
        self.closed = False

    def signatures(self, kind: SemKind) -> dict[str, tuple[str, str]]:
        return {
            ref_id: (row.content_hash, row.model_id)
            for (row_kind, ref_id), row in self.rows.items()
            if row_kind == kind
        }

    def upsert_many(self, documents: Iterable[EmbeddedDocument]) -> int:
        documents = list(documents)
        self.upsert_batches.append(len(documents))
        changed = 0
        for document in documents:
            key = (document.kind, document.ref_id)
            old = self.rows.get(key)
            if old is None or (old.content_hash, old.model_id) != (
                document.content_hash,
                document.model_id,
            ):
                self.rows[key] = document
                changed += 1
        return changed

    def delete_refs(self, kind: SemKind, ref_ids: Iterable[str]) -> int:
        stale = [(kind, ref_id) for ref_id in ref_ids if (kind, ref_id) in self.rows]
        for key in stale:
            del self.rows[key]
        return len(stale)

    def query(
        self,
        embedding: list[float],
        *,
        model_id: str,
        kind: SemKind | None,
        limit: int,
    ) -> list[StoredHit]:
        def cosine(vector: list[float]) -> float:
            numerator = sum(left * right for left, right in zip(embedding, vector, strict=True))
            denominator = math.sqrt(sum(value * value for value in embedding)) * math.sqrt(
                sum(value * value for value in vector)
            )
            return numerator / denominator

        hits = [
            StoredHit(row.kind, row.ref_id, row.text, cosine(row.embedding))
            for row in self.rows.values()
            if row.model_id == model_id and (kind is None or row.kind == kind)
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.kind, hit.ref_id))
        return hits[:limit]

    def close(self) -> None:
        self.closed = True


def _loaders() -> dict[SemKind, sem_index.CorpusLoader]:
    return {
        "skill": lambda: [
            CorpusDocument(
                "skill",
                "cooking-skill",
                "Kitchen prep",
                "Kitchen prep chop vegetables and prepare dinner",
            ),
            CorpusDocument(
                "skill",
                "release-skill",
                "Production delivery",
                "Production delivery deploy and ship services to production",
            ),
        ],
        "tool": lambda: [
            CorpusDocument("tool", "deploy_tool", "Deploy Tool", "deploy_tool: ship a release")
        ],
        "capability": lambda: [
            CorpusDocument(
                "capability", "cloud.release", "Cloud Release", "cloud.release: release service"
            )
        ],
    }


def test_content_hash_is_stable_and_content_sensitive() -> None:
    assert content_hash("same") == content_hash("same")
    assert content_hash("same") != content_hash("changed")


def test_reindex_is_idempotent_and_batches_only_changed_documents() -> None:
    store = MemoryStore()
    embedder = ControlledFakeEmbedding()

    first = reindex(("skill",), store=store, embedder=embedder, loaders=_loaders())
    second = reindex(("skill",), store=store, embedder=embedder, loaders=_loaders())

    assert first["skill"] == sem_index.IndexStats(scanned=2, embedded=2, skipped=0)
    assert second["skill"] == sem_index.IndexStats(scanned=2, embedded=0, skipped=2)
    assert embedder.calls == [
        [
            "Kitchen prep chop vegetables and prepare dinner",
            "Production delivery deploy and ship services to production",
        ]
    ]


def test_reindex_embeds_and_upserts_more_than_one_chunk() -> None:
    store = MemoryStore()
    embedder = ControlledFakeEmbedding()
    document_count = sem_index.INDEX_BATCH_SIZE + 1
    loaders: dict[SemKind, sem_index.CorpusLoader] = {
        "skill": lambda: [
            CorpusDocument("skill", f"skill-{index:03d}", f"Skill {index}", f"text {index}")
            for index in reversed(range(document_count))
        ]
    }

    result = reindex(("skill",), store=store, embedder=embedder, loaders=loaders)

    assert result["skill"] == sem_index.IndexStats(
        scanned=document_count,
        embedded=document_count,
        skipped=0,
    )
    assert [len(call) for call in embedder.calls] == [sem_index.INDEX_BATCH_SIZE, 1]
    assert store.upsert_batches == [sem_index.INDEX_BATCH_SIZE, 1]
    assert embedder.calls[0][0] == "text 0"
    assert embedder.calls[-1] == [f"text {document_count - 1}"]


def test_reindex_rejects_wrong_vector_dimension_before_upsert() -> None:
    class WrongDimensionEmbedding(ControlledFakeEmbedding):
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _text in texts]

    store = MemoryStore()

    with pytest.raises(RuntimeError, match="returned dimension 1; expected 2"):
        reindex(
            ("skill",),
            store=store,
            embedder=WrongDimensionEmbedding(),
            loaders=_loaders(),
        )

    assert store.upsert_batches == []


def test_incomplete_skill_scan_does_not_delete_live_boundary_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sem_index, "SKILL_SCAN_LIMIT", 2)
    requested_limits: list[int] = []

    def fake_list_skills(*, include_archived: bool, limit: int) -> list[dict[str, str]]:
        assert include_archived is True
        requested_limits.append(limit)
        return [
            {
                "id": f"skill-{index}",
                "title": f"Skill {index}",
                "summary": f"summary {index}",
                "preferred_method": "method",
                "status": "active",
            }
            for index in range(3)
        ]

    monkeypatch.setattr(sem_index, "list_skills", fake_list_skills)
    store = MemoryStore()
    embedder = ControlledFakeEmbedding()
    for index in range(3):
        text = f"Skill {index} summary {index} method"
        store.rows[("skill", f"skill-{index}")] = EmbeddedDocument(
            "skill",
            f"skill-{index}",
            text,
            content_hash(text),
            [0.0, 1.0],
            embedder.model_id,
        )

    result = reindex(
        ("skill",),
        store=store,
        embedder=embedder,
        loaders={"skill": sem_index.skill_documents},
    )

    assert requested_limits == [3]
    assert result["skill"] == sem_index.IndexStats(scanned=2, embedded=0, skipped=2)
    assert ("skill", "skill-2") in store.rows


def test_semantic_ranking_query_embedding_and_kind_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    embedder = ControlledFakeEmbedding()
    reindex(("skill", "tool"), store=store, embedder=embedder, loaders=_loaders())
    embedder.calls.clear()

    monkeypatch.setattr(search_module, "build_embedder", lambda: embedder)
    monkeypatch.setattr(search_module, "SemsearchStore", lambda: store)
    monkeypatch.setattr(
        search_module,
        "titles_for",
        lambda kind: {document.ref_id: document.title for document in _loaders()[kind]()},
    )

    skill_hits = search_module.search("release to prod", kind="skill", limit=10)
    tool_hits = search_module.search("release to prod", kind="tool", limit=10)

    assert [hit.ref_id for hit in skill_hits] == ["release-skill", "cooking-skill"]
    assert all(hit.kind == "skill" and hit.source == "semantic" for hit in skill_hits)
    assert [hit.ref_id for hit in tool_hits] == ["deploy_tool"]
    assert embedder.calls == [["release to prod"], ["release to prod"]]


def test_embedding_failure_returns_lexical_hits_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenEmbedding(ControlledFakeEmbedding):
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingUnavailable("offline")

    monkeypatch.setattr(search_module, "build_embedder", BrokenEmbedding)
    monkeypatch.setattr(
        "omniagentos.skills.search",
        lambda query, limit: [
            {"id": "release-skill", "slug": "release-skill", "title": "Release", "score": 0.7}
        ],
    )

    hits = search_function("release to prod", kind="skill", limit=3)

    assert hits == [SemHit("skill", "release-skill", "Release", 0.7, "lexical-fallback")]


@pytest.mark.parametrize("stale_model", [False, True], ids=["empty-table", "current-model-miss"])
def test_empty_semantic_result_falls_back_lexically(
    monkeypatch: pytest.MonkeyPatch,
    stale_model: bool,
) -> None:
    store = MemoryStore()
    if stale_model:
        stale_text = "Release stale index row"
        store.rows[("skill", "stale-release")] = EmbeddedDocument(
            "skill",
            "stale-release",
            stale_text,
            content_hash(stale_text),
            [1.0, 0.0],
            "previous-model",
        )
    monkeypatch.setattr(search_module, "SemsearchStore", lambda: store)
    monkeypatch.setattr(search_module, "build_embedder", ControlledFakeEmbedding)
    monkeypatch.setattr(
        "omniagentos.skills.search",
        lambda query, limit: [{"id": "release-skill", "title": "Release", "score": 0.8}],
    )

    hits = search_function("release", kind="skill", limit=2)

    assert hits == [SemHit("skill", "release-skill", "Release", 0.8, "lexical-fallback")]


def test_unset_dsn_falls_back_without_probing_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_KNOWLEDGE_PG_DSN", raising=False)

    def forbidden_embedder() -> object:
        raise AssertionError("an unset DSN must bypass the embedder")

    monkeypatch.setattr(search_module, "build_embedder", forbidden_embedder)
    monkeypatch.setattr(
        "omniagentos.skills.search",
        lambda query, limit: [
            {"id": "release-skill", "slug": "release-skill", "title": "Release", "score": 0.7}
        ],
    )

    hits = search_function("release", kind="skill", limit=1)

    assert hits == [SemHit("skill", "release-skill", "Release", 0.7, "lexical-fallback")]


def test_empty_or_invalid_requests_are_safe() -> None:
    assert public_search("", kind="all", limit=10) == []
    assert unified_search("anything", kind="unknown", limit=10) == []
    assert unified_search(None, kind="skill", limit=10) == []  # type: ignore[arg-type]
    assert unified_search("anything", kind="skill", limit="not-an-int") == []  # type: ignore[arg-type]
    assert unified_search("x" * (MAX_QUERY_LENGTH + 1), kind="skill", limit=10) == []


def test_public_search_clamps_result_count(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_limits: list[int] = []

    class RecordingStore(MemoryStore):
        def query(
            self,
            embedding: list[float],
            *,
            model_id: str,
            kind: SemKind | None,
            limit: int,
        ) -> list[StoredHit]:
            seen_limits.append(limit)
            return [StoredHit("skill", "release", "Release", 1.0)]

    monkeypatch.setattr(search_module, "SemsearchStore", RecordingStore)
    monkeypatch.setattr(search_module, "build_embedder", ControlledFakeEmbedding)
    monkeypatch.setattr(search_module, "titles_for", lambda kind: {"release": "Release"})

    hits = search_function("release", kind="skill", limit=10_000)

    assert seen_limits == [search_module.MAX_RESULT_COUNT]
    assert hits == [SemHit("skill", "release", "Release", 1.0, "semantic")]
