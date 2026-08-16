"""Unit tests for the read-only launchd fleet pager."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "fleet_pager.py"
SPEC = importlib.util.spec_from_file_location("fleet_pager_under_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fleet_pager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fleet_pager
SPEC.loader.exec_module(fleet_pager)


def test_parse_launchctl_list_filters_our_units(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "PID\tStatus\tLabel\n123\t0\tcom.omniagentos.api\n-\t-15\tcom.omniagentos.runner\n1\t0\tcom.example.other\n"
    monkeypatch.setattr(
        fleet_pager.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )

    assert fleet_pager.read_launchctl_list() == {
        "com.omniagentos.api": ("123", "0"),
        "com.omniagentos.runner": ("-", "-15"),
    }


def test_detect_alerts_handles_loaded_signal_and_disappeared_transitions() -> None:
    baseline = fleet_pager.state_for_units(
        {"com.omniagentos.api": ("123", "0"), "com.omniagentos.old": ("-", "-")},
        seen_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    initially_loaded = fleet_pager.detect_alerts(
        fleet_pager.empty_state(), {"com.omniagentos.new": ("124", "0")}
    )
    loaded = fleet_pager.detect_alerts(baseline, {"com.omniagentos.api": ("124", "0")})
    signalled = fleet_pager.detect_alerts(baseline, {"com.omniagentos.api": ("124", "-15")})
    disappeared = fleet_pager.detect_alerts(baseline, {})

    assert initially_loaded == []
    assert [event.event_type for event in loaded] == ["disappeared"]
    assert [(event.event_type, event.unit, event.last_exit_status) for event in signalled] == [
        ("signal", "com.omniagentos.api", "-15"),
        ("disappeared", "com.omniagentos.old", "-"),
    ]
    assert {event.unit for event in disappeared} == {
        "com.omniagentos.api",
        "com.omniagentos.old",
    }


def test_deduplication_window_is_per_unit_and_condition() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    signal = fleet_pager.AlertEvent("signal", "com.omniagentos.api", "1", "-15", "0")
    state = {"version": 1, "alerts": {signal.key: (now - timedelta(minutes=4)).isoformat()}}

    assert not fleet_pager.should_emit(signal, state, now=now)
    assert fleet_pager.should_emit(signal, state, now=now + timedelta(minutes=1))
    assert fleet_pager.should_emit(
        fleet_pager.AlertEvent("signal", signal.unit, "1", "-9", "0"), state, now=now
    )


def test_json_state_serialization_and_deserialization() -> None:
    state = fleet_pager.state_for_units(
        {"com.omniagentos.api": ("123", "0")},
        seen_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )

    assert json.loads(json.dumps(state)) == state


def test_run_uses_mocked_io_and_records_signal_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state.json"
    alert_path = tmp_path / "alert-state.json"
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    baseline = fleet_pager.state_for_units({"com.omniagentos.api": ("123", "0")}, seen_at=now)
    writes: dict[Path, dict[str, object]] = {}
    monkeypatch.setattr(
        fleet_pager, "read_launchctl_list", lambda: {"com.omniagentos.api": ("-", "-15")}
    )
    monkeypatch.setattr(
        fleet_pager,
        "load_json",
        lambda path, default: baseline if path == state_path else default,
    )
    monkeypatch.setattr(
        fleet_pager, "save_json", lambda path, payload: writes.setdefault(path, payload)
    )
    calls: list[dict[str, str]] = []

    emitted = fleet_pager.run(
        now=now,
        state_path=state_path,
        alert_state_path=alert_path,
        recorder=lambda **kwargs: calls.append(kwargs),
    )

    assert [event.event_type for event in emitted] == ["signal"]
    assert calls[0]["ref_type"] == "fleet_pager"
    assert calls[0]["ref_id"] == "com.omniagentos.api:2026-08-03"
    assert writes[state_path]["units"]["com.omniagentos.api"]["last_exit_status"] == "-15"
