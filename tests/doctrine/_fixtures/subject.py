"""Subject under doctrine self-test — empty-denominator rates are unknown.

Real production analogue:
- ``TraceMetrics.tool_error_rate`` / ``recovery_rate`` → ``None`` when denom is 0
- pulse ``loops.acceptance`` / ``reliability.score`` → ``None`` when no settled rows

The deliberate correct behaviour is ``None``. Favourable fakes are ``0.0`` or
``1.0`` (both look "healthy" on a dashboard tile).
"""

from __future__ import annotations


def rate(numerator: float, denominator: float) -> float | None:
    """Return numerator/denominator, or None when the denominator is empty."""
    if denominator == 0:
        return None
    return numerator / denominator


def tool_error_rate(errors: int, calls: int) -> float | None:
    """TraceLab-shaped rate: unknown when there were no tool calls."""
    return rate(float(errors), float(calls))
