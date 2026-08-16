"""Manometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "manometer"
UNIT = "bar"
READINGS: tuple[float, ...] = (1.013, 1.05, 0.98)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
