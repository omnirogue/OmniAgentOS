"""Tiny subject module for doctrine self-tests and worked-example shapes.

Mirrors a real claim in this repo: a rate over an empty denominator is
``None`` (unknown), never a flattering constant. See
``tests/tracelab/test_metrics.py::test_empty_denominator_rates_are_unknown_through_row_serialization``
and ``omniagentos/tracelab/metrics.py`` / ``omniagentos/pulse/aggregator.py``.
"""
