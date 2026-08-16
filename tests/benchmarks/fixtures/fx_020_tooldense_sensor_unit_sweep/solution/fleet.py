"""Fleet sensor registry and upgraded SI reporter."""

from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SensorInfo:
    """Dataclass holding raw and SI converted sensor information."""

    sensor_id: str
    unit: str
    readings: tuple[float, ...]
    si_unit: str
    si_readings: tuple[float, ...]


class SensorError(RuntimeError):
    """Raised when there is a configuration or missing constant issue with a sensor."""

    pass


def sensor_modules() -> tuple[str, ...]:
    """Returns an explicit tuple of the twelve sensor module names."""
    return (
        "altimeter",
        "anemometer",
        "barometer",
        "flowmeter",
        "hygrometer",
        "manometer",
        "odometer",
        "pitot",
        "pyranometer",
        "scale",
        "tachometer",
        "thermocouple",
    )


def load_sensors() -> dict[str, SensorInfo]:
    """Imports each sensor module and populates a mapping from sensor ID to SensorInfo.

    Raises SensorError if any module is missing its expected SI constants.
    """
    sensors = {}
    for name in sensor_modules():
        mod = importlib.import_module(f"sensors.{name}")

        if not hasattr(mod, "SI_UNIT"):
            raise SensorError(f"Module {name} is missing required SI_UNIT constant")
        if not hasattr(mod, "SI_READINGS"):
            raise SensorError(f"Module {name} is missing required SI_READINGS constant")

        sensors[mod.SENSOR_ID] = SensorInfo(
            sensor_id=mod.SENSOR_ID,
            unit=mod.UNIT,
            readings=mod.READINGS,
            si_unit=mod.SI_UNIT,
            si_readings=mod.SI_READINGS,
        )
    return sensors


def report() -> str:
    """Generates an upgraded SI report of active sensors in the fleet, sorted by sensor ID."""
    sensors = load_sensors()
    lines = []
    for sid in sorted(sensors.keys()):
        s = sensors[sid]
        lines.append(f"{sid}: {len(s.readings)} readings in {s.si_unit}")
    return "\n".join(lines)
