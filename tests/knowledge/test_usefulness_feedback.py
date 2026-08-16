"""T6.3 — the knowledge feedback loop, closed at both ends.

Two defects are covered here.

(1) ``facts.helped_count`` was written by ``record_helped`` and read by NOTHING: recall's
    modulation used trust/importance/recency only, so a fact that demonstrably rode along
    in dozens of successful runs ranked exactly like one that never helped anybody.

(2) The signal that was being written was run-level and binary — every fact in the run's
    ``recall_log`` row got credit, including the ones truncated off the tail of the
    rendered block, which the agent never saw.

The ranking half of these tests runs entirely in-process against a duck-typed stub store,
so they exercise the scoring math with no PostgreSQL dependency and can never be silently
skipped. The attribution half is genuinely database-backed (there is no honest way to test
``UPDATE facts ... FROM recall_log`` without a database) and runs whenever the suite's
session fixture successfully bootstraps the test DB.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omniagentos.knowledge.contracts import (
    RECALL_CHARS_PER_TOKEN,
    RECALL_FOOTER,
    RECALL_HEADER,
    Fact,
    FactStatus,
    RecalledFact,
    RecallResult,
)
from omniagentos.knowledge.recall import (
    _DECAY_LAMBDA_PER_DAY,
    _QUARANTINE_DISCOUNT,
    _USEFULNESS_CAP,
    _USEFULNESS_WEIGHT,
    _fact_line,
    _render,
    _usefulness,
    clear_run_state,
    recall,
)
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder, make_test_gate

# --------------------------------------------------------------------------------------
# In-process ranking harness (no PostgreSQL)
# --------------------------------------------------------------------------------------


# Shared across every stub row on purpose. Calling datetime.now() per row gives each fact
# a slightly different last_accessed, hence a slightly different recency decay, which shows
# up as last-bit score differences and defeats exact-equality assertions about the cap.
_FIXED_NOW = datetime.now(UTC)


def _row(
    fact_id: int,
    *,
    helped_count: int = 0,
    vector_rank: int | None = None,
    fts_rank: int | None = None,
    trust: float = 0.8,
    importance: float = 0.7,
    status: str = "active",
    statement: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One candidate row shaped exactly like recall_candidates() returns."""
    moment = now or _FIXED_NOW
    return {
        "id": fact_id,
        "statement": statement or f"stub fact {fact_id}",
        "discipline": None,
        "scope": "global",
        "episode_id": 1,
        "provenance": "extracted",
        "trust": trust,
        "confidence": 0.8,
        "status": status,
        "valid_at": moment,
        "recorded_at": moment,
        "invalid_at": None,
        "superseded_by": None,
        "importance": importance,
        "access_count": 0,
        "last_accessed": moment,
        "helped_count": helped_count,
        "embedding": None,
        "search_tsv": "ignored",
        "vector_rank": vector_rank,
        "fts_rank": fts_rank,
        "graph_activation": 0.0,
    }


class _StubStore:
    """Duck-typed stand-in; recall() with run_id=None performs no writes at all."""

    _embedder = None

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def recall_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self._rows)


def _scores(rows: list[dict[str, Any]]) -> dict[int, float]:
    result = recall(_StubStore(rows), prompt="stub query")  # type: ignore[arg-type]
    return {item.fact.id: item.score for item in result.facts}


# --------------------------------------------------------------------------------------
# (a) usefulness feeds ranking — bounded and saturating
# --------------------------------------------------------------------------------------


def test_usefulness_raises_rank_for_an_equally_relevant_fact() -> None:
    """Same retrieval evidence, same trust/importance/age — only helped_count differs."""
    never_helped = _row(1, fts_rank=1, helped_count=0)
    often_helped = _row(2, fts_rank=1, helped_count=8)

    scores = _scores([never_helped, often_helped])

    assert scores[2] > scores[1], "demonstrated usefulness must break a relevance tie"
    # 1 + 0.15*ln(9) = 1.3296...
    assert scores[2] / scores[1] == pytest.approx(1.0 + _USEFULNESS_WEIGHT * math.log1p(8))


def test_usefulness_ordering_is_monotonic_but_sharply_diminishing() -> None:
    """More help is never worse, and the 21st confirmation is worth a fraction of the 1st.

    (20/21 rather than 50/51: the cap is already reached at helped_count 27, so a 50-vs-51
    comparison would measure the cap rather than the logarithm.)
    """
    scores = _scores(
        [
            _row(1, fts_rank=1, helped_count=0),
            _row(2, fts_rank=1, helped_count=1),
            _row(3, fts_rank=1, helped_count=2),
            _row(4, fts_rank=1, helped_count=20),
            _row(5, fts_rank=1, helped_count=21),
        ]
    )
    assert scores[1] < scores[2] < scores[3] < scores[4] < scores[5]
    first_confirmation = scores[2] - scores[1]
    twenty_first_confirmation = scores[5] - scores[4]
    assert twenty_first_confirmation > 0
    assert twenty_first_confirmation < first_confirmation / 10, (
        "a linear term here is the rich-get-richer trap; the gain must decay hard"
    )


def test_usefulness_saturates_at_the_cap() -> None:
    """Past the cap, extra credit buys literally nothing — no runaway regime exists."""
    assert _usefulness(0) == 1.0
    assert _usefulness(10_000) == _USEFULNESS_CAP
    assert _usefulness(1_000_000) == _USEFULNESS_CAP
    # The cap binds from 28 confirmations onward — well inside the range a genuinely
    # useful fact reaches in production, so this is a real ceiling, not a theoretical one.
    assert _usefulness(28) == _USEFULNESS_CAP
    assert _usefulness(27) < _USEFULNESS_CAP

    scores = _scores(
        [
            _row(1, fts_rank=1, helped_count=1_000),
            _row(2, fts_rank=1, helped_count=10_000),
        ]
    )
    assert scores[2] == scores[1], "10x more credit past the cap must change nothing"


def test_cap_bounds_usefulness_below_a_much_more_relevant_fresh_fact() -> None:
    """A fact helped 10_000 times must not outrank a clearly better match with no history.

    The veteran is a single-backend hit at rank 25; the newcomer is found independently by
    BOTH vector and FTS at rank 1 and has never helped anyone. The 1.5x ceiling is smaller
    than that relevance gap by construction, so the newcomer wins — and would still win if
    helped_count were a billion.
    """
    veteran = _row(1, fts_rank=25, helped_count=10_000)
    newcomer = _row(2, vector_rank=1, fts_rank=1, helped_count=0)

    scores = _scores([veteran, newcomer])

    assert scores[2] > scores[1]
    # ...and would still win if helped_count were a billion (approx only because each
    # recall() call re-reads the clock, so the recency decay differs in the last bits).
    assert _usefulness(10**9) == _usefulness(10_000)
    billionaire = _scores([_row(1, fts_rank=25, helped_count=10**9), newcomer])
    assert billionaire[1] == pytest.approx(scores[1], rel=1e-9)
    assert billionaire[2] > billionaire[1]


def test_the_cap_and_not_merely_the_log_is_what_bounds_usefulness() -> None:
    """A case the LOG alone would get wrong, so the cap is demonstrably load-bearing.

    RRF compresses hard: the whole span from the best candidate to the 50th (the
    recall_candidates LIMIT) is only a 1.80x score ratio. Uncapped, 1 + 0.15*ln(10001) =
    2.38x, which is enough to drag the worst candidate in the pool past the best one on
    history alone — precisely the rich-get-richer failure. Capped at 1.5x it cannot.
    """
    veteran = _row(1, fts_rank=50, helped_count=10_000)  # last candidate slot
    newcomer = _row(2, fts_rank=1, helped_count=0)  # best match, no history

    scores = _scores([veteran, newcomer])
    assert scores[2] > scores[1]

    uncapped = 1.0 + _USEFULNESS_WEIGHT * math.log1p(10_000)
    assert uncapped > _USEFULNESS_CAP
    would_have_been = scores[1] / _USEFULNESS_CAP * uncapped
    assert would_have_been > scores[2], (
        "without the cap the worst candidate WOULD have outranked the best one"
    )


def test_scoring_is_bit_identical_when_nothing_has_ever_helped() -> None:
    """Cold-DB regression guard: the pre-T6.3 formula must be reproduced exactly."""
    aged = datetime.now(UTC) - timedelta(days=10)
    row = _row(7, vector_rank=3, fts_rank=5, trust=0.8, importance=0.7, helped_count=0)
    row["last_accessed"] = aged

    result = recall(_StubStore([row]), prompt="stub query")  # type: ignore[arg-type]
    item = result.facts[0]

    rrf = 1.0 / (60.0 + 3) + 1.0 / (60.0 + 5)
    bonus = 1.25  # two backends
    age_days = (datetime.now(UTC) - aged).total_seconds() / 86_400.0
    expected = rrf * bonus * 0.8 * (0.5 + 0.5 * 0.7) * math.exp(-_DECAY_LAMBDA_PER_DAY * age_days)
    assert item.score == pytest.approx(expected, rel=1e-6)
    assert item.signals["usefulness"] == 1.0


def test_quarantine_discount_still_applies_on_top_of_usefulness() -> None:
    """An unverified fact cannot buy its way back to parity with a verified one.

    The quarantine multiplier is applied AFTER the usefulness boost, so the best a
    much-helped quarantined fact can reach is 1.5 * 0.15 = 0.225x an equally relevant
    active fact — still far under the 0.25x bar the existing quarantine test asserts.
    """
    active = _row(1, fts_rank=1, helped_count=0)
    quarantined = _row(2, fts_rank=1, helped_count=10_000, status="quarantined")

    result = recall(
        _StubStore([active, quarantined]),  # type: ignore[arg-type]
        prompt="stub query",
        include_quarantined=True,
    )
    scores = {item.fact.id: item.score for item in result.facts}

    assert scores[2] < scores[1]
    assert scores[2] / scores[1] == pytest.approx(_USEFULNESS_CAP * _QUARANTINE_DISCOUNT)
    assert scores[2] < scores[1] * 0.25


def test_usefulness_cannot_resurrect_a_fact_no_retrieval_leg_surfaced() -> None:
    """The term multiplies an existing RRF score; a non-candidate has nothing to boost."""
    ghost = _row(1, helped_count=10_000)  # no vector_rank, no fts_rank, no graph
    real = _row(2, fts_rank=1, helped_count=0)

    scores = _scores([ghost, real])

    assert 1 not in scores
    assert 2 in scores


# --------------------------------------------------------------------------------------
# (b) per-fact attribution — the renderer decides who gets credit
# --------------------------------------------------------------------------------------


def _budget_admitting(lines: list[str], count: int) -> int:
    """Smallest token budget whose char cap admits exactly the first ``count`` lines."""
    body = sum(len(line) + 1 for line in lines[:count])
    cap = len(RECALL_HEADER) + body + len(RECALL_FOOTER) - 1
    return math.ceil((cap + 1) / RECALL_CHARS_PER_TOKEN)


def _stub_recalled(fact_id: int, statement: str) -> RecalledFact:
    now = datetime.now(UTC)
    return RecalledFact(
        fact=Fact(
            id=fact_id,
            statement=statement,
            episode_id=1,
            provenance="extracted",
            trust=0.8,
            confidence=0.8,
            status=FactStatus.ACTIVE,
            valid_at=now,
            recorded_at=now,
            importance=0.7,
            access_count=0,
            last_accessed=now,
            helped_count=0,
        ),
        score=1.0 / fact_id,
        signals={},
    )


def test_render_reports_only_the_facts_that_fit_the_budget() -> None:
    """The tail dropped for budget influenced nothing, so it is not reported as surfaced."""
    items = [
        _stub_recalled(i, f"budget probe fact number {i} with some padding text") for i in (1, 2, 3)
    ]
    result = RecallResult(facts=items)
    lines = [_fact_line(item) for item in items]

    block, surfaced = _render(result, budget_tokens=_budget_admitting(lines, 2))

    assert surfaced == [1, 2]
    assert "number 1" in block and "number 2" in block
    assert "number 3" not in block, "fact 3 was truncated; crediting it would be a lie"


def test_render_reports_nothing_surfaced_when_the_block_is_empty() -> None:
    """An empty list is 'credited nobody', which is distinct from 'attribution unknown'."""
    items = [_stub_recalled(1, "a statement far too long for this budget " * 10)]
    block, surfaced = _render(RecallResult(facts=items), budget_tokens=10)
    assert block == ""
    assert surfaced == []


# --------------------------------------------------------------------------------------
# Database-backed attribution (needs the test DB; the session fixture bootstraps it)
# --------------------------------------------------------------------------------------


def _active_fact(store: KnowledgeStore, statement: str) -> int:
    episode_id = store.add_episode(source="run", content=statement)
    fact_id = store.add_fact(
        statement=statement,
        episode_id=episode_id,
        discipline="code",
        trust=0.8,
        importance=0.7,
        embedding=make_fake_embedder().embed([statement])[0],
    )
    store.promote_fact(fact_id, make_test_gate())
    return fact_id


def _helped(store: KnowledgeStore, fact_id: int) -> int:
    fact = store.get_fact(fact_id)
    assert fact is not None
    return fact.helped_count


def test_migration_005_is_applied_to_the_test_database(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """Guard: if 005 were missing, every attribution test below would silently pass on
    the legacy fallback path instead of testing the new behaviour."""
    assert knowledge_store_admin._has_surfaced_column() is True


def test_record_helped_credits_only_the_facts_that_were_surfaced(
    knowledge_store_admin: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: recall ranks several facts, the budget admits one, one gets credit."""
    store = knowledge_store_admin
    ids = [
        _active_fact(store, "amber lattice deployment checksum verification"),
        _active_fact(store, "amber lattice rollback checksum verification"),
        _active_fact(store, "amber lattice checksum retention verification"),
    ]
    run_id = "run-t63-surfaced-only"
    try:
        preview = recall(store, prompt="amber lattice checksum verification")
        assert len(preview.facts) >= 2, "need at least two candidates to truncate one"
        lines = [_fact_line(item) for item in preview.facts]
        monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_BUDGET", str(_budget_admitting(lines, 1)))

        result = recall(store, prompt="amber lattice checksum verification", run_id=run_id)
        assert result.recall_id is not None
        ranked = [item.fact.id for item in result.facts]
        assert len(ranked) >= 2

        with store._conn().cursor() as cur:
            cur.execute(
                "SELECT fact_ids, surfaced_fact_ids FROM recall_log WHERE id = %s",
                (result.recall_id,),
            )
            row = cur.fetchone()
        assert row is not None
        logged_all, logged_surfaced = list(row[0]), list(row[1])
        assert logged_all == ranked, "the full candidate set is still logged"
        assert logged_surfaced == ranked[:1], "only the rendered prefix is attributed"

        store.record_helped(run_id)

        assert _helped(store, ranked[0]) == 1
        for truncated in ranked[1:]:
            assert _helped(store, truncated) == 0, "a fact the agent never saw must not be credited"
        for unranked in set(ids) - set(ranked):
            assert _helped(store, unranked) == 0
    finally:
        clear_run_state(run_id)


def test_record_helped_credits_nothing_when_nothing_was_surfaced(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """An empty attribution array means 'nobody helped', not 'attribution unknown'."""
    store = knowledge_store_admin
    fact_id = _active_fact(store, "teal meridian handshake timeout")
    run_id = "run-t63-empty-attribution"

    store.record_recall(
        run_id=run_id,
        agent_id=None,
        discipline=None,
        query_digest="deadbeefdeadbeef",
        fact_ids=[fact_id],
        tokens=0,
        latency_ms=1.0,
        surfaced_fact_ids=[],
    )
    store.record_helped(run_id)

    assert _helped(store, fact_id) == 0


def test_record_helped_falls_back_to_whole_run_credit_for_legacy_rows(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """Rows written before migration 005 carry NULL attribution and keep old behaviour."""
    store = knowledge_store_admin
    first = _active_fact(store, "ochre pylon migration ordering")
    second = _active_fact(store, "ochre pylon migration rollback")
    run_id = "run-t63-legacy-null"

    # surfaced_fact_ids omitted entirely -> NULL, exactly what a pre-005 row looks like.
    recall_id = store.record_recall(
        run_id=run_id,
        agent_id=None,
        discipline=None,
        query_digest="cafebabecafebabe",
        fact_ids=[first, second],
        tokens=10,
        latency_ms=1.0,
    )
    with store._conn().cursor() as cur:
        cur.execute("SELECT surfaced_fact_ids FROM recall_log WHERE id = %s", (recall_id,))
        row = cur.fetchone()
    assert row is not None and row[0] is None

    store.record_helped(run_id)

    assert _helped(store, first) == 1
    assert _helped(store, second) == 1


def test_attribution_degrades_to_whole_run_credit_on_an_unmigrated_database(
    knowledge_store_admin: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB without migration 005 must keep working, not lose recall entirely.

    record_recall runs inside recall(), whose only runner-facing wrapper swallows every
    exception — so an UndefinedColumn error there would show up as the runner silently
    injecting NO knowledge at all. The store probes for the column and falls back. This
    forces the fallback branch (the alternative is exercising it for the first time in
    production, on the one database that most needs it to work).
    """
    store = knowledge_store_admin
    monkeypatch.setattr(KnowledgeStore, "_has_surfaced_column", lambda _self: False)

    first = _active_fact(store, "sepia trellis cache eviction")
    second = _active_fact(store, "sepia trellis cache warmup")
    run_id = "run-t63-unmigrated"

    recall_id = store.record_recall(
        run_id=run_id,
        agent_id=None,
        discipline=None,
        query_digest="fedcba9876543210",
        fact_ids=[first, second],
        tokens=10,
        latency_ms=1.0,
        surfaced_fact_ids=[first],  # accepted and dropped, not an error
    )
    assert recall_id > 0

    store.record_helped(run_id)
    assert _helped(store, first) == 1
    assert _helped(store, second) == 1, "legacy path credits the whole run, as it always did"


def test_recorded_usefulness_reaches_ranking(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    """The whole loop: record_helped writes the count, the next recall reads it.

    This is the assertion that was impossible before T6.3 — helped_count moved and the
    score did not.
    """
    store = knowledge_store_admin
    helped = _active_fact(store, "violet cascade index rebuild")
    control = _active_fact(store, "violet cascade index rebuild policy")
    run_id = "run-t63-loop"

    def _modulation(prompt: str) -> dict[int, float]:
        """score / (rrf * bonus) — the modulation alone.

        Comparing raw scores across two recalls is flaky: ts_rank_cd ties between two very
        similar statements let PostgreSQL hand out ROW_NUMBER in either order, which moves
        the RRF term by ~1% and swamps nothing but adds noise. The modulation is the part
        this change actually touches, so assert on that.
        """
        return {
            item.fact.id: item.score / (item.signals["rrf"] * item.signals["multi_signal_bonus"])
            for item in recall(store, prompt=prompt).facts
        }

    prompt = "violet cascade index rebuild"
    before = _modulation(prompt)
    assert helped in before and control in before

    store.record_recall(
        run_id=run_id,
        agent_id=None,
        discipline=None,
        query_digest="0123456789abcdef",
        fact_ids=[helped, control],
        tokens=10,
        latency_ms=1.0,
        surfaced_fact_ids=[helped],
    )
    for _ in range(4):
        store.record_helped(run_id)
    assert _helped(store, helped) == 4
    assert _helped(store, control) == 0

    after = _modulation(prompt)
    assert after[helped] / before[helped] == pytest.approx(_usefulness(4), rel=1e-3)
    assert after[control] == pytest.approx(before[control], rel=1e-3)
