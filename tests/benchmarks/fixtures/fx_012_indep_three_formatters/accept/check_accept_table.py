"""
FROZEN acceptance check for fx_012_indep_three_formatters.
This check file is copied over the workspace AFTER the agent runs to prevent tampering.
It verifies the table renderer implementation.
"""

from __future__ import annotations

from fmt.table import render_table


def test_render_table_right_align() -> None:
    # Example 1 from SPEC.md
    headers = ["Name", "Age", "City"]
    rows = [["Alice", "24", "New York"], ["Bob", "300", "Paris"]]
    align = "lrr"
    expected = (
        "Name  | Age |     City\n"
        "----------------------\n"
        "Alice |  24 | New York\n"
        "Bob   | 300 |    Paris"
    )
    result = render_table(headers, rows, align)
    assert result == expected


def test_render_table_left_align() -> None:
    # Example 2 from SPEC.md
    headers = ["Name", "Age", "City"]
    rows = [["Alice", "24", "New York"], ["Bob", "300", "Paris"]]
    align = "lll"
    expected = (
        "Name  | Age | City\n----------------------\nAlice | 24  | New York\nBob   | 300 | Paris"
    )
    result = render_table(headers, rows, align)
    assert result == expected

    # Default should be left-aligned too
    assert render_table(headers, rows, "") == expected


def test_render_table_no_rows() -> None:
    headers = ["Name", "Age"]
    rows = []
    align = "lr"
    expected = "Name | Age\n----------"
    result = render_table(headers, rows, align)
    assert result == expected


def test_render_table_errors() -> None:
    # Empty headers
    try:
        render_table([], [])
        raise AssertionError("Should raise ValueError for empty headers")
    except ValueError:
        pass

    # Row length mismatch
    try:
        render_table(["A", "B"], [["1", "2"], ["1"]])
        raise AssertionError("Should raise ValueError for row length mismatch")
    except ValueError:
        pass

    # Align length mismatch
    try:
        render_table(["A", "B"], [["1", "2"]], "l")
        raise AssertionError("Should raise ValueError for align length mismatch")
    except ValueError:
        pass

    # Align invalid character
    try:
        render_table(["A", "B"], [["1", "2"]], "lx")
        raise AssertionError("Should raise ValueError for invalid align character")
    except ValueError:
        pass
