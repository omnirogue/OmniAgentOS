"""Contract tests for the ``safe_ratio`` helper."""

import pytest

from omniagentos.simprobe import ratio


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (10, 2, 5.0),
        (1, 4, 0.25),
    ],
)
def test_safe_ratio_divides_positive_integers(
    numerator: int, denominator: int, expected: float
) -> None:
    assert ratio.safe_ratio(numerator, denominator) == expected


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (7.5, 2.5, 3.0),
        (1.5, 4.0, 0.375),
    ],
)
def test_safe_ratio_divides_floats(numerator: float, denominator: float, expected: float) -> None:
    assert ratio.safe_ratio(numerator, denominator) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (-10, 2, -5.0),
        (10, -2, -5.0),
        (-10, -2, 5.0),
    ],
)
def test_safe_ratio_divides_negative_numbers(
    numerator: int, denominator: int, expected: float
) -> None:
    assert ratio.safe_ratio(numerator, denominator) == expected


def _assert_zero_denominator_contract() -> None:
    """Assert the exact sentinel required for an undefined ratio."""
    assert ratio.safe_ratio(10, 0) is None


def test_safe_ratio_returns_none_for_zero_denominator() -> None:
    # ``is None`` deliberately excludes 0, 0.0, infinity, and other values.
    # Reaching the assertion also proves that the call did not raise.
    _assert_zero_denominator_contract()


def test_revert_none_on_zero_guard_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the zero-denominator contract catches a reverted guard."""

    # Revert-test / mutation test: replacing the guard with a plausible 0.0
    # fallback must make the same contract assertion used above fail. This
    # documents that ``is None`` is load-bearing rather than incidental.
    def reverted_safe_ratio(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else 0.0

    monkeypatch.setattr(ratio, "safe_ratio", reverted_safe_ratio)

    with pytest.raises(AssertionError):
        _assert_zero_denominator_contract()
