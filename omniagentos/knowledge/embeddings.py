"""Embedding provider implementations for the knowledge subsystem."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import requests

from omniagentos.knowledge.contracts import EMBED_DIM, EmbeddingUnavailable

if TYPE_CHECKING:
    pass


class OllamaEmbedding:
    """Ollama embedding provider — calls local bge-m3 server."""

    def __init__(self, url: str = "http://localhost:11434") -> None:
        self.url = url
        self._dim: int | None = None
        self._model_id = "ollama:bge-m3"

    @property
    def dim(self) -> int:
        """Embedding dimension (cached after first call)."""
        if self._dim is None:
            # Try endpoints in order of preference
            for endpoint in ["/api/embed", "/api/embeddings"]:
                try:
                    resp = requests.post(
                        f"{self.url}{endpoint}",
                        json={"model": "bge-m3", "input": ["test"]},
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        embeddings = data.get("embeddings")
                        if not embeddings:
                            # Some Ollama versions use different field names
                            embeddings = data.get("data")
                        if embeddings:
                            self._dim = len(embeddings[0])
                            break
                except Exception:
                    continue

            if self._dim is None:
                raise EmbeddingUnavailable("Could not determine Ollama embedding dimension")

        return self._dim

    @property
    def model_id(self) -> str:
        """Model identifier."""
        return self._model_id

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Ollama bge-m3."""
        if not texts:
            return []

        # Try both endpoints
        for endpoint in ["/api/embed", "/api/embeddings"]:
            try:
                resp = requests.post(
                    f"{self.url}{endpoint}",
                    json={"model": "bge-m3", "input": texts},
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embeddings = data.get("embeddings")
                    if not embeddings:
                        embeddings = data.get("data")
                    if embeddings:
                        return embeddings
            except Exception:
                continue

        raise EmbeddingUnavailable(f"Ollama embedding failed at {self.url}")


class FakeEmbedding:
    """Deterministic fake embedding provider for tests."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self._dim = dim
        self._model_id = "fake"

    @property
    def dim(self) -> int:
        """Embedding dimension."""
        return self._dim

    @property
    def model_id(self) -> str:
        """Model identifier."""
        return self._model_id

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate deterministic embeddings from text hash."""
        result = []
        for text in texts:
            # Hash the text to get a seed
            seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)

            # Use the seed to generate a deterministic unit vector
            # This is a simple approach: use hash to generate random floats
            # and normalize to unit vector
            values: list[float] = []
            for i in range(self._dim):
                # Use different parts of the seed for different dimensions
                hash_obj = hashlib.sha256(f"{seed}_{i}".encode())
                val = int(hash_obj.hexdigest(), 16) % 1000 / 1000.0
                values.append(val - 0.5)  # Center around 0

            # Normalize to unit vector
            magnitude = sum(v * v for v in values) ** 0.5
            if magnitude > 0:
                values = [v / magnitude for v in values]
            else:
                values = [1.0 / (self._dim**0.5)] * self._dim

            result.append(values)

        return result
