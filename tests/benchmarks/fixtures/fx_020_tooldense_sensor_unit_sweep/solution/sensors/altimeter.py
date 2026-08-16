"""Altimeter sensor."""

from __future__ import annotations

import units

SENSOR_ID = "altimeter"
UNIT = "foot"
READINGS: tuple[float, ...] = (12000.0, 12500.0, 11800.0)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
