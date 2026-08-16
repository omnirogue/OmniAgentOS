"""Decisive tests for the doctrine fixture subject.

These are the *shape* later lanes copy: a behavioural claim with an obvious
counterfeit (return a flattering constant instead of None).
"""

from __future__ import annotations

from tests.doctrine._fixtures.subject import rate, tool_error_rate


def test_empty_denominator_returns_none() -> None:
    """Claim: a rate over an empty denominator is unknown (None), not 0.0."""
    assert rate(0, 0) is None
    assert rate(5, 0) is None
    assert tool_error_rate(0, 0) is None


def test_positive_denominator_computes() -> None:
    assert rate(1, 4) == 0.25
    assert tool_error_rate(3, 4) == 0.75
