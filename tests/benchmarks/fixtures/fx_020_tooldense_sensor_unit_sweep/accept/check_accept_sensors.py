"""FROZEN acceptance check for fx_020_tooldense_sensor_unit_sweep

This file is copied in after the agent finishes execution so that the agent
cannot weaken or change the verification rules.
"""

from __future__ import annotations

import importlib
import math
import types

import fleet
import summary
import units


def test_sensor_modules_constants() -> None:
    """Verifies that each of the 12 sensor modules correctly defines SI_UNIT and SI_READINGS."""
    for name in fleet.sensor_modules():
        mod = importlib.import_module(f"sensors.{name}")
        assert hasattr(mod, "SI_UNIT"), f"Module {name} is missing SI_UNIT"
        assert hasattr(mod, "SI_READINGS"), f"Module {name} is missing SI_READINGS"

        expected_unit = units.si_unit(mod.UNIT)
        assert mod.SI_UNIT == expected_unit, f"{name}: expected {expected_unit}, got {mod.SI_UNIT}"

        assert len(mod.SI_READINGS) == len(mod.READINGS), f"{name}: length mismatch"
        for raw, si in zip(mod.READINGS, mod.SI_READINGS, strict=True):
            expected_val, _ = units.convert(raw, mod.UNIT)
            assert math.isclose(si, expected_val, rel_tol=1e-12), (
                f"{name}: value mismatch: got {si}, expected {expected_val}"
            )


def test_fleet_loading() -> None:
    """Verifies that load_sensors correctly populates the sensor info with raw and SI fields."""
    sensors = fleet.load_sensors()
    assert len(sensors) == 12, f"Expected 12 sensors, got {len(sensors)}"
    for name in fleet.sensor_modules():
        mod = importlib.import_module(f"sensors.{name}")
        assert name in sensors
        info = sensors[name]
        assert info.sensor_id == name
        assert info.unit == mod.UNIT
        assert info.readings == mod.READINGS
        assert info.si_unit == mod.SI_UNIT
        assert info.si_readings == mod.SI_READINGS


def test_fleet_error_handling() -> None:
    """Verifies that load_sensors raises SensorError if SI constants are missing."""
    assert issubclass(fleet.SensorError, RuntimeError)

    original_import = importlib.import_module

    def mock_import(name: str) -> any:
        if name == "sensors.altimeter":
            m = types.ModuleType("sensors.altimeter")
            m.SENSOR_ID = "altimeter"
            m.UNIT = "foot"
            m.READINGS = (1.0,)
            # SI_UNIT and SI_READINGS are missing
            return m
        return original_import(name)

    importlib.import_module = mock_import
    try:
        try:
            fleet.load_sensors()
            raise AssertionError(
                "load_sensors did not raise SensorError when constants were missing"
            )
        except fleet.SensorError as e:
            assert "missing" in str(e).lower() or "altimeter" in str(e).lower()
    finally:
        importlib.import_module = original_import


def test_fleet_report() -> None:
    """Verifies that report matches the byte-exact expected report formatting."""
    expected_report = (
        "altimeter: 3 readings in meter\n"
        "anemometer: 3 readings in m_per_s\n"
        "barometer: 3 readings in pascal\n"
        "flowmeter: 3 readings in cubic_meter\n"
        "hygrometer: 3 readings in meter\n"
        "manometer: 3 readings in pascal\n"
        "odometer: 3 readings in meter\n"
        "pitot: 3 readings in m_per_s\n"
        "pyranometer: 3 readings in pascal\n"
        "scale: 3 readings in kilogram\n"
        "tachometer: 3 readings in kilogram\n"
        "thermocouple: 3 readings in kelvin"
    )
    actual_report = fleet.report()
    assert actual_report == expected_report, (
        f"Report mismatch.\nExpected:\n{expected_report}\nGot:\n{actual_report}"
    )


def test_summary_expected_units() -> None:
    """Verifies that summary.EXPECTED_SI_UNITS and summary.si_units are updated and correct."""
    sensors = fleet.load_sensors()
    assert len(summary.EXPECTED_SI_UNITS) == 12, "EXPECTED_SI_UNITS must have exactly 12 sensors"
    for name, info in sensors.items():
        assert name in summary.EXPECTED_SI_UNITS, f"Missing {name} in EXPECTED_SI_UNITS"
        assert summary.EXPECTED_SI_UNITS[name] == info.si_unit, (
            f"{name}: unit mismatch in EXPECTED_SI_UNITS"
        )

    si_mapping = summary.si_units()
    expected_mapping = tuple(sorted(summary.EXPECTED_SI_UNITS.items()))
    assert si_mapping == expected_mapping, (
        "si_units returned mapping is not sorted or does not match"
    )
