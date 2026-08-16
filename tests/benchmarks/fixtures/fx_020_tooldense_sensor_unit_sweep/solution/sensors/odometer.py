"""Odometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "odometer"
UNIT = "mile"
READINGS: tuple[float, ...] = (104.5, 105.2, 106.0)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
