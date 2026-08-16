"""Relevance retrieval over a scope's OLDER conversation turns (hybrid leg).

The v1 assembler is recency-only: it renders the newest ``max_node_turns`` turns
and nothing else from the conversation. memcert measured the consequence
(devtasks/memcert/RESULTS-2026-08-12.md): multi-session integration (axis B)
collapses to ~0 and early-session lessons fall off the window (axis G), while
knowledge updates (axis D) ace precisely BECAUSE recency structure wins there.

This module is the pre-registered fix (hypotheses H1 + H3): a BM25 retrieval
leg over the turns BEHIND the recency window, scored ``bm25 x recency_prior``
so that when the same fact appears in an old and a new turn, the new one wins
(protects D), while an old-but-relevant turn still surfaces (fixes B/G/H).

Deliberately stdlib-only and dependency-light. BM25 parameters mirror the
memcert ``rag`` baseline byte-for-byte (k1=1.5, b=0.75, +0.5/+0.5-smoothed
idf) so certified numbers transfer between the instrument and production.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

from omniagentos.memory.contracts import ConversationReader, ConversationTurn

_LOG = logging.getLogger(__name__)

# Matches memcert scripts/memcert/arms.py so instrument and production agree.
_BM25_K1 = 1.5
_BM25_B = 0.75

# Recency prior over candidate turns: FLOOR + (1-FLOOR) * DECAY^age_rank where
# age_rank counts newer candidate turns. FLOOR keeps old-but-relevant turns
# retrievable (never below half weight); DECAY separates a fresh statement of a
# value from a stale one when their BM25 scores tie (the axis-D protection).
_RECENCY_FLOOR = 0.5
_RECENCY_DECAY = 0.99

# Second-hop query expansion (multi-hop joins): a join's second fact often
# shares NO term with the question ("which machine carries the workloads of the
# project that Bevora leads?" — the project's name is the bridge, and it only
# appears in hop-1 evidence). Hop 2 extracts entity-like RARE terms from the
# top hop-1 hits and retrieves again. memcert measured the failure this closes:
# axis-B sufficiency 0.0 at M/L scales single-hop, restored by hop 2.
_HOP_TOP_HITS = 3  # hop-1 hits mined for expansion terms
_HOP_MAX_TERMS = 8  # expansion terms tried, strongest-evidence first
_HOP_DF_FRACTION = 0.08  # a term is entity-like when df <= max(2, frac * n_docs)
_HOP_WEIGHT = 0.6  # hop-2 score weight relative to hop-1
_HOP_SLOTS = 2  # reserved result slots for hop-2 lookups (never displace base hits)

# Same ~30-word stopword list as scripts/memcert/arms.py (query side only).
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the of to in on at is are was were be been being this that these
    those and or but for with as by from it its what which who whom when
    where why how do does did
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Below this, a "sentence" is a fragment (an initial, a number, a stray token);
# fold it into retrieval by skipping it — fragments carry no evidence.
_MIN_SENTENCE_CHARS = 15


def _split_sentences(text: str) -> list[str]:
    """Deterministic sentence split; short fragments COALESCE, never drop.

    A fragment below the floor ("PR-992.", "Yes.", an ID or date) merges into
    its neighbouring unit instead of being discarded — a length floor that
    silently erases short facts is a favourable-absence bug: the retriever
    could never surface them and nothing would say so (gemini-critic round 3,
    2026-08-13). Every character of the turn stays retrievable in exactly one
    unit.
    """
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and (len(part) < _MIN_SENTENCE_CHARS or len(merged[-1]) < _MIN_SENTENCE_CHARS):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged

# Requests every turn while keeping the ConversationReader protocol unchanged
# (the same idiom as assemble._ALL_TURNS_LIMIT).
_ALL_TURNS_LIMIT = (1 << 63) - 1


@dataclass(frozen=True)
class HistoryHit:
    """One retrieved older turn with its combined relevance score."""

    turn: ConversationTurn
    score: float


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def query_terms(query: str) -> list[str]:
    """Deduplicated, stopword-filtered query tokens (order-preserving)."""
    return list(dict.fromkeys(t for t in _tokenize(query) if t not in _STOPWORDS))


def _bm25_scores(docs: list[list[str]], terms: list[str]) -> list[float]:
    """BM25 score per document for ``terms`` (k1/b/idf identical to memcert rag)."""
    if not docs or not terms:
        return [0.0] * len(docs)
    doc_lens = [len(d) for d in docs]
    n_docs = len(docs)
    avgdl = sum(doc_lens) / n_docs if n_docs else 0.0
    df = {term: sum(1 for d in docs if term in d) for term in set(terms)}
    scores: list[float] = []
    for doc, dl in zip(docs, doc_lens, strict=True):
        counts = Counter(doc)
        score = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if freq == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log((n_docs - n_q + 0.5) / (n_q + 0.5) + 1)
            norm = 1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0)
            score += idf * (freq * (_BM25_K1 + 1)) / (freq + _BM25_K1 * norm)
        scores.append(score)
    return scores


def retrieve_history(
    reader: ConversationReader,
    scope_type: str,
    scope_id: str,
    query: str,
    *,
    top_k: int = 6,
    recent_window: int = 12,
) -> list[HistoryHit]:
    """Top-``top_k`` older turns of the scope relevant to ``query``.

    "Older" means strictly behind the newest ``recent_window`` turns — those
    are already rendered verbatim by the assembler's conversation section, so
    retrieving them again would only duplicate budget. ``role='system'`` turns
    (rolling summaries etc.) are excluded; they have their own sections.

    Scoring is ``bm25 x recency_prior`` with a deterministic tie-break on
    newest-first ``seq``. Only positive-BM25 turns are returned: a turn with no
    query-term overlap carries no retrieval signal, and padding the section
    with irrelevant history is exactly the hallucination bait memcert's axis E
    measured (never-stated facts get fabricated when noise fills the context).

    Never raises: any reader/scoring fault degrades to ``[]``.
    """
    if top_k <= 0:
        return []
    terms = query_terms(query)
    if not terms:
        return []
    try:
        turns = reader.recent_turns(scope_type, scope_id, _ALL_TURNS_LIMIT)
        # ConversationReader is a Protocol, not runtime-enforced: a reader can
        # return None without raising. Guard the same way as an exception so
        # the slicing/comprehension below never sees a non-list (gemini
        # review, PR#407 -- the "Never raises" contract must hold on this
        # gap too, not just on the call itself).
        turns = turns or []
        if recent_window > 0:
            turns = turns[:-recent_window] if len(turns) > recent_window else []
        source_turns = [t for t in turns if t.role != "system" and t.content.strip()]
    except Exception:  # noqa: BLE001 -- retrieval is best-effort; never fail assembly.
        # LOUD degradation: an empty result from a FAULT must be
        # distinguishable in logs from a healthy zero-hit search (favourable
        # absence — codex-critic CR-004).
        _LOG.warning(
            "history retrieval reader fault for %s:%s; degrading to empty",
            scope_type,
            scope_id,
            exc_info=True,
        )
        return []
    if not source_turns:
        return []
    try:
        # SENTENCE granularity: the retrieval unit is a sentence, rendered as a
        # turn whose content is just that sentence. Whole-turn retrieval put
        # co-located FOREIGN facts next to the evidence ("Kireti leads the
        # Rutadu effort. Gehori runs its workloads on Nopikihe.") and models
        # joined the adjacent confusable instead of the true bridge — measured
        # live 2026-08-13: axis B system arm 84/96 WRONG with evidence present
        # (sufficiency 0.94). Sentence hits keep precision without the bait.
        candidates: list[ConversationTurn] = []
        for turn in source_turns:
            for sentence in _split_sentences(turn.content):
                candidates.append(turn.model_copy(update={"content": sentence}))
        if not candidates:
            return []
        docs = [_tokenize(t.content) for t in candidates]
        base = _bm25_scores(docs, terms)
        n = len(candidates)

        def _prior(i: int) -> float:
            return _RECENCY_FLOOR + (1 - _RECENCY_FLOOR) * _RECENCY_DECAY ** (n - 1 - i)

        base_hits = sorted(
            (
                HistoryHit(turn=candidates[i], score=base[i] * _prior(i))
                for i in range(n)
                if base[i] > 0.0
            ),
            key=lambda h: (-h.score, -h.turn.seq, h.turn.content),
        )

        # Hop 2 (join bridges): reserve a minority of slots for entity lookups
        # the base query cannot reach. Reserved slots mean hop noise can never
        # DISPLACE a base hit (measured: additive hop scoring dropped S-scale
        # sufficiency), while a genuine bridge still gets guaranteed room.
        hop_slots = min(_HOP_SLOTS, max(0, top_k - 1)) if base_hits else 0
        hop_hits: list[HistoryHit] = []
        if hop_slots:
            # Exclude EVERY potentially-retained base hit (the full top_k
            # slice, not just the guaranteed slice): when fewer hop hits are
            # found than reserved slots, the retained base slice widens, and a
            # hop lookup that picked from that widened tail would render the
            # same sentence twice (gemini-critic F3, 2026-08-13). Identity is
            # (seq, content) — sentence granularity means one turn can
            # legitimately contribute two different sentences.
            taken = {(h.turn.seq, h.turn.content) for h in base_hits[:top_k]}
            for term in _expansion_terms(docs, base, terms):
                if len(hop_hits) >= hop_slots:
                    break
                term_scores = _bm25_scores(docs, [term])
                # Filter taken candidates BEFORE max(): the expansion term was
                # mined from the top base hits, so the argmax over all
                # candidates almost always lands back on a taken hit — skipping
                # the term there starves hop expansion entirely (gemini-critic
                # round 2 F3). The best UNTAKEN candidate is what hop 2 is for.
                best = max(
                    (
                        i
                        for i in range(n)
                        if term_scores[i] > 0.0
                        and (candidates[i].seq, candidates[i].content) not in taken
                    ),
                    key=lambda i: (term_scores[i] * _prior(i), i),
                    default=None,
                )
                if best is None:
                    continue
                taken.add((candidates[best].seq, candidates[best].content))
                hop_hits.append(
                    HistoryHit(
                        turn=candidates[best],
                        score=_HOP_WEIGHT * term_scores[best] * _prior(best),
                    )
                )
        kept = base_hits[: top_k - len(hop_hits)] + hop_hits
        kept.sort(key=lambda h: (-h.score, -h.turn.seq))
        return kept[:top_k]
    except Exception:  # noqa: BLE001 -- scoring fault must not break assembly.
        _LOG.warning(
            "history retrieval scoring fault for %s:%s; degrading to empty",
            scope_type,
            scope_id,
            exc_info=True,
        )
        return []


def _expansion_terms(
    docs: list[list[str]], base_scores: list[float], query: list[str]
) -> list[str]:
    """Entity-like rare terms mined from the top hop-1 hits (the join bridge).

    A term qualifies when it is not a query term, not a stopword, at least 3
    chars, and RARE across the corpus (df <= max(2, _HOP_DF_FRACTION * n)) —
    rarity is what separates proper-noun bridges from filler vocabulary.
    Candidates are ranked by the summed BASE score of the mined hits containing
    them (terms from the strongest hop-1 evidence first), then alphabetically
    (fully deterministic), capped at _HOP_MAX_TERMS.
    """
    ranked = sorted(
        (i for i, s in enumerate(base_scores) if s > 0.0),
        key=lambda i: (-base_scores[i], i),
    )[:_HOP_TOP_HITS]
    if not ranked:
        return []
    n_docs = len(docs)
    df_cap = max(2, int(_HOP_DF_FRACTION * n_docs))
    query_set = set(query)
    weight: dict[str, float] = {}
    for i in ranked:
        for tok in set(docs[i]):
            if len(tok) < 3 or tok in query_set or tok in _STOPWORDS:
                continue
            weight[tok] = weight.get(tok, 0.0) + base_scores[i]
    if not weight:
        return []
    # Exact single-pass DF (codex-critic CR-003 through R3): one linear sweep
    # builds document frequency for EVERY term, so no shortlist and no probe
    # budget exists to crowd a rare bridge token out — the two prior bounded
    # designs each starved it in a narrower way. Cost is O(total corpus
    # tokens), the same order the BM25 pass above already paid.
    df_all = Counter(tok for d in docs for tok in set(d))
    candidates = [t for t in weight if df_all[t] <= df_cap]
    candidates.sort(key=lambda t: (-weight[t], t))
    return candidates[:_HOP_MAX_TERMS]


__all__ = ["HistoryHit", "query_terms", "retrieve_history"]
