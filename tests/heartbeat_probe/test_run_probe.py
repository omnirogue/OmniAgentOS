"""End-to-end regression coverage for ``scripts/heartbeat-probe/run_probe.sh``."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "scripts" / "heartbeat-probe" / "run_probe.sh"
GATE = REPO_ROOT / "scripts" / "merge-gate.sh"
RUNTIME_DIR = REPO_ROOT / "var" / "heartbeat-probe"
STATIONS = ("propose", "claim", "build", "gate", "receipt", "learning_event", "cleanup")


def _run_probe(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PROBE), *args],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )


def _summary_statuses(output: str) -> dict[str, str]:
    stations = "|".join(STATIONS)
    matches = re.findall(rf"^\s*({stations})\s+(PASS|FAIL)\s+—", output, re.MULTILINE)
    assert len(matches) >= len(STATIONS), output
    return dict(matches[-len(STATIONS) :])


def _runtime_snapshot() -> set[Path]:
    if not RUNTIME_DIR.exists():
        return set()
    return {path.relative_to(RUNTIME_DIR) for path in RUNTIME_DIR.rglob("*")}


def test_dry_run_passes_all_stations_without_durable_probe_output() -> None:
    before = _runtime_snapshot()
    result = _run_probe("--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _summary_statuses(result.stdout) == {station: "PASS" for station in STATIONS}
    assert _runtime_snapshot() == before


def test_injected_gate_failure_is_the_only_failed_station() -> None:
    result = _run_probe("--inject-failure=gate")

    assert result.returncode != 0
    assert _summary_statuses(result.stdout) == {
        **{station: "PASS" for station in STATIONS if station != "gate"},
        "gate": "FAIL",
    }
    assert "injected failure for station-level verification" in result.stdout


def test_missing_real_gate_wrapper_fails_gate_station_and_is_restored() -> None:
    if not GATE.exists():
        pytest.skip("scripts/merge-gate.sh is already missing")

    backup = GATE.with_name(f"{GATE.name}.heartbeat-probe-test.bak")
    if backup.exists():
        pytest.skip(f"refusing to overwrite pre-existing backup: {backup}")

    os.rename(GATE, backup)
    try:
        result = _run_probe()
    finally:
        os.rename(backup, GATE)

    assert GATE.exists()
    assert result.returncode != 0
    statuses = _summary_statuses(result.stdout)
    assert statuses["gate"] == "FAIL"
    assert "real merge-gate wrapper is missing or non-executable" in result.stdout


def test_normal_run_writes_a_synthetic_learning_event() -> None:
    result = _run_probe()

    assert result.returncode == 0, result.stdout + result.stderr
    event_file = RUNTIME_DIR / "synthetic-events.jsonl"
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[-1])
    assert event["synthetic"] is True
