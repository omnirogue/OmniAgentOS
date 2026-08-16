"""Semantic Versioning (SemVer) validator module."""

from __future__ import annotations


def parse_version(raw: str) -> tuple[int, int, int, tuple[str, ...]]:
    """Parse a semantic version string into a tuple."""
    raise NotImplementedError()


def compare_versions(a: str, b: str) -> int:
    """Compare two semantic version strings."""
    raise NotImplementedError()


def latest(versions: list[str]) -> str:
    """Return the latest version from a list."""
    raise NotImplementedError()
