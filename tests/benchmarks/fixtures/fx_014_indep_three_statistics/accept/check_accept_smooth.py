"""FROZEN acceptance check for fx_014_indep_three_statistics.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

import math
import os

from stats.smooth import deltas, exponential_moving_average, moving_average


def read_series() -> list[float]:
    path = os.path.join("data", "series.txt")
    with open(path, encoding="utf-8") as f:
        return [float(line.strip()) for line in f if line.strip()]


def test_moving_average_basic() -> None:
    ma = moving_average([1.0, 2.0, 3.0, 4.0], 2)
    assert len(ma) == 3
    assert math.isclose(ma[0], 1.5, rel_tol=1e-9)
    assert math.isclose(ma[1], 2.5, rel_tol=1e-9)
    assert math.isclose(ma[2], 3.5, rel_tol=1e-9)

    ma_one = moving_average([10.0], 1)
    assert len(ma_one) == 1
    assert math.isclose(ma_one[0], 10.0, rel_tol=1e-9)


def test_moving_average_errors() -> None:
    for bad_w in [0, -2]:
        try:
            moving_average([1.0, 2.0], bad_w)
            raise AssertionError(f"Should raise ValueError for window={bad_w}")
        except ValueError:
            pass

    try:
        moving_average([1.0, 2.0], 3)
        raise AssertionError("Should raise ValueError for window > length")
    except ValueError:
        pass


def test_exponential_moving_average_basic() -> None:
    # values: [10.0, 20.0, 30.0], alpha = 0.5
    # ema[0] = 10.0
    # ema[1] = 0.5*20.0 + 0.5*10.0 = 15.0
    # ema[2] = 0.5*30.0 + 0.5*15.0 = 22.5
    ema = exponential_moving_average([10.0, 20.0, 30.0], 0.5)
    assert len(ema) == 3
    assert math.isclose(ema[0], 10.0, rel_tol=1e-9)
    assert math.isclose(ema[1], 15.0, rel_tol=1e-9)
    assert math.isclose(ema[2], 22.5, rel_tol=1e-9)

    # Empty values returns empty list
    assert exponential_moving_average([], 0.5) == []


def test_exponential_moving_average_errors() -> None:
    for bad_alpha in [0.0, 1.1, -0.5, 2.0]:
        try:
            exponential_moving_average([1.0, 2.0], bad_alpha)
            raise AssertionError(f"Should raise ValueError for alpha={bad_alpha}")
        except ValueError:
            pass


def test_deltas_basic() -> None:
    res_deltas = deltas([10.0, 12.0, 15.0])
    assert len(res_deltas) == 2
    for got, want in zip(res_deltas, [2.0, 3.0], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)
    assert deltas([5.0]) == []
    assert deltas([]) == []


def test_loaded_series() -> None:
    values = read_series()
    # values[:5] = [12.5, 24.0, 5.5, 18.2, 30.1]

    # moving_average window = 3
    ma = moving_average(values[:5], 3)
    assert len(ma) == 3
    # (12.5 + 24.0 + 5.5) / 3 = 14.0
    assert math.isclose(ma[0], 14.0, rel_tol=1e-9)
    # (24.0 + 5.5 + 18.2) / 3 = 15.9
    assert math.isclose(ma[1], 15.9, rel_tol=1e-9)
    # (5.5 + 18.2 + 30.1) / 3 = 17.933333333333334
    assert math.isclose(ma[2], 17.933333333333334, rel_tol=1e-9)

    # exponential_moving_average alpha = 0.1
    ema = exponential_moving_average(values[:3], 0.1)
    assert len(ema) == 3
    assert math.isclose(ema[0], 12.5, rel_tol=1e-9)
    # 0.1 * 24.0 + 0.9 * 12.5 = 2.4 + 11.25 = 13.65
    assert math.isclose(ema[1], 13.65, rel_tol=1e-9)
    # 0.1 * 5.5 + 0.9 * 13.65 = 0.55 + 12.285 = 12.835
    assert math.isclose(ema[2], 12.835, rel_tol=1e-9)

    # deltas
    diffs = deltas(values[:5])
    assert len(diffs) == 4
    for got, want in zip(diffs, [11.5, -18.5, 12.7, 11.9], strict=True):
        assert math.isclose(got, want, rel_tol=1e-9)
