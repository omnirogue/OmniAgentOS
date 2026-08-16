"""Histogram binning and rendering utilities."""

from __future__ import annotations


def bin_edges(low: float, high: float, bins: int) -> list[float]:
    """Return bins + 1 evenly spaced edges from low to high inclusive.

    Raises ValueError if bins < 1.
    """
    if bins < 1:
        raise ValueError("Number of bins must be at least 1.")
    if low == high:
        return [low] * (bins + 1)

    step = (high - low) / bins
    return [low + i * step for i in range(bins)] + [high]


def histogram(values: list[float], bins: int) -> list[tuple[float, float, int]]:
    """Compute the histogram of the values partitioned into the specified number of bins.

    Each bin is half-open [lo, hi) except the last, which is closed [lo, hi].
    Returns a list of tuples (lo, hi, count).
    Raises ValueError if values is empty or bins < 1.
    """
    if not values:
        raise ValueError("Input values list cannot be empty.")
    if bins < 1:
        raise ValueError("Number of bins must be at least 1.")

    low = min(values)
    high = max(values)

    edges = bin_edges(low, high, bins)
    counts = [0] * bins

    if low == high:
        counts[0] = len(values)
    else:
        for val in values:
            for i in range(bins):
                lo = edges[i]
                hi = edges[i + 1]
                if i == bins - 1:
                    if lo <= val <= hi:
                        counts[i] += 1
                        break
                else:
                    if lo <= val < hi:
                        counts[i] += 1
                        break

    return [(edges[i], edges[i + 1], counts[i]) for i in range(bins)]


def render(counts: list[tuple[float, float, int]], width: int = 20) -> str:
    """Render the histogram as ASCII text.

    Each line represents a bin: "[<lo:.2f>, <hi:.2f>) <bar>" (or "]" for the last bin),
    where <bar> is '#' repeated round(count / max_count * width) times.
    If counts is empty, returns an empty string.
    """
    if not counts:
        return ""

    max_count = max(cnt for _, _, cnt in counts)
    lines = []

    for i, (lo, hi, cnt) in enumerate(counts):
        closing = "]" if i == len(counts) - 1 else ")"
        if max_count > 0:
            bar_len = round((cnt / max_count) * width)
        else:
            bar_len = 0
        bar = "#" * bar_len
        lines.append(f"[{lo:.2f}, {hi:.2f}{closing} {bar}")

    return "\n".join(lines)
