"""Anemometer sensor."""

from __future__ import annotations

import units

SENSOR_ID = "anemometer"
UNIT = "km_per_h"
READINGS: tuple[float, ...] = (15.0, 22.5, 18.0)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
