"""Tachometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "tachometer"
UNIT = "ounce"
READINGS: tuple[float, ...] = (2.3, 2.5, 2.1)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
