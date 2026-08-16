"""Small string helpers."""

from __future__ import annotations


def normalize(s: str) -> str:
    """Collapse leading/trailing whitespace."""
    return s.strip()
