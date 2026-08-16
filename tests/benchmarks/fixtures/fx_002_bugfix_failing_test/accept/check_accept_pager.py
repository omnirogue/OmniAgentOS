"""FROZEN acceptance check for fx_002_bugfix_failing_test.

Wider than the visible check on purpose: a fix that special-cases the visible
inputs (``if total_items == 11: return 2``) fails here.
"""

from __future__ import annotations

import pager
import pytest


@pytest.mark.parametrize(
    ("total", "per_page", "expected"),
    [
        (0, 10, 0),
        (1, 10, 1),
        (9, 10, 1),
        (10, 10, 1),
        (11, 10, 2),
        (19, 10, 2),
        (20, 10, 2),
        (21, 10, 3),
        (7, 3, 3),
        (1, 1, 1),
        (100, 7, 15),
    ],
)
def test_page_count_matrix(total: int, per_page: int, expected: int) -> None:
    assert pager.page_count(total, per_page) == expected


def test_page_count_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        pager.page_count(10, 0)
    with pytest.raises(ValueError):
        pager.page_count(10, -1)
    with pytest.raises(ValueError):
        pager.page_count(-1, 10)


def test_page_slice_unchanged() -> None:
    items = [1, 2, 3, 4, 5]
    assert pager.page_slice(items, 1, 2) == [1, 2]
    assert pager.page_slice(items, 3, 2) == [5]
    assert pager.page_slice(items, 4, 2) == []
    with pytest.raises(ValueError):
        pager.page_slice(items, 0, 2)
