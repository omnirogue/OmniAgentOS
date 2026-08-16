"""FROZEN acceptance check for fx_014_indep_three_statistics.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

import math
import os

from stats.quantile import percentile, quartiles, sorted_copy


def read_series() -> list[float]:
    path = os.path.join("data", "series.txt")
    with open(path, encoding="utf-8") as f:
        return [float(line.strip()) for line in f if line.strip()]


def test_sorted_copy_basic() -> None:
    values = [4.5, 1.2, 9.8, 3.4]
    res = sorted_copy(values)
    assert len(res) == 4
    for got, want in zip(res, [1.2, 3.4, 4.5, 9.8], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)
    assert len(values) == 4
    for got, want in zip(values, [4.5, 1.2, 9.8, 3.4], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)


def test_percentile_worked_examples() -> None:
    # Worked examples from the prompt
    assert math.isclose(percentile([1.0, 2.0, 3.0, 4.0], 50.0), 2.5, rel_tol=1e-9)
    assert math.isclose(percentile([1.0, 2.0, 3.0, 4.0], 0.0), 1.0, rel_tol=1e-9)
    assert math.isclose(percentile([1.0, 2.0, 3.0, 4.0], 100.0), 4.0, rel_tol=1e-9)


def test_percentile_interpolation_and_non_mutation() -> None:
    v = [1.0, 2.0, 3.0, 4.0]
    # p = 25. r = 0.25 * 3 = 0.75. val = 1 + 0.75 * 1 = 1.75
    assert math.isclose(percentile(v, 25.0), 1.75, rel_tol=1e-9)
    # p = 75. r = 0.75 * 3 = 2.25. val = 3 + 0.25 * 1 = 3.25
    assert math.isclose(percentile(v, 75.0), 3.25, rel_tol=1e-9)
    # Original list check
    assert len(v) == 4
    for got, want in zip(v, [1.0, 2.0, 3.0, 4.0], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)


def test_percentile_errors() -> None:
    # empty list
    try:
        percentile([], 50.0)
        raise AssertionError("Should raise ValueError for empty list")
    except ValueError:
        pass

    # p out of bounds
    for bad_p in [-0.1, 100.1, -10.0, 150.0]:
        try:
            percentile([1.0, 2.0], bad_p)
            raise AssertionError(f"Should raise ValueError for p={bad_p}")
        except ValueError:
            pass


def test_quartiles_basic() -> None:
    v = [1.0, 2.0, 3.0, 4.0]
    q = quartiles(v)
    assert len(q) == 3
    assert math.isclose(q[0], 1.75, rel_tol=1e-9)
    assert math.isclose(q[1], 2.5, rel_tol=1e-9)
    assert math.isclose(q[2], 3.25, rel_tol=1e-9)
    assert len(v) == 4
    for got, want in zip(v, [1.0, 2.0, 3.0, 4.0], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)


def test_quartiles_errors() -> None:
    try:
        quartiles([])
        raise AssertionError("Should raise ValueError for empty list")
    except ValueError:
        pass


def test_loaded_series() -> None:
    values = read_series()
    # Expected hand-computed quantiles/percentiles
    # sorted: [5.5, 6.2, 8.9, 9.5, 11.0, 12.5, 13.7, 14.4, 15.0, 16.5, 17.8, 18.2, 19.0, 20.0, 21.3, 22.1, 24.0, 25.4, 28.6, 30.1]
    # n = 20, n-1 = 19
    # p = 25 -> r = 4.75 -> 11.0 + 0.75 * 1.5 = 12.125
    # p = 50 -> r = 9.5 -> 16.5 + 0.5 * 1.3 = 17.15
    # p = 75 -> r = 14.25 -> 21.3 + 0.25 * 0.8 = 21.5
    # p = 90 -> r = 17.1 -> 25.4 + 0.1 * 3.2 = 25.72
    q = quartiles(values)
    assert math.isclose(q[0], 12.125, rel_tol=1e-9)
    assert math.isclose(q[1], 17.15, rel_tol=1e-9)
    assert math.isclose(q[2], 21.5, rel_tol=1e-9)
    assert math.isclose(percentile(values, 90.0), 25.72, rel_tol=1e-9)
