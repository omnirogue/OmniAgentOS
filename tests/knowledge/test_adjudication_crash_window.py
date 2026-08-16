"""M-27: the adjudication crash window, against BOTH backends.

Adjudicating a contradiction takes two durable writes: superseding the loser and closing
the CONTRADICTS edge. The edge is the only durable record that the conflict exists, so if
it is closed first and the process dies before the supersede lands, the conflict is erased:
the next run sees no open edge, adjudicates nothing, and reports status=ok while both
contradictory facts stay active forever.

Every test here injects a deterministic failure at exactly one of those two boundaries and
then reruns, asserting the properties that the ordering exists to buy:

  * never status=ok while an open conflict is outstanding (and never with two ACTIVE
    contradictory facts);
  * a failed run leaves work the next run can still find;
  * the rerun does not adjudicate the same pair twice;
  * the winner/loser lineage a crash leaves behind is the lineage an uninterrupted run
    would have left.

The same bodies run against the in-memory store and, when PostgreSQL is available, the
real one — an ordering contract that only holds for one backend is not a contract.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from omniagentos.knowledge.config import require_pg
from omniagentos.knowledge.consolidate import STATUS_FAILED, STATUS_OK, STATUS_PARTIAL, consolidate
from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.embeddings import FakeEmbedding
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore
from omniagentos.knowledge.promotion import gate as make_gate


class _FailOnce:
    """Deterministic injected failure: raise on the nth call, delegate on every other."""

    def __init__(self, real: Callable[..., Any], *, fail_on: int = 1) -> None:
        self._real = real
        self._fail_on = fail_on
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError(f"injected failure on call {self.calls}")
        return self._real(*args, **kwargs)


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Any:
    """The same test body against the in-memory store and the real database."""
    if request.param == "postgres":
        if not require_pg():
            pytest.skip("OMNIAGENTOS_REQUIRE_PG not set; skipping live DB path")
        return request.getfixturevalue("knowledge_store_admin")
    return InMemoryKnowledgeStore(role="knowledge_admin", embedder=FakeEmbedding(dim=1024))


@pytest.fixture
def vault() -> Iterator[str]:
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


def _add_active(store: Any, statement: str, *, trust: float) -> int:
    episode = store.add_episode(
        source="run", content=statement, source_ref=f"crash-{uuid.uuid4().hex[:10]}"
    )
    fact_id = store.add_fact(
        statement=statement,
        episode_id=episode,
        provenance="extracted",
        trust=trust,
        confidence=0.7,
        importance=0.5,
    )
    store.promote_fact(fact_id, make_gate())
    return fact_id


def _contradict(store: Any, src_id: int, dst_id: int) -> None:
    store.add_edge(
        src_kind="fact",
        src_id=src_id,
        dst_kind="fact",
        dst_id=dst_id,
        edge_type="contradicts",
        weight=1.0,
    )


def _seed_conflict(store: Any) -> tuple[int, int]:
    """Two ACTIVE contradictory facts; the higher-trust one must win. (loser, winner)."""
    winner = _add_active(store, "The reactor coolant loop is sealed", trust=0.9)
    loser = _add_active(store, "The reactor coolant loop is open", trust=0.5)
    _contradict(store, loser, winner)
    return loser, winner


def _status(store: Any, fact_id: int) -> str:
    fact = store.get_fact(fact_id)
    assert fact is not None
    return fact.status.value if hasattr(fact.status, "value") else str(fact.status)


def _open_pairs(store: Any) -> set[frozenset[int]]:
    """Contradiction edges still open (invalid_at IS NULL) in either backend."""
    return {frozenset((a, b)) for a, b in store.list_contradicts_edges()}


def _assert_truthful(store: Any, result: dict[str, Any], watched: tuple[int, ...]) -> None:
    """status=ok is a claim that no adjudication work is outstanding. Hold it to that."""
    unresolved = [
        pair
        for pair in _open_pairs(store)
        if all(_status(store, fid) != FactStatus.SUPERSEDED.value for fid in pair)
    ]
    if result["status"] == STATUS_OK:
        assert not unresolved, f"status=ok while conflicts {unresolved} are still open"
    active = [fid for fid in watched if _status(store, fid) == FactStatus.ACTIVE.value]
    for pair in _open_pairs(store):
        both_active = [fid for fid in pair if fid in active]
        if len(both_active) == 2:
            assert result["status"] != STATUS_OK, (
                f"status=ok with two ACTIVE contradictory facts {both_active}"
            )


class TestSupersedeBoundary:
    """Failure BEFORE the loser is superseded must leave the conflict findable."""

    def test_failed_supersede_leaves_the_edge_open_for_the_next_run(
        self, store: Any, vault: str
    ) -> None:
        """The exact M-27 window: run 1 must not durably erase the conflict.

        Under the old ordering the edge was closed first, so this run left an invalidated
        edge and two ACTIVE facts, and the rerun below reported status=ok with the
        contradiction silently gone.
        """
        loser, winner = _seed_conflict(store)
        boom = _FailOnce(store.supersede_fact)
        store.supersede_fact = boom  # type: ignore[method-assign]

        first = consolidate(store, vault, dry_run=False)

        assert boom.calls == 1, "the run must have reached the supersede boundary"
        assert first["status"] in (STATUS_PARTIAL, STATUS_FAILED)
        assert first["retryable"] is True
        assert first["adjudications"] == [], "a failed supersede is not an adjudication"
        assert any(e["phase"] == "adjudication" for e in first["phase_errors"])
        # Nothing durable changed: both facts live, the edge still open.
        assert _status(store, loser) == FactStatus.ACTIVE.value
        assert _status(store, winner) == FactStatus.ACTIVE.value
        assert frozenset((loser, winner)) in _open_pairs(store), (
            "the conflict must survive a failed supersede, or no later run can find it"
        )
        _assert_truthful(store, first, (loser, winner))

        del store.supersede_fact  # type: ignore[attr-defined]
        second = consolidate(store, vault, dry_run=False)

        assert second["status"] == STATUS_OK
        assert [(a, b) for a, b, _ in second["adjudications"]] == [(loser, winner)], (
            "the retry must actually re-adjudicate the pair the failure skipped"
        )
        assert _status(store, loser) == FactStatus.SUPERSEDED.value
        assert store.get_fact(loser).superseded_by == winner
        assert _status(store, winner) == FactStatus.ACTIVE.value
        assert frozenset((loser, winner)) not in _open_pairs(store)
        _assert_truthful(store, second, (loser, winner))


class TestEdgeCloseBoundary:
    """Failure AFTER the loser is superseded is real progress — and must be reported."""

    def test_failed_edge_close_is_partial_and_healed_by_the_next_run(
        self, store: Any, vault: str
    ) -> None:
        loser, winner = _seed_conflict(store)
        boom = _FailOnce(store.resolve_contradiction_edge)
        store.resolve_contradiction_edge = boom  # type: ignore[method-assign]

        first = consolidate(store, vault, dry_run=False)

        assert boom.calls == 1
        assert [(a, b) for a, b, _ in first["adjudications"]] == [(loser, winner)], (
            "the supersede landed, so the adjudication happened and must be reported"
        )
        assert _status(store, loser) == FactStatus.SUPERSEDED.value
        superseded_by = store.get_fact(loser).superseded_by
        assert superseded_by == winner
        assert first["status"] == STATUS_PARTIAL, "an unclosed edge is not a clean run"
        assert first["retryable"] is True
        assert any(
            e["phase"] == "adjudication" and str(loser) in e["error"] for e in first["phase_errors"]
        )
        _assert_truthful(store, first, (loser, winner))

        del store.resolve_contradiction_edge  # type: ignore[attr-defined]
        second = consolidate(store, vault, dry_run=False)

        assert second["adjudications"] == [], "the settled pair must not be adjudicated twice"
        assert _status(store, loser) == FactStatus.SUPERSEDED.value
        assert store.get_fact(loser).superseded_by == superseded_by, "lineage must be stable"
        assert _status(store, winner) == FactStatus.ACTIVE.value
        assert frozenset((loser, winner)) not in _open_pairs(store), (
            "the rerun must finish the interrupted close, not leave the edge open forever"
        )
        assert second["status"] == STATUS_OK
        _assert_truthful(store, second, (loser, winner))

    def test_a_superseded_loser_no_longer_blocks_promotion(self, store: Any, vault: str) -> None:
        """The open edge left by the interrupted run must not wedge the winner.

        Supersede-first is only safe because an unresolved-peer scan ignores superseded
        peers. If it did not, an interrupted adjudication would block promotion of every
        fact still attached to the stranded edge.
        """
        loser, winner = _seed_conflict(store)
        boom = _FailOnce(store.resolve_contradiction_edge)
        store.resolve_contradiction_edge = boom  # type: ignore[method-assign]
        consolidate(store, vault, dry_run=False)
        del store.resolve_contradiction_edge  # type: ignore[attr-defined]

        assert frozenset((loser, winner)) in _open_pairs(store), "edge still open by construction"
        assert store.list_unresolved_contradiction_peers(winner) == []


class TestDiamondUnderInterruption:
    """A three-way conflict must settle to the same lineage whether or not a run dies."""

    def _seed_diamond(self, store: Any) -> tuple[int, int, int]:
        a = _add_active(store, "Alpha describes the coolant loop", trust=0.9)
        b = _add_active(store, "Beta contradicts the coolant loop", trust=0.5)
        c = _add_active(store, "Gamma disputes the coolant loop", trust=0.3)
        for src, dst in ((a, b), (a, c), (b, c)):
            _contradict(store, src, dst)
        return a, b, c

    def test_uninterrupted_diamond_collapses_to_one_winner(self, store: Any, vault: str) -> None:
        a, b, c = self._seed_diamond(store)

        result = consolidate(store, vault, dry_run=False)

        assert _status(store, a) == FactStatus.ACTIVE.value
        assert _status(store, b) == FactStatus.SUPERSEDED.value
        assert _status(store, c) == FactStatus.SUPERSEDED.value
        # Every loser points at the fact that actually survived, not at another loser.
        assert store.get_fact(b).superseded_by == a
        assert store.get_fact(c).superseded_by == a
        assert result["status"] == STATUS_OK
        assert _open_pairs(store) == set(), "a settled diamond leaves no open conflict"
        _assert_truthful(store, result, (a, b, c))

    def test_diamond_interrupted_mid_loop_resumes_to_the_same_lineage(
        self, store: Any, vault: str
    ) -> None:
        a, b, c = self._seed_diamond(store)
        # Die on the SECOND supersede: the first pair is durable, the rest is not.
        boom = _FailOnce(store.supersede_fact, fail_on=2)
        store.supersede_fact = boom  # type: ignore[method-assign]

        first = consolidate(store, vault, dry_run=False)

        assert boom.calls >= 2
        assert first["status"] == STATUS_PARTIAL
        assert first["retryable"] is True
        assert [(loser, winner) for loser, winner, _ in first["adjudications"]] == [(b, a)]
        _assert_truthful(store, first, (a, b, c))

        del store.supersede_fact  # type: ignore[attr-defined]
        second = consolidate(store, vault, dry_run=False)

        assert _status(store, a) == FactStatus.ACTIVE.value
        assert _status(store, b) == FactStatus.SUPERSEDED.value
        assert _status(store, c) == FactStatus.SUPERSEDED.value
        assert store.get_fact(b).superseded_by == a
        assert store.get_fact(c).superseded_by == a, (
            "the interrupted run must converge on the same winner as an uninterrupted one"
        )
        assert second["status"] == STATUS_OK
        assert _open_pairs(store) == set()
        _assert_truthful(store, second, (a, b, c))

        third = consolidate(store, vault, dry_run=False)
        assert third["adjudications"] == [], "a settled diamond must never be re-adjudicated"
        assert third["status"] == STATUS_OK


class TestDryRunWritesNothing:
    """The settled-pair sweep must stay on the write side of dry_run."""

    def test_dry_run_neither_supersedes_nor_closes_edges(self, store: Any, vault: str) -> None:
        loser, winner = _seed_conflict(store)

        result = consolidate(store, vault, dry_run=True)

        assert [(a, b) for a, b, _ in result["adjudications"]] == [(loser, winner)]
        assert _status(store, loser) == FactStatus.ACTIVE.value
        assert _status(store, winner) == FactStatus.ACTIVE.value
        assert frozenset((loser, winner)) in _open_pairs(store)


class TestNonSortedEdgeInsertionParity:
    """Non-sorted edge insertion must yield identical durable lineage in memory and PG."""

    def test_non_sorted_edge_insertion_parity(self, store: Any, vault: str) -> None:
        """Inserting (B, C) then (A, B) must process (A, B) first on both backends.

        When edges are inserted in non-sorted order (B, C) then (A, B):
        - Sorted (src_id, dst_id) iteration processes (A, B) first -> A (trust=0.9) wins, B (trust=0.7) superseded by A.
        - Then (B, C) is checked -> B is already superseded, so (B, C) is settled and C (trust=0.5) stays ACTIVE.
        - Unsorted iteration (prior memory store bug) processed (B, C) first -> B wins over C, C superseded by B,
          then (A, B) -> A wins over B, B superseded by A, leaving C incorrectly superseded.
        """
        a = _add_active(store, "Fact A statement", trust=0.9)
        b = _add_active(store, "Fact B statement", trust=0.7)
        c = _add_active(store, "Fact C statement", trust=0.5)
        assert a < b < c

        # Insert edges in non-sorted order: (b, c) then (a, b)
        _contradict(store, b, c)
        _contradict(store, a, b)

        result = consolidate(store, vault, dry_run=False)

        assert result["status"] == STATUS_OK
        assert _status(store, a) == FactStatus.ACTIVE.value
        assert _status(store, b) == FactStatus.SUPERSEDED.value
        assert store.get_fact(b).superseded_by == a
        assert _status(store, c) == FactStatus.ACTIVE.value, (
            "Fact C must remain ACTIVE on both backends regardless of edge insertion order"
        )
        assert _open_pairs(store) == set()
        _assert_truthful(store, result, (a, b, c))
