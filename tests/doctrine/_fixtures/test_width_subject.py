"""Decisive + chain fixtures for the width/counterfeit worked example."""

from __future__ import annotations

from tests.doctrine._fixtures.width_subject import worker_count


def test_wide_body_is_parallel() -> None:
    """Single root then five independent siblings ⇒ width >= 2 (really 5)."""
    nodes = ["root", "a", "b", "c", "d", "e"]
    edges = [("root", "a"), ("root", "b"), ("root", "c"), ("root", "d"), ("root", "e")]
    assert worker_count(edges, nodes) >= 2
    assert worker_count(edges, nodes) == 5


def test_chain_stays_sequential() -> None:
    """Strict chain must stay width 1 — catches worker_count = max(..., 2)."""
    nodes = ["t1", "t2", "t3", "t4"]
    edges = [("t1", "t2"), ("t2", "t3"), ("t3", "t4")]
    assert worker_count(edges, nodes) == 1
