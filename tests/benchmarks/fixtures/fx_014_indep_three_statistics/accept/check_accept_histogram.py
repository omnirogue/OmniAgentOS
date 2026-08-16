"""FROZEN acceptance check for fx_014_indep_three_statistics.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

import math
import os

from stats.histogram import bin_edges, histogram, render


def read_series() -> list[float]:
    path = os.path.join("data", "series.txt")
    with open(path, encoding="utf-8") as f:
        return [float(line.strip()) for line in f if line.strip()]


def test_bin_edges_basic() -> None:
    # 5 bins from 10.0 to 20.0 should have 6 edges: [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
    edges = bin_edges(10.0, 20.0, 5)
    assert len(edges) == 6
    for actual, expected in zip(edges, [10.0, 12.0, 14.0, 16.0, 18.0, 20.0], strict=True):
        assert math.isclose(actual, expected, rel_tol=1e-9)

    # Identical bounds low == high
    edges_ident = bin_edges(5.0, 5.0, 3)
    assert len(edges_ident) == 4
    for got, want in zip(edges_ident, [5.0, 5.0, 5.0, 5.0], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)


def test_bin_edges_errors() -> None:
    for bad_bin in [0, -1, -5]:
        try:
            bin_edges(1.0, 10.0, bad_bin)
            raise AssertionError(f"Should raise ValueError for bins={bad_bin}")
        except ValueError:
            pass


def test_histogram_basic() -> None:
    # 2 bins from 1.0 to 4.0: [1.0, 2.5, 4.0]
    # [1.0, 2.0, 3.0, 4.0]
    # Bin 0: [1.0, 2.5) -> 1.0, 2.0 (count 2)
    # Bin 1: [2.5, 4.0] -> 3.0, 4.0 (count 2)
    hist = histogram([1.0, 2.0, 3.0, 4.0], 2)
    assert len(hist) == 2
    assert math.isclose(hist[0][0], 1.0, rel_tol=1e-9)
    assert math.isclose(hist[0][1], 2.5, rel_tol=1e-9)
    assert hist[0][2] == 2

    assert math.isclose(hist[1][0], 2.5, rel_tol=1e-9)
    assert math.isclose(hist[1][1], 4.0, rel_tol=1e-9)
    assert hist[1][2] == 2


def test_histogram_identical_values() -> None:
    # low == high
    # All must fall into the first bin
    hist = histogram([5.0, 5.0, 5.0], 3)
    assert len(hist) == 3
    assert math.isclose(hist[0][0], 5.0, rel_tol=1e-9)
    assert math.isclose(hist[0][1], 5.0, rel_tol=1e-9)
    assert hist[0][2] == 3

    assert math.isclose(hist[1][0], 5.0, rel_tol=1e-9)
    assert math.isclose(hist[1][1], 5.0, rel_tol=1e-9)
    assert hist[1][2] == 0

    assert math.isclose(hist[2][0], 5.0, rel_tol=1e-9)
    assert math.isclose(hist[2][1], 5.0, rel_tol=1e-9)
    assert hist[2][2] == 0


def test_histogram_errors() -> None:
    # empty list
    try:
        histogram([], 3)
        raise AssertionError("Should raise ValueError for empty list")
    except ValueError:
        pass

    # bins < 1
    try:
        histogram([1.0, 2.0], 0)
        raise AssertionError("Should raise ValueError for bins < 1")
    except ValueError:
        pass


def test_render_basic() -> None:
    counts = [(1.0, 2.5, 2), (2.5, 4.0, 2)]
    # width = 10, max_count = 2 -> round(2/2 * 10) = 10
    expected = "[1.00, 2.50) ##########\n[2.50, 4.00] ##########"
    assert render(counts, 10) == expected

    # empty list
    assert render([]) == ""

    # max_count = 0
    counts_zero = [(5.0, 5.0, 0)]
    assert render(counts_zero, 5) == "[5.00, 5.00] "


def test_loaded_series() -> None:
    values = read_series()
    # sorted: [5.5, 6.2, 8.9, 9.5, 11.0, 12.5, 13.7, 14.4, 15.0, 16.5, 17.8, 18.2, 19.0, 20.0, 21.3, 22.1, 24.0, 25.4, 28.6, 30.1]
    # min = 5.5, max = 30.1
    # bins = 4. step = 6.15
    # edges = [5.5, 11.65, 17.8, 23.95, 30.1]
    # bin 0 [5.5, 11.65): 5
    # bin 1 [11.65, 17.8): 5
    # bin 2 [17.8, 23.95): 6
    # bin 3 [23.95, 30.1]: 4
    hist = histogram(values, 4)
    assert len(hist) == 4
    assert math.isclose(hist[0][0], 5.5, rel_tol=1e-9)
    assert math.isclose(hist[0][1], 11.65, rel_tol=1e-9)
    assert hist[0][2] == 5

    assert math.isclose(hist[1][0], 11.65, rel_tol=1e-9)
    assert math.isclose(hist[1][1], 17.8, rel_tol=1e-9)
    assert hist[1][2] == 5

    assert math.isclose(hist[2][0], 17.8, rel_tol=1e-9)
    assert math.isclose(hist[2][1], 23.95, rel_tol=1e-9)
    assert hist[2][2] == 6

    assert math.isclose(hist[3][0], 23.95, rel_tol=1e-9)
    assert math.isclose(hist[3][1], 30.1, rel_tol=1e-9)
    assert hist[3][2] == 4

    # test render for this histogram
    # max_count = 6. width = 12
    # bin 0: round(5/6 * 12) = 10
    # bin 1: round(5/6 * 12) = 10
    # bin 2: round(6/6 * 12) = 12
    # bin 3: round(4/6 * 12) = 8
    expected_render = (
        "[5.50, 11.65) ##########\n"
        "[11.65, 17.80) ##########\n"
        "[17.80, 23.95) ############\n"
        "[23.95, 30.10] ########"
    )
    assert render(hist, 12) == expected_render
