"""Catalog indexing + semantic (cosine) + hybrid (RRF) search.

Hermetic: the embedding backend is replaced with a tiny deterministic vocabulary
embedder, and the catalog uses a tmp DB + tmp document root — no Ollama, no real files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.filesearch import catalog, embeddings, semantic
from omniagentos.filesearch.service import SearchHit

# 4-dim toy embedding: finance / animals / code / travel. Similar topics → similar vectors.
_AXES = (
    ("money", "invoice", "payment", "finance", "billing", "owes"),
    ("cat", "dog", "animal", "pet", "puppy", "kitten"),
    ("code", "python", "function", "class", "import"),
    ("travel", "flight", "hotel", "trip", "vacation"),
)


def _fake_embed(texts: list[str], model: str = "x", timeout: float = 0.0) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        low = text.lower()
        vector = [1.0 if any(w in low for w in axis) else 0.0 for axis in _AXES]
        if not any(vector):
            vector = [0.01, 0.01, 0.01, 0.01]
        vectors.append(vector)
    return vectors


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    monkeypatch.setattr(embeddings, "embed", _fake_embed)
    monkeypatch.setattr(embeddings, "is_available", lambda model=embeddings.DEFAULT_MODEL: True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "invoice_notes.md").write_text("the client owes a payment; send the invoice today")
    (docs / "pets.md").write_text("my cat and my dog are the cutest animals ever")
    (docs / "trip.md").write_text("booking a flight and a hotel for our travel next month")
    monkeypatch.setattr(catalog, "curated_roots", lambda: [(str(docs), "local")])
    return docs


def test_reindex_indexes_embeds_and_is_incremental(hermetic: Path) -> None:
    first = catalog.reindex()
    assert first["indexed"] == 3
    assert first["embedded"] == 3
    assert first["total"] == 3
    # No changes → nothing re-indexed.
    assert catalog.reindex()["indexed"] == 0
    # Delete a file → it is pruned on the next pass.
    (hermetic / "trip.md").unlink()
    pruned = catalog.reindex()
    assert pruned["deleted"] == 1
    assert pruned["total"] == 2


def test_semantic_search_finds_by_meaning(hermetic: Path) -> None:
    catalog.reindex()
    # "who owes us money" has NO literal token from the invoice file except via meaning.
    hits = semantic.semantic_search("who owes us money for services")
    assert hits, "expected semantic hits"
    assert hits[0].name == "invoice_notes.md"
    # An animals query ranks the pets file first.
    assert semantic.semantic_search("photos of a kitten")[0].name == "pets.md"


def test_semantic_degrades_without_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    monkeypatch.setattr(embeddings, "embed", lambda texts, model="x", timeout=0.0: None)
    # No embeddings → no results, no crash (hybrid then falls back to keyword upstream).
    assert semantic.semantic_search("anything") == []


def test_hybrid_fuses_keyword_and_semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    def _hit(path: str) -> SearchHit:
        return SearchHit(path, path.strip("/"), "local", "md", 1, "d", "", 0.0, 0.0)

    monkeypatch.setattr(
        semantic, "search_files", lambda q, scopes=None, limit=20: [_hit("/a"), _hit("/b")]
    )
    monkeypatch.setattr(
        semantic, "semantic_search", lambda q, limit=20, model="x": [_hit("/b"), _hit("/c")]
    )

    hits = semantic.hybrid_search("x", limit=5)
    # /b is ranked by BOTH backends → RRF puts it first; union of all three appears.
    assert hits[0].path == "/b"
    assert {h.path for h in hits} == {"/a", "/b", "/c"}
