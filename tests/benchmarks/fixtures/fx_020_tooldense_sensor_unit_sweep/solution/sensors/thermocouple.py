"""Thermocouple sensor."""

from __future__ import annotations

import units

SENSOR_ID = "thermocouple"
UNIT = "fahrenheit"
READINGS: tuple[float, ...] = (68.0, 72.5, 65.0)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
