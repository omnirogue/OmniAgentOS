"""adjudicate: modes, score preference, determinism."""

from __future__ import annotations

from omniagentos.fanin import FanInMode, adjudicate


def test_select_best_prefers_score_not_majority() -> None:
    # Two identical low-score "facts" vs one high-score different fact.
    result = adjudicate(
        [
            {"id": "m1", "content": {"answer": 1}, "score": 0.2},
            {"id": "m2", "content": {"answer": 1}, "score": 0.2},  # deduped
            {"id": "best", "content": {"answer": 2}, "score": 0.95},
        ],
        mode=FanInMode.SELECT_BEST,
    )
    assert result.decision == "selected"
    assert result.selected[0].id == "best"
    assert result.selected[0].content == {"answer": 2}


def test_tie_break_is_deterministic() -> None:
    r1 = adjudicate(
        [
            {"id": "a", "content": "A", "score": 0.5, "confidence": 0.4},
            {"id": "b", "content": "B", "score": 0.5, "confidence": 0.9},
        ],
        mode=FanInMode.SELECT_BEST,
    )
    r2 = adjudicate(
        [
            {"id": "b", "content": "B", "score": 0.5, "confidence": 0.9},
            {"id": "a", "content": "A", "score": 0.5, "confidence": 0.4},
        ],
        mode=FanInMode.SELECT_BEST,
    )
    assert r1.selected[0].id == r2.selected[0].id == "b"


def test_union_returns_all_unique() -> None:
    result = adjudicate(
        [
            {"id": "a", "content": 1},
            {"id": "b", "content": 2},
            {"id": "a2", "content": 1},
        ],
        mode=FanInMode.UNION,
    )
    assert result.decision == "union"
    assert len(result.selected) == 2
    assert result.rejected == ()


def test_synthesize_merges_dicts_high_score_wins() -> None:
    result = adjudicate(
        [
            {"id": "low", "content": {"a": 1, "b": 1}, "score": 0.1},
            {"id": "high", "content": {"b": 9, "c": 3}, "score": 0.9},
        ],
        mode=FanInMode.SYNTHESIZE,
    )
    assert result.decision == "synthesize"
    assert result.selected[0].content == {"a": 1, "b": 9, "c": 3}
    assert result.selected[0].id.startswith("synth:")


def test_synthesize_fallback_when_not_dicts() -> None:
    result = adjudicate(
        [
            {"id": "a", "content": "text-a", "score": 0.2},
            {"id": "b", "content": "text-b", "score": 0.8},
        ],
        mode=FanInMode.SYNTHESIZE,
    )
    assert result.decision == "synthesize_fallback_select"
    assert result.selected[0].id == "b"


def test_integrate_orders_by_score() -> None:
    result = adjudicate(
        [
            {"id": "slow", "content": {"part": 1}, "score": 0.2},
            {"id": "fast", "content": {"part": 2}, "score": 0.9},
        ],
        mode=FanInMode.INTEGRATE,
    )
    assert result.decision == "integrate"
    assert result.evidence["integration_order"][0] == "fast"
    assert len(result.selected) == 2


def test_reject_replan_mode() -> None:
    result = adjudicate(
        [{"id": "a", "content": "x", "score": 1.0}],
        mode=FanInMode.REJECT_REPLAN,
    )
    assert result.decision == "reject_replan"
    assert result.needs_replan is True
    assert result.selected == ()
    assert len(result.rejected) == 1


def test_adjudicate_min_score_reject() -> None:
    result = adjudicate(
        [{"id": "a", "content": "z", "score": 0.1}],
        criteria={"min_score": 0.5},
        mode=FanInMode.ADJUDICATE,
    )
    assert result.decision == "reject_replan"
    assert result.needs_replan is True


def test_adjudicate_require_agreement_without_clear_winner() -> None:
    result = adjudicate(
        [
            {"id": "a", "content": {"v": 1}, "score": 0.5},
            {"id": "b", "content": {"v": 2}, "score": 0.5},
        ],
        criteria={"require_agreement": True},
        mode=FanInMode.ADJUDICATE,
    )
    assert result.needs_replan is True
    assert result.decision == "reject_replan"


def test_adjudicate_require_agreement_with_score_winner() -> None:
    result = adjudicate(
        [
            {"id": "a", "content": {"v": 1}, "score": 0.4},
            {"id": "b", "content": {"v": 2}, "score": 0.9},
        ],
        criteria={"require_agreement": True},
        mode=FanInMode.ADJUDICATE,
    )
    assert result.decision == "adjudicated"
    assert result.selected[0].id == "b"


def test_empty_input() -> None:
    result = adjudicate([], mode=FanInMode.SELECT_BEST)
    assert result.decision == "no_candidates"
    assert result.selected == ()
    empty_adj = adjudicate([], mode=FanInMode.ADJUDICATE)
    assert empty_adj.needs_replan is True
