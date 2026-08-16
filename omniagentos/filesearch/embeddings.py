"""Local text embeddings for semantic file search.

Ollama-backed (``nomic-embed-text`` by default — 768-dim, local, ZERO API cost), with a
graceful fallback: every function returns ``None`` / ``False`` instead of raising when no
backend is running, so the catalog + semantic layer degrade cleanly to keyword-only
search. No third-party client — just the stdlib over Ollama's HTTP API.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"


def is_available(model: str = DEFAULT_MODEL, timeout: float = 2.0) -> bool:
    """True iff Ollama is up and has ``model`` pulled. Never raises."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    names = {str(entry.get("name", "")).split(":")[0] for entry in data.get("models", [])}
    return model.split(":")[0] in names


def embed(
    texts: list[str], model: str = DEFAULT_MODEL, timeout: float = 120.0
) -> list[list[float]] | None:
    """Embed a batch → one vector per text, or ``None`` if the backend is unavailable
    (the caller then degrades to keyword search). Never raises."""
    if not texts:
        return []
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    vectors = data.get("embeddings")
    if isinstance(vectors, list) and len(vectors) == len(texts):
        return vectors
    return None


def embed_one(text: str, model: str = DEFAULT_MODEL) -> list[float] | None:
    result = embed([text], model=model)
    return result[0] if result else None
