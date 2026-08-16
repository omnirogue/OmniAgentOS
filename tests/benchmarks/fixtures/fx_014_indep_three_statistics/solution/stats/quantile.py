"""Quantile calculation utilities."""

from __future__ import annotations

import math


def sorted_copy(values: list[float]) -> list[float]:
    """Return a sorted copy of the input list of floats without mutating the original list."""
    return sorted(values)


def percentile(values: list[float], p: float) -> float:
    """Compute the percentile using linear interpolation between closest ranks (inclusive method).

    Raises ValueError if values is empty or if p is not in [0, 100].
    """
    if not values:
        raise ValueError("Input values list cannot be empty.")
    if not (0.0 <= p <= 100.0):
        raise ValueError("Percentile p must be between 0 and 100 inclusive.")

    v = sorted_copy(values)
    n = len(v)
    r = (p / 100.0) * (n - 1)

    idx_floor = math.floor(r)
    idx_ceil = math.ceil(r)

    val_floor = v[idx_floor]
    val_ceil = v[idx_ceil]

    return val_floor + (r - idx_floor) * (val_ceil - val_floor)


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """Return the 25th, 50th, and 75th percentiles of the values."""
    if not values:
        raise ValueError("Input values list cannot be empty.")
    return (
        percentile(values, 25.0),
        percentile(values, 50.0),
        percentile(values, 75.0),
    )
