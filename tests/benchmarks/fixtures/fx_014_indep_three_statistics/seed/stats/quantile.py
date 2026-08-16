"""Quantile calculation utilities."""

from __future__ import annotations


def sorted_copy(values: list[float]) -> list[float]:
    """Return a sorted copy of the input list of floats without mutating the original list."""
    raise NotImplementedError()


def percentile(values: list[float], p: float) -> float:
    """Compute the percentile using linear interpolation between closest ranks (inclusive method).

    Raises ValueError if values is empty or if p is not in [0, 100].
    """
    raise NotImplementedError()


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """Return the 25th, 50th, and 75th percentiles of the values."""
    raise NotImplementedError()
