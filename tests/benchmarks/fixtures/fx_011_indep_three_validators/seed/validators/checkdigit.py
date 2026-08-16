"""Checkdigit helper module."""

from __future__ import annotations


def digits_only(raw: str) -> str:
    """Return only the numeric digits from the input string."""
    return "".join(c for c in raw if c.isdigit())
