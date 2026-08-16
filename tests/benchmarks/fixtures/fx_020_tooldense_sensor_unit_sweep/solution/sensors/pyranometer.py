"""Pyranometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "pyranometer"
UNIT = "mmhg"
READINGS: tuple[float, ...] = (760.0, 755.2, 762.1)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
