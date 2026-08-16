from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.lease.models import (
    LEASE_VERSION,
    Lease,
    LeaseCeilings,
    LeaseSubject,
    current_generation,
    sign_lease,
)
from omniagentos.lease.record import (
    append_lease_record,
    lease_ledger_path,
    lease_line,
)


def _lease(**overrides: Any) -> Lease:
    """Helper to build a Lease instance with default values for tests."""
    defaults: dict[str, Any] = {
        "lease_id": "lse_test",
        "subject": LeaseSubject(run_id="run_1"),
        "issued_at": 1000.0,
        "expires_at": 2000.0,
        "generation": current_generation(),
        "fs_read_roots": (),
        "fs_write_roots": (),
        "net_mode": "open",
        "net_allow_domains": (),
        "capabilities": (),
        "credential_handles": (),
        "ceilings": LeaseCeilings(),
        "auto_run_effect_classes": (),
        "approval_required_classes": (),
        "version": LEASE_VERSION,
        "signature": "",
    }
    defaults.update(overrides)
    return Lease(**defaults)


def test_lease_ledger_path(tmp_path: Path) -> None:
    """Pin that lease_ledger_path resolves to monthly-partitioned JSONL ledger files using the correct month suffix."""
    epoch = 1700000000.0  # Unambiguous epoch
    expected_month = datetime.fromtimestamp(epoch, UTC).strftime("%Y%m")
    expected_filename = f"leases-{expected_month}.jsonl"

    resolved = lease_ledger_path(str(tmp_path), when=epoch)
    assert resolved.name == expected_filename
    assert resolved.parent == tmp_path


def test_lease_line_and_security_property() -> None:
    """Pin lease_line payload contents, newline formatting, and strict signature security properties."""
    signed = sign_lease(_lease())
    assert signed.signature != ""  # Verify it is signed

    line = lease_line(signed, event="launched", mode="enforce")

    # Assert formatting
    assert line.endswith("\n")

    # Parse and assert content
    record = json.loads(line)
    assert record["event"] == "launched"
    assert record["mode"] == "enforce"
    assert "recorded_at" in record
    assert record["lease_id"] == "lse_test"

    # Assert security property
    assert "signature" not in record
    assert signed.signature not in line


def test_append_lease_record_creates_parent_appends_and_merges_extras(tmp_path: Path) -> None:
    """Pin that append_lease_record handles directory creation, multi-line append telemetry, and top-level extra metadata."""
    ledger_dir = tmp_path / "deep" / "nested" / "ledger"
    # Ensure directory does not exist initially
    assert not ledger_dir.exists()

    lease_obj = sign_lease(_lease())

    # Call 1
    path_str = append_lease_record(
        lease_obj,
        event="issued",
        mode="enforce",
        ledger_dir=str(ledger_dir),
        extra={"custom_key": "custom_val"},
    )
    assert path_str != ""
    written_path = Path(path_str)
    assert written_path.exists()

    # Read and assert Call 1 contents
    lines = written_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "issued"
    assert record["custom_key"] == "custom_val"

    # Call 2 with the same lease
    path_str_2 = append_lease_record(
        lease_obj,
        event="launched",
        mode="enforce",
        ledger_dir=str(ledger_dir),
        extra={"another_key": "another_val"},
    )
    assert path_str_2 == path_str

    # Read and assert Call 2 contents
    lines_2 = written_path.read_text(encoding="utf-8").splitlines()
    assert len(lines_2) == 2
    record_1 = json.loads(lines_2[0])
    record_2 = json.loads(lines_2[1])
    assert record_1["event"] == "issued"
    assert record_2["event"] == "launched"
    assert record_2["another_key"] == "another_val"


def test_append_lease_record_fails_closed_without_raising(tmp_path: Path) -> None:
    """Pin that append_lease_record degrades gracefully on OSError, returning an empty string without propagating the exception."""
    # Create an existing file to block directory creation
    existing_file = tmp_path / "blocker_file"
    existing_file.write_text("not a directory", encoding="utf-8")

    # Point ledger_dir underneath the file
    bad_ledger_dir = existing_file / "bad_subdir"

    lease_obj = sign_lease(_lease())

    # This attempt should catch OSError internally, log it, and return ""
    result_path = append_lease_record(
        lease_obj,
        event="issued",
        mode="enforce",
        ledger_dir=str(bad_ledger_dir),
    )
    assert result_path == ""
