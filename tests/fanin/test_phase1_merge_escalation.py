"""Proof tests for flag-gated synthesis escalation and size triggers."""

from __future__ import annotations

import pytest

from omniagentos.fanin import FanInMode, adjudicate
from omniagentos.fanin.adjudicate import (
    SECOND_SYNTHESIS_WORD_THRESHOLD,
    MergeEscalationExhausted,
    get_last_synthesis_evidence,
)

SYNTHESIS_MODE_ENV = "OMNIAGENTOS_FANIN_SYNTHESIS_MODE"
MERGE_BUDGET_ENV = "OMNIAGENTOS_FANIN_MERGE_BUDGET"


def test_unset_and_off_preserve_legacy_rank_order_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "id": "high",
            "content": {"choice": "high-value", "high_only": "retained"},
            "score": 0.9,
        },
        {
            "id": "low",
            "content": {"choice": "low-value", "low_only": "also-retained"},
            "score": 0.2,
        },
    ]
    legacy_content = {
        "choice": "high-value",
        "high_only": "retained",
        "low_only": "also-retained",
    }

    monkeypatch.delenv(SYNTHESIS_MODE_ENV, raising=False)
    unset_result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "off")
    off_result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    assert unset_result == off_result
    assert unset_result.selected[0].content == legacy_content


def test_shadow_records_all_conflict_evidence_without_changing_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "id": "high",
            "content": {"answer": "rank-zero", "agreed": "same"},
            "score": 0.9,
        },
        {
            "id": "low",
            "content": {"answer": "rank-one", "agreed": "same"},
            "score": 0.2,
        },
    ]

    monkeypatch.delenv(MERGE_BUDGET_ENV, raising=False)
    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "off")
    legacy_result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "shadow")
    shadow_result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    assert shadow_result == legacy_result
    evidence_payload = get_last_synthesis_evidence()
    assert evidence_payload is not None
    assert evidence_payload["evidence"]["escalated"] is False
    assert evidence_payload["evidence"]["conflicting_keys"] == ["answer"]

    conflict = evidence_payload["content"]["answer"]
    assert conflict == {
        "primary": "rank-zero",
        "conflict": True,
        "candidates": [
            {
                "worker_id": "high",
                "candidate_id": "high",
                "rank": 0,
                "value": "rank-zero",
            },
            {
                "worker_id": "low",
                "candidate_id": "low",
                "rank": 1,
                "value": "rank-one",
            },
        ],
    }


def test_enforce_preserves_every_conflicting_candidate_with_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "id": "first",
            "content": {"answer": "primary", "agreed": "same"},
            "score": 0.9,
        },
        {
            "id": "second",
            "content": {"answer": "runner-up", "agreed": "same"},
            "score": 0.5,
        },
        {
            "id": "third",
            "content": {"answer": "last", "agreed": "same"},
            "score": 0.1,
        },
    ]

    monkeypatch.delenv(MERGE_BUDGET_ENV, raising=False)
    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "enforce")
    result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    content = result.selected[0].content
    assert content["agreed"] == "same"
    assert content["answer"] == {
        "primary": "primary",
        "conflict": True,
        "candidates": [
            {
                "worker_id": "first",
                "candidate_id": "first",
                "rank": 0,
                "value": "primary",
            },
            {
                "worker_id": "second",
                "candidate_id": "second",
                "rank": 1,
                "value": "runner-up",
            },
            {
                "worker_id": "third",
                "candidate_id": "third",
                "rank": 2,
                "value": "last",
            },
        ],
    }
    assert [item["value"] for item in content["answer"]["candidates"]] == [
        "primary",
        "runner-up",
        "last",
    ]


def test_enforce_raises_when_merge_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {"id": "high", "content": {"first": "a", "second": "b"}, "score": 0.9},
        {"id": "low", "content": {"first": "x", "second": "y"}, "score": 0.2},
    ]

    monkeypatch.delenv(MERGE_BUDGET_ENV, raising=False)
    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "enforce")

    with pytest.raises(MergeEscalationExhausted, match="Merge escalation exhausted") as raised:
        adjudicate(candidates, mode=FanInMode.SYNTHESIZE, merge_budget=1)

    assert raised.value.attempts == 2
    assert "exceeds budget (1)" in raised.value.reason


def test_shadow_marks_exhaustion_and_returns_legacy_dict_not_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {"id": "high", "content": {"first": "a", "second": "b"}, "score": 0.9},
        {"id": "low", "content": {"first": "x", "second": "y"}, "score": 0.2},
    ]

    monkeypatch.setenv(MERGE_BUDGET_ENV, "1")
    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "shadow")
    result = adjudicate(candidates, mode=FanInMode.SYNTHESIZE)

    evidence_payload = get_last_synthesis_evidence()
    assert evidence_payload is not None
    assert evidence_payload["evidence"]["escalated"] is True
    assert evidence_payload["evidence"]["attempts"] == 2
    assert evidence_payload["content"] is None
    assert evidence_payload["result"] is None

    returned_content = result.selected[0].content
    assert isinstance(returned_content, dict)
    assert returned_content == {"first": "a", "second": "b"}
    assert not isinstance(returned_content, str)


def test_second_synthesis_trigger_is_word_size_not_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MERGE_BUDGET_ENV, raising=False)
    monkeypatch.setenv(SYNTHESIS_MODE_ENV, "enforce")

    first_word_count = SECOND_SYNTHESIS_WORD_THRESHOLD // 2
    second_word_count = SECOND_SYNTHESIS_WORD_THRESHOLD - first_word_count
    first_text = "alpha " * first_word_count
    second_text = "beta " * second_word_count
    few_large_candidates = [
        {
            "id": "large-a",
            "content": {"text": first_text},
            "score": 0.9,
        },
        {
            "id": "large-b",
            "content": {"text": second_text},
            "score": 0.8,
        },
    ]
    assert (
        len(first_text.split()) + len(second_text.split())
        == SECOND_SYNTHESIS_WORD_THRESHOLD
    )

    few_large_result = adjudicate(
        few_large_candidates,
        mode=FanInMode.SYNTHESIZE,
    )

    assert few_large_result.evidence["second_synthesis_triggered"] is True
    assert (
        few_large_result.evidence["original_word_count"]
        >= SECOND_SYNTHESIS_WORD_THRESHOLD
    )

    many_small_candidates = [
        {
            "id": f"small-{index}",
            "content": {"text": f"word-{index}"},
            "score": 1.0 - index / 10,
        }
        for index in range(6)
    ]
    assert len(many_small_candidates) * 2 < SECOND_SYNTHESIS_WORD_THRESHOLD

    many_small_result = adjudicate(
        many_small_candidates,
        mode=FanInMode.SYNTHESIZE,
    )

    assert many_small_result.evidence["second_synthesis_triggered"] is False
    assert "original_word_count" not in many_small_result.evidence
