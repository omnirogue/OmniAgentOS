"""Fleet sensor registry and raw reporter."""

from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SensorInfo:
    """Dataclass holding raw sensor information."""

    sensor_id: str
    unit: str
    readings: tuple[float, ...]


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
    """Imports each sensor module and populates a mapping from sensor ID to SensorInfo."""
    sensors = {}
    for name in sensor_modules():
        mod = importlib.import_module(f"sensors.{name}")
        sensors[mod.SENSOR_ID] = SensorInfo(
            sensor_id=mod.SENSOR_ID,
            unit=mod.UNIT,
            readings=mod.READINGS,
        )
    return sensors


def report() -> str:
    """Generates a raw report of active sensors in the fleet."""
    sensors = load_sensors()
    lines = []
    for sid in sorted(sensors.keys()):
        s = sensors[sid]
        lines.append(f"{sid}: {len(s.readings)} readings in {s.unit}")
    return "\n".join(lines)
