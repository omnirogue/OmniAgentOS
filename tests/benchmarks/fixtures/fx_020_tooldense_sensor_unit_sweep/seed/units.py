"""Unit conversion utilities for OmniAgentOS sensors."""

from __future__ import annotations


class UnknownUnit(KeyError):
    """Raised when an unknown unit is passed."""

    pass


TO_SI: dict[str, tuple[str, float]] = {
    "celsius": ("kelvin", 1.0),  # offset handled by convert()
    "fahrenheit": ("kelvin", 1.0),  # offset handled by convert()
    "psi": ("pascal", 6894.757293168361),
    "bar": ("pascal", 100000.0),
    "mmhg": ("pascal", 133.322387415),
    "km_per_h": ("m_per_s", 0.2777777777777778),
    "mph": ("m_per_s", 0.44704),
    "knot": ("m_per_s", 0.5144444444444445),
    "foot": ("meter", 0.3048),
    "inch": ("meter", 0.0254),
    "mile": ("meter", 1609.344),
    "pound": ("kilogram", 0.45359237),
    "ounce": ("kilogram", 0.028349523125),
    "gallon": ("cubic_meter", 0.003785411784),
}


def convert(value: float, unit: str) -> tuple[float, str]:
    """Converts a value from the specified unit to its SI equivalent.

    Returns (converted_value, si_unit_name).
    """
    if unit not in TO_SI:
        raise UnknownUnit(f"Unknown unit: {unit}")

    target_unit, factor = TO_SI[unit]
    if unit == "celsius":
        return value + 273.15, target_unit
    elif unit == "fahrenheit":
        return (value - 32.0) * 5.0 / 9.0 + 273.15, target_unit
    else:
        return value * factor, target_unit


def si_unit(unit: str) -> str:
    """Returns the SI unit name corresponding to the given unit."""
    if unit not in TO_SI:
        raise UnknownUnit(f"Unknown unit: {unit}")
    return TO_SI[unit][0]
