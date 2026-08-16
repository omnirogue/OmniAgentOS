"""Scale sensor."""

from __future__ import annotations

import units

SENSOR_ID = "scale"
UNIT = "pound"
READINGS: tuple[float, ...] = (150.0, 155.2, 148.5)

SI_UNIT: str = units.si_unit(UNIT)
SI_READINGS: tuple[float, ...] = tuple(units.convert(v, UNIT)[0] for v in READINGS)
