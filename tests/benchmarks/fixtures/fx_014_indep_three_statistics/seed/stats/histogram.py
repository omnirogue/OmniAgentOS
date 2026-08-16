"""Histogram binning and rendering utilities."""

from __future__ import annotations


def bin_edges(low: float, high: float, bins: int) -> list[float]:
    """Return bins + 1 evenly spaced edges from low to high inclusive."""
    raise NotImplementedError()


def histogram(values: list[float], bins: int) -> list[tuple[float, float, int]]:
    """Compute the histogram of the values partitioned into the specified number of bins."""
    raise NotImplementedError()


def render(counts: list[tuple[float, float, int]], width: int = 20) -> str:
    """Render the histogram as ASCII text."""
    raise NotImplementedError()
