"""Utilities for constraining values to inclusive bounds."""


def clamp[T: (int, float)](value: T, lo: T, hi: T) -> T:
    """Return *value* constrained to the inclusive range [*lo*, *hi*].

    Raises:
        ValueError: If the lower bound is greater than the upper bound.
    """
    if lo > hi:
        # Revert-test evidence: a silent bound swap made the reversed-bounds
        # contract test fail because it did not raise ValueError.
        raise ValueError(f"lower bound {lo!r} must not exceed upper bound {hi!r}")

    return min(max(value, lo), hi)
