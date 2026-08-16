"""Smoothing utilities for time-series / data sequences."""

from __future__ import annotations


def moving_average(values: list[float], window: int) -> list[float]:
    """Return the simple moving average of values using the specified window size.

    The resulting list has length len(values) - window + 1.
    Raises ValueError if window < 1 or window > len(values).
    """
    if window < 1:
        raise ValueError("Window size must be at least 1.")
    if window > len(values):
        raise ValueError("Window size cannot be greater than the number of values.")

    result = []
    for i in range(len(values) - window + 1):
        result.append(sum(values[i : i + window]) / window)

    return result


def exponential_moving_average(values: list[float], alpha: float) -> list[float]:
    """Compute the exponential moving average of values with smoothing factor alpha.

    ema[0] = values[0]
    ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]

    Raises ValueError if alpha is not in (0, 1].
    An empty input returns an empty list.
    """
    if not (0.0 < alpha <= 1.0):
        raise ValueError("Alpha must be strictly greater than 0 and less than or equal to 1.")
    if not values:
        return []

    ema = [values[0]]
    for i in range(1, len(values)):
        ema.append(alpha * values[i] + (1.0 - alpha) * ema[-1])
    return ema


def deltas(values: list[float]) -> list[float]:
    """Return consecutive differences of values: deltas[i] = values[i+1] - values[i].

    Returns an empty list for inputs of length 0 or 1.
    """
    if len(values) <= 1:
        return []
    return [values[i + 1] - values[i] for i in range(len(values) - 1)]
