"""Contract tests for the strict ``clamp`` helper."""

import pytest

from omniagentos.simprobe import clamp


def test_clamp_returns_in_range_value() -> None:
    assert clamp.clamp(5, 1, 10) == 5


def test_clamp_clamps_value_below_lower_bound() -> None:
    assert clamp.clamp(-3, 0, 10) == 0


def test_clamp_clamps_value_above_upper_bound() -> None:
    assert clamp.clamp(15, 0, 10) == 10


@pytest.mark.parametrize(
    ("value", "lo", "hi", "expected"),
    [
        (0, 0, 10, 0),
        (10, 0, 10, 10),
        (5, 5, 5, 5),
    ],
    ids=["value-equals-lo", "value-equals-hi", "lo-equals-hi"],
)
def test_clamp_boundary_equal_cases(value: int, lo: int, hi: int, expected: int) -> None:
    assert clamp.clamp(value, lo, hi) == expected


def test_clamp_raises_value_error_when_lower_bound_exceeds_upper_bound() -> None:
    with pytest.raises(ValueError):
        clamp.clamp(5, 10, 0)
