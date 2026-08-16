from __future__ import annotations

import pytest

from omniagentos.lab.eval.scoring import aggregate_case_scores, score_case


def test_exact_match_default_field_is_text() -> None:
    expected = {"value": "42"}
    assert score_case(expected, {"text": "42"}) == {"correct": 1.0, "score": 1.0}
    assert score_case(expected, {"text": "43"}) == {"correct": 0.0, "score": 0.0}


def test_exact_match_can_target_the_json_field() -> None:
    expected = {"value": {"answer": 4}, "field": "json"}
    assert score_case(expected, {"json": {"answer": 4}})["correct"] == 1.0
    assert score_case(expected, {"json": {"answer": 5}})["correct"] == 0.0


def test_contains_match() -> None:
    expected = {"value": "capital of France", "match": "contains"}
    assert score_case(expected, {"text": "Paris is the capital of France."})["correct"] == 1.0
    assert score_case(expected, {"text": "Paris."})["correct"] == 0.0


def test_contains_match_tolerates_missing_field() -> None:
    expected = {"value": "x", "match": "contains"}
    assert score_case(expected, {})["correct"] == 0.0


def test_set_equal_match_ignores_order() -> None:
    expected = {"value": ["b", "a", "c"], "field": "json", "match": "set_equal"}
    assert score_case(expected, {"json": ["a", "b", "c"]})["correct"] == 1.0
    assert score_case(expected, {"json": ["a", "b"]})["correct"] == 0.0


def test_numeric_tol_exact_hit() -> None:
    expected = {"value": 10.0, "field": "json", "match": "numeric_tol", "tol": 0.5}
    result = score_case(expected, {"json": 10.0})
    assert result["correct"] == 1.0
    assert result["score"] == 1.0


def test_numeric_tol_within_tolerance_but_not_zero_distance() -> None:
    expected = {"value": 10.0, "field": "json", "match": "numeric_tol", "tol": 1.0}
    result = score_case(expected, {"json": 10.5})
    assert result["correct"] == 1.0
    assert 0.0 < result["score"] < 1.0


def test_numeric_tol_far_miss_scores_zero_but_does_not_raise() -> None:
    expected = {"value": 10.0, "field": "json", "match": "numeric_tol", "tol": 0.1}
    result = score_case(expected, {"json": 999.0})
    assert result["correct"] == 0.0
    assert result["score"] == 0.0


def test_numeric_tol_non_numeric_output_scores_zero() -> None:
    expected = {"value": 10.0, "field": "json", "match": "numeric_tol", "tol": 0.1}
    result = score_case(expected, {"json": "not a number"})
    assert result == {"correct": 0.0, "score": 0.0}


def test_unknown_match_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown match mode"):
        score_case({"value": "x", "match": "telepathy"}, {"text": "x"})


def test_missing_value_key_raises() -> None:
    with pytest.raises(ValueError, match="value"):
        score_case({"match": "exact"}, {"text": "x"})


def test_aggregate_case_scores_is_equal_weighted_mean() -> None:
    per_case = {
        "c1": {"correct": 1.0, "score": 1.0},
        "c2": {"correct": 0.0, "score": 0.25},
    }
    assert aggregate_case_scores(per_case) == {"correct": 0.5, "score": 0.625}


def test_aggregate_case_scores_empty_input() -> None:
    assert aggregate_case_scores({}) == {}
