"""Flowmeter sensor."""

from __future__ import annotations

import units

SENSOR_ID = "flowmeter"
UNIT = "gallon"
READINGS: tuple[float, ...] = (1.2, 2.5, 0.8)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
