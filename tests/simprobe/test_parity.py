"""Contract tests for counting even and odd integers."""

from omniagentos.simprobe.parity import even_odd_counts


def test_even_odd_counts_mixed_values() -> None:
    assert even_odd_counts([1, 2, 3, 4, 6]) == (3, 2)


def test_even_odd_counts_all_even_values() -> None:
    assert even_odd_counts([0, 2, 4, 8]) == (4, 0)


def test_even_odd_counts_all_odd_values() -> None:
    assert even_odd_counts([1, 3, 5, 9]) == (0, 4)


def test_even_odd_counts_negative_values() -> None:
    assert even_odd_counts([-5, -4, -3, -2, -1]) == (2, 3)


def test_even_odd_counts_empty_input() -> None:
    assert even_odd_counts([]) == (0, 0)
