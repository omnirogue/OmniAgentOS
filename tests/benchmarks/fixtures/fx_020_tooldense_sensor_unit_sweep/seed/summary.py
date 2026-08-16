"""Sensor expected SI units summary."""

from __future__ import annotations

# Deliberately stale unit map: only 8 sensors, and some are wrong.
EXPECTED_SI_UNITS: dict[str, str] = {
    "altimeter": "feet",
    "anemometer": "m_per_s",
    "barometer": "pascal",
    "flowmeter": "cubic_meter",
    "hygrometer": "inches",
    "manometer": "pascal",
    "odometer": "meter",
    "pitot": "m_per_s",
}


def si_units() -> tuple[tuple[str, str], ...]:
    """Returns the expected SI unit mapping sorted by sensor ID."""
    return tuple(sorted(EXPECTED_SI_UNITS.items()))
