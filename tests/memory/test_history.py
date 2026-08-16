"""Decisive tests for the hybrid history-retrieval leg (omniagentos.memory.history).

The leg is the pre-registered memcert fix H1+H3 (devtasks/memcert/DESIGN-v2.md):
BM25 over the turns BEHIND the recency window, scored ``bm25 x recency_prior``.
These tests pin the retrieval semantics the sufficiency certification
(tests/memcert/test_sufficiency.py) depends on.
"""

from __future__ import annotations

from omniagentos.memory.contracts import ConversationTurn
from omniagentos.memory.history import HistoryHit, query_terms, retrieve_history


def _turn(seq: int, content: str, role: str = "user") -> ConversationTurn:
    return ConversationTurn(seq=seq, role=role, content=content)  # type: ignore[arg-type]


class _Reader:
    def __init__(self, turns: list[ConversationTurn]) -> None:
        self.turns = turns

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        return self.turns[-limit:] if limit < len(self.turns) else list(self.turns)

    def resolve_ancestors(self, scope_type: str, scope_id: str):  # pragma: no cover
        return []

    def rolling_summary(self, scope_type: str, scope_id: str):  # pragma: no cover
        return None


class _RaisingReader:
    def recent_turns(self, scope_type: str, scope_id: str, limit: int):
        raise RuntimeError("table lost")

    def resolve_ancestors(self, scope_type: str, scope_id: str):  # pragma: no cover
        return []

    def rolling_summary(self, scope_type: str, scope_id: str):  # pragma: no cover
        return None


class _NoneReturningReader:
    """Protocol is not runtime-enforced: a reader can return None without
    raising. retrieve_history must degrade to [], per its "Never raises"
    contract, not let a TypeError escape from the unguarded gap between the
    reader-fault try/except and the scoring try/except."""

    def recent_turns(self, scope_type: str, scope_id: str, limit: int):
        return None

    def resolve_ancestors(self, scope_type: str, scope_id: str):  # pragma: no cover
        return []

    def rolling_summary(self, scope_type: str, scope_id: str):  # pragma: no cover
        return None


def _corpus(n: int = 40) -> list[ConversationTurn]:
    turns = [_turn(i, f"Routine filler chatter with nothing of note, slot {i}.") for i in range(n)]
    return turns


def test_relevant_old_turn_is_retrieved_and_irrelevant_is_not() -> None:
    turns = _corpus()
    turns[3] = _turn(3, "Bevora leads the Kilmot effort.")
    hits = retrieve_history(
        _Reader(turns), "task", "t1", "Which machine serves the project Bevora leads?",
        top_k=4, recent_window=12,
    )
    assert [h.turn.seq for h in hits] == [3]
    assert all(isinstance(h, HistoryHit) and h.score > 0 for h in hits)


def test_zero_overlap_returns_empty_never_noise() -> None:
    # No query-term overlap anywhere: padding the section with noise is the
    # axis-E hallucination bait the leg must never produce.
    hits = retrieve_history(
        _Reader(_corpus()), "task", "t1", "zanzibar quartz telescope", top_k=4, recent_window=12
    )
    assert hits == []


def test_recency_prior_prefers_newer_statement_of_equal_relevance() -> None:
    turns = _corpus(60)
    turns[5] = _turn(5, "The Kilmot budget is 52400 dollars.")
    turns[30] = _turn(30, "The Kilmot budget is 57900 dollars.")
    hits = retrieve_history(
        _Reader(turns), "task", "t1", "What is the Kilmot budget in dollars?",
        top_k=2, recent_window=12,
    )
    assert [h.turn.seq for h in hits] == [30, 5]  # newer first: the axis-D protection


def test_recency_floor_keeps_old_relevant_turns_retrievable() -> None:
    turns = _corpus(400)
    turns[0] = _turn(0, "Ancient but load-bearing: Kilmot answers requests at 4711.")
    hits = retrieve_history(
        _Reader(turns), "task", "t1", "Which port does Kilmot answer requests at?",
        top_k=1, recent_window=12,
    )
    assert [h.turn.seq for h in hits] == [0]


def test_recent_window_and_system_turns_are_excluded() -> None:
    turns = _corpus(30)
    turns[28] = _turn(28, "Kilmot answers requests at 4711.")  # inside window of 12
    turns[4] = _turn(4, "Kilmot summary rollup.", role="system")
    hits = retrieve_history(
        _Reader(turns), "task", "t1", "Kilmot requests port", top_k=5, recent_window=12
    )
    assert [h.turn.seq for h in hits] == []


def test_top_k_and_determinism() -> None:
    turns = _corpus(40)
    for seq in (2, 6, 10, 14, 18):
        turns[seq] = _turn(seq, f"Kilmot deploy note number {seq} for the Kilmot service.")
    first = retrieve_history(_Reader(turns), "task", "t1", "Kilmot deploy", top_k=3, recent_window=12)
    second = retrieve_history(_Reader(turns), "task", "t1", "Kilmot deploy", top_k=3, recent_window=12)
    assert len(first) == 3
    assert [(h.turn.seq, h.score) for h in first] == [(h.turn.seq, h.score) for h in second]


def test_raising_reader_degrades_to_empty() -> None:
    assert retrieve_history(_RaisingReader(), "task", "t1", "Kilmot deploy", top_k=3) == []


def test_none_returning_reader_degrades_to_empty_not_typeerror() -> None:
    # gemini review, PR#407: reader.recent_turns() returning None (a healthy
    # call, no exception) must not crash retrieve_history via the unguarded
    # slicing/comprehension gap between the two try/except blocks.
    assert retrieve_history(_NoneReturningReader(), "task", "t1", "Kilmot deploy", top_k=3) == []


def test_query_terms_filters_stopwords_and_dedupes() -> None:
    terms = query_terms("What is the THE budget budget of Kilmot?")
    assert terms == ["budget", "kilmot"]


def test_short_fragments_coalesce_and_stay_retrievable() -> None:
    # gemini-critic round 3: a length floor that silently DROPS short sentences
    # is a favourable-absence bug. "PR-992." co-located with a long sentence
    # must remain retrievable (coalesced into a neighbouring unit).
    from omniagentos.memory.history import _split_sentences

    units = _split_sentences(
        "The deployment review for the billing stack finished cleanly. PR-992. "
        "Follow-up items were assigned to the usual owners afterwards."
    )
    assert any("PR-992" in u for u in units)
    assert "".join(units).count("PR-992") == 1

    turns = _corpus(30)
    turns[4] = _turn(4, "The umbrella ticket for the Kilmot cleanup is tracked. KLM-77.")
    hits = retrieve_history(
        _Reader(turns), "task", "t1", "Which ticket tracks the Kilmot cleanup?",
        top_k=3, recent_window=12,
    )
    assert hits and any("KLM-77" in h.turn.content for h in hits)


def test_empty_query_or_topk_returns_empty() -> None:
    reader = _Reader(_corpus())
    assert retrieve_history(reader, "task", "t1", "the of and", top_k=3) == []
    assert retrieve_history(reader, "task", "t1", "Kilmot", top_k=0) == []
