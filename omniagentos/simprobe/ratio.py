"""Ratio helpers."""


def safe_ratio(numerator: float, denominator: float) -> float | None:
    """Return the quotient, or ``None`` when the denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator
