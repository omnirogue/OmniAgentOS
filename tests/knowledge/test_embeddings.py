"""Test: embedding providers (FakeEmbedding, OllamaEmbedding)."""

from __future__ import annotations

import pytest

from omniagentos.knowledge.contracts import EMBED_DIM, EmbeddingUnavailable
from omniagentos.knowledge.embeddings import FakeEmbedding, OllamaEmbedding


def test_fake_embedding_dim() -> None:
    """FakeEmbedding reports correct dimension."""
    embedder = FakeEmbedding()
    assert embedder.dim == EMBED_DIM
    assert embedder.model_id == "fake"


def test_fake_embedding_deterministic() -> None:
    """FakeEmbedding generates deterministic vectors."""
    embedder = FakeEmbedding()
    text = "The quick brown fox"

    vec1 = embedder.embed([text])[0]
    vec2 = embedder.embed([text])[0]

    assert vec1 == vec2, "Same text must produce identical embeddings"
    assert len(vec1) == EMBED_DIM


def test_fake_embedding_unit_vectors() -> None:
    """FakeEmbedding produces unit vectors."""
    embedder = FakeEmbedding()
    text = "Test text"

    vec = embedder.embed([text])[0]

    # Check magnitude is approximately 1
    magnitude = sum(v * v for v in vec) ** 0.5
    assert 0.99 < magnitude < 1.01, f"Vector magnitude should be ~1, got {magnitude}"


def test_fake_embedding_batch() -> None:
    """FakeEmbedding handles batch embedding."""
    embedder = FakeEmbedding()
    texts = ["Text 1", "Text 2", "Text 3"]

    vecs = embedder.embed(texts)
    assert len(vecs) == 3
    assert all(len(v) == EMBED_DIM for v in vecs)


@pytest.mark.live_ollama
def test_ollama_embedding_real() -> None:
    """OllamaEmbedding connects to real Ollama (skip if down)."""
    try:
        embedder = OllamaEmbedding()
        # Test that we can get dimension
        dim = embedder.dim
        assert dim == EMBED_DIM
        assert embedder.model_id == "ollama:bge-m3"

        # Test embedding
        vecs = embedder.embed(["test text"])
        assert len(vecs) == 1
        assert len(vecs[0]) == EMBED_DIM
    except EmbeddingUnavailable:
        pytest.skip("Ollama not available")


def test_ollama_embedding_unavailable() -> None:
    """OllamaEmbedding raises EmbeddingUnavailable when Ollama is down."""
    # Point to a dead port
    embedder = OllamaEmbedding(url="http://localhost:9999")

    with pytest.raises(EmbeddingUnavailable):
        _ = embedder.dim

    with pytest.raises(EmbeddingUnavailable):
        embedder.embed(["test"])


def test_ollama_empty_batch() -> None:
    """OllamaEmbedding handles empty batch gracefully."""
    embedder = FakeEmbedding()
    vecs = embedder.embed([])
    assert vecs == []
