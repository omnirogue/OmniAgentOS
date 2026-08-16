"""Observability pulse — daily snapshot aggregator + time-series store.

One row per (metric, date) in ``pulse_series``. The aggregator is callable as
a plain function (routine-compatible) so existing schedulers can invoke it
without bespoke wiring. The :class:`PulseStore` exposes read-only series
access and the seed-on-empty helper the HTTP layer needs so the very first
chart load is never blank.

Metric namespace is dotted (``skills.total``, ``loops.fires`` …) so a single
table carries every tile's trend without JOINs.
"""

from omniagentos.pulse.aggregator import METRICS, snapshot
from omniagentos.pulse.store import PulseStore

__all__ = ["PulseStore", "METRICS", "snapshot"]
