"""Dependency-free statistical primitives for lab experiment analysis.

The lab uses small, auditable experiments, so these helpers deliberately keep
their assumptions visible. Exact tests are used for discrete paired outcomes;
normal approximations are reserved for confidence intervals and prospective
sample-size planning.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Alternative = Literal["two-sided", "less", "greater"]

_MIN_STABLE_OBSERVATIONS = 2


@dataclass(frozen=True, slots=True)
class IntervalEstimate:
    """A confidence interval plus whether there was enough data to estimate it.

    ``bounds`` is absent, rather than collapsed to ``[mean, mean]``, when fewer
    than two observations are available. Callers can therefore distinguish an
    explicitly unstable estimate from a precise zero-width interval.
    """

    bounds: tuple[float, float] | None
    observations: int
    stable: bool
    reason: str | None = None

    def as_list(self) -> list[float] | None:
        """Return JSON-compatible bounds while preserving ``None`` for unstable data."""
        if self.bounds is None:
            return None
        return [self.bounds[0], self.bounds[1]]


def _validate_probability(value: float, name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_confidence(confidence: float) -> None:
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be finite and strictly between 0 and 1")


def _validate_count(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_alternative(alternative: Alternative) -> None:
    if alternative not in {"two-sided", "less", "greater"}:
        raise ValueError("alternative must be 'two-sided', 'less', or 'greater'")


def _log_binomial_pmf(successes: int, trials: int, probability: float) -> float:
    if probability == 0.0:
        return 0.0 if successes == 0 else -math.inf
    if probability == 1.0:
        return 0.0 if successes == trials else -math.inf
    return (
        math.lgamma(trials + 1)
        - math.lgamma(successes + 1)
        - math.lgamma(trials - successes + 1)
        + successes * math.log(probability)
        + (trials - successes) * math.log1p(-probability)
    )


def _sum_log_probabilities(log_probabilities: Sequence[float]) -> float:
    finite = [value for value in log_probabilities if value != -math.inf]
    if not finite:
        return 0.0
    pivot = max(finite)
    return math.exp(pivot) * math.fsum(math.exp(value - pivot) for value in finite)


def exact_binomial_test(
    successes: int,
    trials: int,
    *,
    probability: float = 0.5,
    alternative: Alternative = "two-sided",
) -> float:
    """Return the exact binomial-test p-value.

    The two-sided definition matches the conventional exact test: sum the
    probability of every outcome no more likely under the null than the
    observed outcome. This is not the doubled one-sided approximation.
    """
    _validate_count(successes, "successes")
    _validate_count(trials, "trials")
    if successes > trials:
        raise ValueError("successes cannot exceed trials")
    _validate_probability(probability, "probability")
    _validate_alternative(alternative)
    if trials == 0:
        return 1.0

    observed = _log_binomial_pmf(successes, trials, probability)
    if alternative == "less":
        included: Sequence[int] = range(successes + 1)
    elif alternative == "greater":
        included = range(successes, trials + 1)
    else:
        if observed == -math.inf:
            return 0.0
        # Floating-point log probabilities that are equal analytically can
        # differ by a few ulps. A scale-aware tolerance keeps both tails.
        tolerance = 1e-12 * max(1.0, abs(observed))
        included = [
            outcome
            for outcome in range(trials + 1)
            if _log_binomial_pmf(outcome, trials, probability) <= observed + tolerance
        ]
    p_value = _sum_log_probabilities(
        [_log_binomial_pmf(outcome, trials, probability) for outcome in included]
    )
    return min(1.0, max(0.0, p_value))


def sign_test(
    observations_or_positive: Sequence[float] | int,
    negative: int | None = None,
    *,
    alternative: Alternative = "two-sided",
) -> float:
    """Return an exact paired sign-test p-value, excluding ties.

    Pass either a sequence of signed paired differences, or explicit positive
    and negative counts as ``sign_test(positive, negative)``.
    """
    if isinstance(observations_or_positive, bool):
        raise ValueError("positive must be a non-negative integer")
    if isinstance(observations_or_positive, int):
        if negative is None:
            raise ValueError("negative count is required when positive is a count")
        positive = observations_or_positive
        _validate_count(positive, "positive")
        _validate_count(negative, "negative")
    else:
        if negative is not None:
            raise ValueError("negative count cannot be combined with signed observations")
        values = [float(value) for value in observations_or_positive]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("signed observations must be finite")
        positive = sum(value > 0.0 for value in values)
        negative = sum(value < 0.0 for value in values)
    return exact_binomial_test(
        positive,
        positive + negative,
        probability=0.5,
        alternative=alternative,
    )


def mcnemar_test(
    first_only: int,
    second_only: int,
    *,
    exact: bool = True,
    correction: bool = True,
) -> float:
    """Return the two-sided McNemar p-value from discordant paired counts.

    ``first_only`` and ``second_only`` are the off-diagonal cells. The exact
    form is a binomial test. The optional large-sample form uses a one-degree
    chi-square statistic, with Edwards' continuity correction by default.
    """
    _validate_count(first_only, "first_only")
    _validate_count(second_only, "second_only")
    discordant = first_only + second_only
    if discordant == 0:
        return 1.0
    if exact:
        return exact_binomial_test(first_only, discordant)

    difference = abs(first_only - second_only)
    if correction:
        difference = max(0, difference - 1)
    statistic = difference**2 / discordant
    # For one degree of freedom, chi-square survival is erfc(sqrt(x / 2)).
    return math.erfc(math.sqrt(statistic / 2.0))


def wilson_confidence_interval(
    successes: float,
    trials: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Return a Wilson score interval for a binomial proportion.

    Fractional ``successes`` are accepted for aggregated soft scores, provided
    they remain within ``[0, trials]``. At zero trials there is no estimate,
    so callers must supply observations rather than receiving a fake interval.
    """
    _validate_count(trials, "trials")
    if not math.isfinite(successes) or not 0.0 <= successes <= trials:
        raise ValueError("successes must be finite and between 0 and trials")
    if trials == 0:
        raise ValueError("trials must be positive")
    _validate_confidence(confidence)

    z = statistics.NormalDist().inv_cdf(0.5 + confidence / 2.0)
    proportion = successes / trials
    z_squared = z * z
    denominator = 1.0 + z_squared / trials
    center = (proportion + z_squared / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / trials
            + z_squared / (4.0 * trials * trials)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def mean_confidence_interval(
    observations: Sequence[float],
    *,
    confidence: float = 0.95,
) -> IntervalEstimate:
    """Estimate a normal-approximation interval for a replicate mean.

    The sample standard deviation estimates replicate variance. Fewer than two
    observations produce an explicit unstable result with no bounds, never a
    zero-width interval that could accidentally pass a reproducibility gate.
    """
    _validate_confidence(confidence)
    values = [float(value) for value in observations]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("observations must be finite")
    count = len(values)
    if count < _MIN_STABLE_OBSERVATIONS:
        return IntervalEstimate(
            bounds=None,
            observations=count,
            stable=False,
            reason="fewer_than_two_observations",
        )

    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(count)
    z = statistics.NormalDist().inv_cdf(0.5 + confidence / 2.0)
    margin = z * standard_error
    return IntervalEstimate(
        bounds=(mean - margin, mean + margin),
        observations=count,
        stable=True,
    )


def minimum_detectable_effect(
    sample_size_per_arm: int,
    *,
    baseline_rate: float = 0.5,
    alpha: float = 0.05,
    target_power: float = 0.8,
    two_sided: bool = True,
) -> float:
    """Approximate the detectable absolute rate difference for equal-size arms.

    This prospective planning approximation uses variance at ``baseline_rate``.
    It is the algebraic inverse of :func:`statistical_power` up to the
    negligible opposite tail of a two-sided test.
    """
    _validate_count(sample_size_per_arm, "sample_size_per_arm")
    if sample_size_per_arm == 0:
        raise ValueError("sample_size_per_arm must be positive")
    _validate_probability(baseline_rate, "baseline_rate")
    if baseline_rate in {0.0, 1.0}:
        raise ValueError("baseline_rate must be strictly between 0 and 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    if not 0.0 < target_power < 1.0:
        raise ValueError("target_power must be strictly between 0 and 1")

    normal = statistics.NormalDist()
    alpha_quantile = normal.inv_cdf(1.0 - alpha / (2.0 if two_sided else 1.0))
    power_quantile = normal.inv_cdf(target_power)
    standard_error = math.sqrt(
        2.0 * baseline_rate * (1.0 - baseline_rate) / sample_size_per_arm
    )
    return (alpha_quantile + power_quantile) * standard_error


def statistical_power(
    effect: float,
    sample_size_per_arm: int,
    *,
    baseline_rate: float = 0.5,
    alpha: float = 0.05,
    two_sided: bool = True,
) -> float:
    """Approximate power for an equal-size two-arm rate comparison."""
    if not math.isfinite(effect):
        raise ValueError("effect must be finite")
    _validate_count(sample_size_per_arm, "sample_size_per_arm")
    if sample_size_per_arm == 0:
        raise ValueError("sample_size_per_arm must be positive")
    _validate_probability(baseline_rate, "baseline_rate")
    if baseline_rate in {0.0, 1.0}:
        raise ValueError("baseline_rate must be strictly between 0 and 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")

    standard_error = math.sqrt(
        2.0 * baseline_rate * (1.0 - baseline_rate) / sample_size_per_arm
    )
    normal = statistics.NormalDist()
    critical = normal.inv_cdf(1.0 - alpha / (2.0 if two_sided else 1.0))
    noncentrality = abs(effect) / standard_error
    if two_sided:
        result = 1.0 - normal.cdf(critical - noncentrality)
        result += normal.cdf(-critical - noncentrality)
    else:
        result = 1.0 - normal.cdf(critical - noncentrality)
    return min(1.0, max(0.0, result))


# Concise aliases for callers that use the standard abbreviated names.
wilson_interval = wilson_confidence_interval
wilson_ci = wilson_confidence_interval
power = statistical_power


__all__ = [
    "Alternative",
    "IntervalEstimate",
    "exact_binomial_test",
    "mcnemar_test",
    "mean_confidence_interval",
    "minimum_detectable_effect",
    "power",
    "sign_test",
    "statistical_power",
    "wilson_confidence_interval",
    "wilson_ci",
    "wilson_interval",
]
