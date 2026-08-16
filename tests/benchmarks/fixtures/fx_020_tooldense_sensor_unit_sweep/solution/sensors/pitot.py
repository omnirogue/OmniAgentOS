"""Pitot sensor."""

from __future__ import annotations

import units

SENSOR_ID = "pitot"
UNIT = "knot"
READINGS: tuple[float, ...] = (120.0, 135.5, 110.0)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
