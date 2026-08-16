"""ISBN validator module."""

from __future__ import annotations


def normalize_isbn(raw: str) -> str:
    """Strip ASCII spaces and hyphens, and uppercase the result."""
    raise NotImplementedError()


def is_valid_isbn10(raw: str) -> bool:
    """Check if normalized raw is a valid ISBN-10."""
    raise NotImplementedError()


def is_valid_isbn13(raw: str) -> bool:
    """Check if normalized raw is a valid ISBN-13."""
    raise NotImplementedError()
