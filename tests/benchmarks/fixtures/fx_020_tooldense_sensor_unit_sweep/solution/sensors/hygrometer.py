"""Hygrometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "hygrometer"
UNIT = "inch"
READINGS: tuple[float, ...] = (0.15, 0.22, 0.18)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
