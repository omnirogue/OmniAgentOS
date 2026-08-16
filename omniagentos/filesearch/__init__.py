"""Federated + semantic file search across local + iCloud + Google Drive + Dropbox.

    from omniagentos.filesearch import search, search_files_text
    print(search_files_text("globex contract"))            # keyword (Spotlight)
    hits = search("the doc about our pricing", mode="hybrid")    # keyword + semantic

Layers: Spotlight keyword search (``search_files``), a local-embedding catalog + semantic
search (``semantic_search`` / ``reindex``) with metadata filtering (``filter_files`` —
root/category/sort), hybrid fusion (``hybrid_search``), and a DEEP chunk-level pgvector
index in the knowledge Postgres (``semantic_index`` / ``semantic_query`` — LOCAL Ollama
embeddings only; nothing leaves the machine). All degrade gracefully — no embedding
backend / no catalog → keyword-only. Placeholder-safe.
"""

from omniagentos.filesearch.catalog import category_for, filter_files, reindex, root_label
from omniagentos.filesearch.catalog import stats as catalog_stats
from omniagentos.filesearch.semantic import (
    SemanticUnavailable,
    hybrid_search,
    semantic_index,
    semantic_query,
    semantic_search,
)
from omniagentos.filesearch.service import (
    SearchHit,
    render_hits,
    resolve_roots,
    search_files,
    search_files_text,
)


def search(
    query: str,
    mode: str = "hybrid",
    scopes: list[str] | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    """Dispatch by mode: ``keyword`` (Spotlight), ``semantic`` (catalog), or ``hybrid``
    (both, fused — the default and best)."""
    if mode == "semantic":
        return semantic_search(query, limit=limit)
    if mode == "keyword":
        return search_files(query, scopes=scopes, limit=limit)
    return hybrid_search(query, scopes=scopes, limit=limit)


__all__ = [
    "SearchHit",
    "SemanticUnavailable",
    "catalog_stats",
    "category_for",
    "filter_files",
    "hybrid_search",
    "reindex",
    "render_hits",
    "resolve_roots",
    "root_label",
    "search",
    "search_files",
    "search_files_text",
    "semantic_index",
    "semantic_query",
    "semantic_search",
]
