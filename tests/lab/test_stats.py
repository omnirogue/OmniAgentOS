from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.lab.stats import (
    exact_binomial_test,
    mcnemar_test,
    mean_confidence_interval,
    minimum_detectable_effect,
    sign_test,
    statistical_power,
    wilson_confidence_interval,
)


def test_exact_binomial_test_matches_hand_computable_two_sided_tail() -> None:
    # Two tails at 0 or 10 successes: 2 / 2**10.
    assert exact_binomial_test(10, 10) == pytest.approx(2 / 2**10)
    assert exact_binomial_test(5, 10) == pytest.approx(1.0)


def test_exact_binomial_test_supports_directional_alternatives() -> None:
    assert exact_binomial_test(0, 4, alternative="less") == pytest.approx(1 / 16)
    assert exact_binomial_test(4, 4, alternative="greater") == pytest.approx(1 / 16)
    assert exact_binomial_test(0, 4, alternative="greater") == pytest.approx(1.0)
    assert exact_binomial_test(1, 4, probability=0.0) == 0.0


def test_sign_test_excludes_ties_and_accepts_count_form() -> None:
    observations = [4.0, 2.0, 0.0, -3.0, 0.0, 8.0]
    assert sign_test(observations) == pytest.approx(sign_test(3, 1))
    assert sign_test(observations, alternative="greater") == pytest.approx(5 / 16)
    assert sign_test([0.0, 0.0]) == 1.0


def test_mcnemar_exact_and_corrected_large_sample_forms() -> None:
    assert mcnemar_test(1, 9) == pytest.approx(22 / 2**10)
    assert mcnemar_test(0, 0) == 1.0
    assert mcnemar_test(1, 9, exact=False) == pytest.approx(0.0268566955)


def test_wilson_confidence_interval_matches_reference_value() -> None:
    lower, upper = wilson_confidence_interval(5, 10)
    assert lower == pytest.approx(0.2365930905)
    assert upper == pytest.approx(0.7634069095)


def test_wilson_confidence_interval_accepts_aggregated_soft_scores() -> None:
    lower, upper = wilson_confidence_interval(1.5, 2)
    assert 0.0 <= lower < 0.75 < upper <= 1.0


@pytest.mark.parametrize("observations", [[], [0.75]])
def test_mean_interval_reports_fewer_than_two_observations_as_unstable(
    observations: list[float],
) -> None:
    estimate = mean_confidence_interval(observations)
    assert estimate.bounds is None
    assert estimate.as_list() is None
    assert estimate.stable is False
    assert estimate.reason == "fewer_than_two_observations"
    assert estimate.observations == len(observations)


def test_mean_interval_is_stable_with_estimable_variance() -> None:
    estimate = mean_confidence_interval([0.2, 0.4, 0.6])
    assert estimate.stable is True
    assert estimate.reason is None
    assert estimate.bounds is not None
    assert estimate.bounds[0] < 0.4 < estimate.bounds[1]


def test_mde_and_power_are_consistent_and_scale_with_sample_size() -> None:
    effect = minimum_detectable_effect(100, baseline_rate=0.5)
    assert effect == pytest.approx(0.198101, rel=1e-5)
    assert statistical_power(effect, 100, baseline_rate=0.5) == pytest.approx(0.8, rel=1e-4)
    assert minimum_detectable_effect(400) == pytest.approx(effect / 2.0)
    assert statistical_power(effect, 400) > statistical_power(effect, 100)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: exact_binomial_test(3, 2), "cannot exceed"),
        (lambda: wilson_confidence_interval(0, 0), "positive"),
        (lambda: minimum_detectable_effect(0), "positive"),
        (lambda: statistical_power(float("nan"), 10), "finite"),
        (lambda: mean_confidence_interval([float("inf"), 1.0]), "finite"),
    ],
)
def test_invalid_statistical_inputs_are_rejected(
    call: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        call()
