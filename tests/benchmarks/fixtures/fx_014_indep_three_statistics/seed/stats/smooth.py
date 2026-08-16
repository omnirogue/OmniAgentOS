"""Smoothing utilities for time-series / data sequences."""

from __future__ import annotations


def moving_average(values: list[float], window: int) -> list[float]:
    """Return the simple moving average of values using the specified window size."""
    raise NotImplementedError()


def exponential_moving_average(values: list[float], alpha: float) -> list[float]:
    """Compute the exponential moving average of values with smoothing factor alpha."""
    raise NotImplementedError()


def deltas(values: list[float]) -> list[float]:
    """Return consecutive differences of values."""
    raise NotImplementedError()
