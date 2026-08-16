"""ISBN validator module."""

from __future__ import annotations


def normalize_isbn(raw: str) -> str:
    """Strip ASCII spaces and hyphens, and uppercase the result."""
    cleaned = "".join(c for c in raw if c not in (" ", "-"))
    return cleaned.upper()


def is_valid_isbn10(raw: str) -> bool:
    """Check if normalized raw is a valid ISBN-10."""
    try:
        norm = normalize_isbn(raw)
        if len(norm) != 10:
            return False
        if not all(c.isdigit() for c in norm[:9]):
            return False

        last_char = norm[9]
        if not (last_char.isdigit() or last_char == "X"):
            return False

        total = 0
        for i in range(9):
            total += (10 - i) * int(norm[i])

        last_val = 10 if last_char == "X" else int(last_char)
        total += 1 * last_val

        return total % 11 == 0
    except Exception:
        return False


def is_valid_isbn13(raw: str) -> bool:
    """Check if normalized raw is a valid ISBN-13."""
    try:
        norm = normalize_isbn(raw)
        if len(norm) != 13:
            return False
        if not norm.isdigit():
            return False

        total = 0
        for i, c in enumerate(norm):
            digit = int(c)
            multiplier = 1 if i % 2 == 0 else 3
            total += digit * multiplier

        return total % 10 == 0
    except Exception:
        return False
