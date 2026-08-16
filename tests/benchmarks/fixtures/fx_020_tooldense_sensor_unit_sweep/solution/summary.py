"""Sensor expected SI units summary."""

from __future__ import annotations

EXPECTED_SI_UNITS: dict[str, str] = {
    "altimeter": "meter",
    "anemometer": "m_per_s",
    "barometer": "pascal",
    "flowmeter": "cubic_meter",
    "hygrometer": "meter",
    "manometer": "pascal",
    "odometer": "meter",
    "pitot": "m_per_s",
    "pyranometer": "pascal",
    "scale": "kilogram",
    "tachometer": "kilogram",
    "thermocouple": "kelvin",
}


def si_units() -> tuple[tuple[str, str], ...]:
    """Returns the expected SI unit mapping sorted by sensor ID."""
    return tuple(sorted(EXPECTED_SI_UNITS.items()))
