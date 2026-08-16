"""Fail-open unified semantic search with lexical fallback."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from omniagentos.knowledge.contracts import EmbeddingUnavailable
from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.search import build_index

from .constants import MAX_QUERY_LENGTH, MAX_RESULT_COUNT
from .index import build_embedder, titles_for
from .store import ALL_KINDS, SemKind, SemsearchStore


@dataclass(frozen=True, slots=True)
class SemHit:
    """One ranked result from semantic search or its lexical fallback."""

    kind: SemKind
    ref_id: str
    title: str
    score: float
    source: str


def _selected_kinds(kind: str) -> tuple[SemKind, ...]:
    if kind == "all":
        return ALL_KINDS
    if kind in ALL_KINDS:
        return (kind,)  # type: ignore[return-value]
    return ()


def _tool_fallback(query: str, kind: SemKind, limit: int) -> list[SemHit]:
    catalog = default_catalog()
    entries: list[CatalogEntry]
    if kind == "capability":
        entries = [entry for entry in catalog.values() if entry.source == "connector"]
    else:
        entries = list(catalog.values())
    return [
        SemHit(kind, hit.entry.id, hit.entry.label, hit.score, "lexical-fallback")
        for hit in build_index(entries).search(query, limit)
    ]


def _lexical_fallback(query: str, kinds: tuple[SemKind, ...], limit: int) -> list[SemHit]:
    hits: list[SemHit] = []
    for selected in kinds:
        try:
            if selected == "skill":
                from omniagentos.skills import search as search_skills

                hits.extend(
                    SemHit(
                        "skill",
                        str(row.get("id") or row.get("slug") or ""),
                        str(row.get("title") or row.get("slug") or row.get("id") or ""),
                        float(row.get("score") or 0.0),
                        "lexical-fallback",
                    )
                    for row in search_skills(query, limit=limit)
                )
            else:
                hits.extend(_tool_fallback(query, selected, limit))
        except Exception:
            continue
    hits.sort(key=lambda hit: (-hit.score, hit.kind, hit.ref_id))
    return hits[:limit]


def search(query: str, *, kind: str = "all", limit: int = 10) -> list[SemHit]:
    """Search the semantic index, falling back lexically on every dependency error."""
    normalized_query = ""
    resolved_limit = 0
    kinds: tuple[SemKind, ...] = ()
    store: SemsearchStore | None = None
    try:
        normalized_query = query.strip()
        resolved_limit = min(MAX_RESULT_COUNT, max(0, int(limit)))
        kinds = _selected_kinds(kind)
        if (
            not normalized_query
            or len(normalized_query) > MAX_QUERY_LENGTH
            or resolved_limit == 0
            or not kinds
        ):
            return []

        # Resolve the optional DSN before doing any embedding work. An unset DSN
        # should take the lexical path immediately, without probing Ollama.
        store = SemsearchStore()
        embedder = build_embedder()
        vector = embedder.embed([normalized_query])
        if len(vector) != 1:
            raise EmbeddingUnavailable("query embedder did not return exactly one vector")
        raw_hits = store.query(
            vector[0],
            model_id=embedder.model_id,
            kind=None if kind == "all" else kinds[0],
            limit=resolved_limit,
        )
        if not raw_hits:
            return _lexical_fallback(normalized_query, kinds, resolved_limit)
        title_maps = {selected: titles_for(selected) for selected in kinds}
        return [
            SemHit(
                raw.kind,
                raw.ref_id,
                title_maps.get(raw.kind, {}).get(raw.ref_id, raw.ref_id),
                raw.score,
                "semantic",
            )
            for raw in raw_hits
        ]
    except psycopg.Error:
        return _lexical_fallback(normalized_query, kinds, resolved_limit)
    except Exception:
        return _lexical_fallback(normalized_query, kinds, resolved_limit)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
