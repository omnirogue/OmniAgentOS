"""Visible check for the pager bug. Run: python -m pytest check_pager.py"""

from __future__ import annotations

import pager


def test_page_count_partial_final_page() -> None:
    assert pager.page_count(11, 10) == 2


def test_page_count_exact_fit() -> None:
    assert pager.page_count(20, 10) == 2


def test_page_slice_still_works() -> None:
    assert pager.page_slice([1, 2, 3, 4, 5], 2, 2) == [3, 4]
