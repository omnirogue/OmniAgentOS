"""The retrieval half of the deferred tool catalog.

Given a natural-language need, return AT MOST max_definitions() (default 5) full
tool definitions, drawn ONLY from what the caller's ExposureDecision says they may
see.

Security Property (Construction-not-Filtering):
-----------------------------------------------
A hidden tool is not filtered out of the results; it is never inserted into the
index in the first place. The index is built from decision.allowed +
decision.deferred and nothing else, so there is no code path — no query, no
scoring tie, no off-by-one in a limit — that can surface a hidden tool.
Post-filtering would make that a property of the filter's correctness;
construction makes it a property of the data. That distinction is the whole
design.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.config import max_definitions
from omniagentos.toolplane.exposure import ExposureDecision

__all__ = [
    "B",
    "BM25Index",
    "FIELD_WEIGHTS",
    "K1",
    "SearchHit",
    "build_index",
    "search_tools",
    "tokenize",
]

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "it", "with", "my"}
)
_MIN_TOKEN_LEN: Final[int] = 2
_QUERY_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "find": ("search",),
}
K1: Final[float] = 1.5
B: Final[float] = 0.75

#: Field -> how many times its tokens are repeated in the document bag. Repetition IS the
#: weighting: BM25 scores term frequency, so emitting a field's tokens n times weights that
#: field n-fold without changing the scorer. Identity fields dominate because a user searching
#: "stripe refund" means the tool named that, not one whose prose mentions it.
FIELD_WEIGHTS: Final[dict[str, int]] = {
    "id": 3,
    "namespace": 3,
    "label": 2,
    "compact_hint": 2,
    "parameter_names": 2,
    "input_examples": 1,
    "description": 1,
}


def tokenize(text: str) -> list[str]:
    """Lowercase and tokenize a string by splitting on non-alphanumeric, camelCase, snake_case, and dotted ids.

    Drops stopwords and tokens shorter than the minimum length.
    """
    segments = [s for s in re.split(r"[^a-zA-Z0-9]+", text) if s]

    tokens: list[str] = []
    for seg in segments:
        s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", seg)
        s2 = re.sub(r"([A-Z]+)([A-Z][a-z0-9])", r"\1 \2", s1)
        parts = [p.lower() for p in s2.split() if p]

        seg_lower = seg.lower()
        if len(parts) > 1:
            tokens.append(seg_lower)
            tokens.extend(parts)
        else:
            tokens.append(seg_lower)

    return [t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS]


def _query_tokens(query: str) -> list[str]:
    """Tokenize a query and add its deterministic, one-way search synonyms.

    Synonyms are expanded only on the query. Catalog documents retain their exact
    declared fields and weights, including the construction-time visibility boundary.
    """
    tokens = tokenize(query)
    return [token for source in tokens for token in (source, *_QUERY_SYNONYMS.get(source, ()))]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A single hit returned from a BM25 index search, wrapping a CatalogEntry and its score."""

    entry: CatalogEntry
    score: float


class BM25Index:
    """A hand-rolled Okapi BM25 over tool documents. ~40 lines; no dependency earns its place here."""

    def __init__(self, entries: Iterable[CatalogEntry]) -> None:
        """Initialize the BM25 index and compute corpus statistics.

        Builds the token bag for each entry by tokenizing fields weighted according
        to FIELD_WEIGHTS.
        """
        self._entries: list[CatalogEntry] = list(entries)
        self._N: int = len(self._entries)

        self._doc_lengths: list[int] = []
        self._doc_term_freqs: list[dict[str, int]] = []
        self._doc_freqs: dict[str, int] = {}

        for entry in self._entries:
            token_bag: list[str] = []
            for field, weight in FIELD_WEIGHTS.items():
                val = getattr(entry, field)
                field_tokens: list[str] = []
                if isinstance(val, (list, tuple)):
                    for item in val:
                        field_tokens.extend(tokenize(item))
                elif isinstance(val, str):
                    field_tokens.extend(tokenize(val))
                token_bag.extend(field_tokens * weight)

            doc_len = len(token_bag)
            self._doc_lengths.append(doc_len)

            freqs: dict[str, int] = {}
            for token in token_bag:
                freqs[token] = freqs.get(token, 0) + 1
            self._doc_term_freqs.append(freqs)

            for token in freqs:
                self._doc_freqs[token] = self._doc_freqs.get(token, 0) + 1

        if self._N > 0:
            self._avgdl: float = sum(self._doc_lengths) / self._N
        else:
            self._avgdl = 0.0

    @property
    def size(self) -> int:
        """Return the number of entries indexed."""
        return self._N

    def search(self, query: str, limit: int) -> tuple[SearchHit, ...]:
        """Perform Okapi BM25 search over the indexed documents.

        Uses the always-positive IDF formulation to avoid negative scores for frequent terms.
        """
        limit = max(0, limit)
        if limit == 0:
            return ()

        q_tokens = _query_tokens(query)
        if not q_tokens or self._N == 0:
            return ()

        hits: list[SearchHit] = []
        unique_q_tokens = set(q_tokens)

        for i, entry in enumerate(self._entries):
            score = 0.0
            doc_len = self._doc_lengths[i]
            term_freqs = self._doc_term_freqs[i]

            for q in unique_q_tokens:
                df = self._doc_freqs.get(q, 0)
                if df == 0:
                    continue

                idf = math.log(1.0 + (self._N - df + 0.5) / (df + 0.5))

                f = term_freqs.get(q, 0)
                if f == 0:
                    continue

                numerator = f * (K1 + 1.0)
                denominator = f + K1 * (1.0 - B + B * (doc_len / self._avgdl))
                score += idf * (numerator / denominator)

            if score > 0.0:
                hits.append(SearchHit(entry=entry, score=score))

        hits.sort(key=lambda h: (-h.score, h.entry.id))
        return tuple(hits[:limit])


def build_index(entries: Iterable[CatalogEntry]) -> BM25Index:
    """Build a BM25Index from an iterable of CatalogEntry objects."""
    return BM25Index(entries)


def search_tools(
    query: str,
    decision: ExposureDecision,
    catalog: Mapping[str, CatalogEntry] | None = None,
    limit: int | None = None,
) -> tuple[SearchHit, ...]:
    """Search the catalog for tools that are visible (allowed or deferred) to the caller.

    Resolves the catalog (defaulting to default_catalog()), builds a BM25Index from
    visible entries, and performs the search, limiting results to max_definitions()
    (or the specified limit).
    """
    cat = catalog if catalog is not None else default_catalog()

    # Security Property: Construction-not-Filtering Security Property.
    # By constructing the BM25 index exclusively from the authorized visible tools
    # (decision.allowed + decision.deferred) and ignoring decision.hidden entirely,
    # we ensure that unauthorized tools are structurally impossible to retrieve.
    # There is no code path, score tie, or off-by-one limit that can ever expose
    # a hidden tool because they are never present in the index.
    visible_entries: list[CatalogEntry] = []
    seen_ids: set[str] = set()

    for tool_id in decision.allowed + decision.deferred:
        if tool_id in seen_ids:
            continue
        seen_ids.add(tool_id)

        entry = cat.get(tool_id)
        if entry is not None:
            visible_entries.append(entry)

    resolved_limit = limit if limit is not None else max_definitions()
    resolved_limit = max(0, resolved_limit)

    index = build_index(visible_entries)
    return index.search(query, resolved_limit)
