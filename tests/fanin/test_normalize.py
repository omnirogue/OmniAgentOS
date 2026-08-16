"""normalize_candidates: schema, hash stability, dedup."""

from __future__ import annotations

import pytest

from omniagentos.fanin import Candidate, content_hash, normalize_candidates


def test_normalize_schema_and_hash_stability() -> None:
    rows = [
        Candidate(id="a", content={"x": 1, "y": 2}, score=0.5),
        {"id": "b", "content": {"y": 2, "x": 1}, "score": 0.9},  # same content, different key order
    ]
    out = normalize_candidates(rows)
    assert len(out) == 1  # dedup by content hash
    assert out[0].id == "a"
    assert out[0].content_hash == content_hash({"x": 1, "y": 2})
    # second call is stable
    again = normalize_candidates(rows)
    assert again[0].content_hash == out[0].content_hash


def test_normalize_keeps_first_on_dup() -> None:
    out = normalize_candidates(
        [
            {"id": "first", "content": "same", "score": 0.1},
            {"id": "second", "content": "same", "score": 0.9},
            {"id": "third", "content": "other", "score": 0.2},
        ]
    )
    assert [c.id for c in out] == ["first", "third"]


def test_normalize_requires_id_and_content() -> None:
    with pytest.raises(ValueError, match="id"):
        normalize_candidates([{"content": "x"}])
    with pytest.raises(ValueError, match="content"):
        normalize_candidates([{"id": "a"}])
    with pytest.raises(ValueError, match="non-empty"):
        normalize_candidates([{"id": "  ", "content": "x"}])


def test_empty_string_content_is_allowed() -> None:
    out = normalize_candidates([{"id": "a", "content": ""}])
    assert len(out) == 1
    assert out[0].content == ""
