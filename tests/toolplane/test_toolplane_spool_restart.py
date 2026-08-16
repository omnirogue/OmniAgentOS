"""Test toolplane spool restart recovery and corrupt entry quarantine."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.toolplane.observe import (
    DEADLETTER_SUBDIR,
    OBSERVATIONS_SUBDIR,
    SPOOL_SUBDIR,
    ObservationSink,
)


def test_toolplane_spool_restart_recovery_and_quarantine(tmp_path: Path) -> None:
    ledger_dir = str(tmp_path)
    spool_dir = tmp_path / SPOOL_SUBDIR
    spool_dir.mkdir(parents=True)

    # Write a valid spooled entry
    valid_payload = {
        "attempts": 1,
        "observation": {
            "version": "1",
            "ts": "2026-07-25T20:00:00+00:00",
            "tool": "read_file",
            "correlation_id": "corr_valid_12345",
            "status": "success",
            "ok": True,
            "duration_ms": 12,
            "source": "toolplane",
        },
    }
    (spool_dir / "corr_valid_12345.json").write_text(json.dumps(valid_payload), encoding="utf-8")

    # Write a corrupt spooled entry (invalid JSON)
    (spool_dir / "corr_corrupt_67890.json").write_text("{corrupt json content", encoding="utf-8")

    # Initialize a fresh ObservationSink (simulating process restart)
    sink = ObservationSink(ledger_dir=ledger_dir)

    # recover_pending sees the valid entry (corrupt entry gets quarantined during load)
    assert sink.recover_pending() == 1

    # retry_pending drains the valid entry and quarantines the corrupt entry
    still_pending = sink.retry_pending()
    assert still_pending == 0

    # The valid observation is now durably stored
    obs_dir = tmp_path / OBSERVATIONS_SUBDIR
    obs_files = list(obs_dir.glob("*.json"))
    assert len(obs_files) == 1
    recorded = json.loads(obs_files[0].read_text(encoding="utf-8"))
    assert recorded["correlation_id"] == "corr_valid_12345"

    # Corrupt file moved to deadletter directory
    deadletter_dir = tmp_path / DEADLETTER_SUBDIR
    dead_files = list(deadletter_dir.glob("*.json"))
    assert len(dead_files) == 1
    assert "corrupt_" in dead_files[0].name
