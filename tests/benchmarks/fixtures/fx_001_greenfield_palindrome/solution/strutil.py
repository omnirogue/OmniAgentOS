"""Reference solution — proves the fixture is passable and that acceptance
discriminates. Never copied into an arm's workspace (only ``seed/`` is)."""

from __future__ import annotations


def normalize(s: str) -> str:
    """Collapse leading/trailing whitespace."""
    return s.strip()


def is_palindrome(s: str) -> bool:
    """True when ``s`` mirrors itself, ignoring spaces and case."""
    squeezed = s.replace(" ", "").lower()
    return squeezed == squeezed[::-1]
