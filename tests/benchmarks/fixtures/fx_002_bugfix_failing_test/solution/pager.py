"""Reference solution — see fx_001's solution/ for why these exist."""

from __future__ import annotations

from collections.abc import Sequence


def page_count(total_items: int, per_page: int) -> int:
    """Number of pages needed to hold ``total_items`` items."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total_items < 0:
        raise ValueError("total_items must not be negative")
    return -(-total_items // per_page)


def page_slice[T](items: Sequence[T], page: int, per_page: int) -> list[T]:
    """The 1-indexed ``page`` of ``items``."""
    if page < 1:
        raise ValueError("page is 1-indexed")
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    start = (page - 1) * per_page
    return list(items[start : start + per_page])
