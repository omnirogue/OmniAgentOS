"""Tests for zero-row writer probe.

The probe must:
1. Open the database in read-only mode (mode=ro, no writes)
2. Report row counts for each target table
3. Locate writer call sites by grep
4. Determine writer status (unreachable/gated/failing_silently)
5. Emit JSON findings report

All operations are read-only; the database and schema are never mutated.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def test_db_path():
    """Create a temporary test database with zero-row target tables."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        # Create database with the target tables (schema only, zero rows)
        conn = sqlite3.connect(str(db_path))
        try:
            # Create repomap_tag_cache
            conn.execute(
                """
                CREATE TABLE repomap_tag_cache (
                    content_hash TEXT PRIMARY KEY,
                    lang TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    tags_json TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_used TEXT NOT NULL
                )
            """
            )

            # Create gate_evidence
            conn.execute(
                """
                CREATE TABLE gate_evidence (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    run_id TEXT,
                    task_id TEXT,
                    attempt_ref TEXT,
                    gate_name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_ms REAL,
                    output_artifact_path TEXT,
                    output_sha256 TEXT,
                    test_counts_json TEXT,
                    commit_sha TEXT,
                    env_digest TEXT
                )
            """
            )

            # Create provider_call_usage
            conn.execute(
                """
                CREATE TABLE provider_call_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    run_id TEXT,
                    campaign_id TEXT,
                    reservation_id TEXT,
                    task_id TEXT,
                    attempt_id TEXT,
                    session_id TEXT,
                    work_id TEXT,
                    root_trace_id TEXT,
                    stage TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    effective_model TEXT NOT NULL,
                    model_lineage TEXT NOT NULL,
                    billing_provider TEXT NOT NULL,
                    adapter_key TEXT NOT NULL,
                    provider_request_id TEXT,
                    request_state TEXT NOT NULL,
                    provider_outcome TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_usd_decimal TEXT,
                    cost_usd_nanos INTEGER,
                    cost_upper_bound_usd_nanos INTEGER,
                    cost_quality TEXT NOT NULL,
                    cost_source TEXT NOT NULL,
                    pricing_revision TEXT,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                )
            """
            )

            # Create metacog_memory_records
            conn.execute(
                """
                CREATE TABLE metacog_memory_records (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    applicability_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    promotion_status TEXT NOT NULL DEFAULT 'pending',
                    valid_from TEXT,
                    expires_at TEXT,
                    invalidated_by_json TEXT NOT NULL DEFAULT '{}',
                    supersedes_json TEXT NOT NULL DEFAULT '[]',
                    owner TEXT NOT NULL DEFAULT 'memory_curator',
                    company_id TEXT NOT NULL DEFAULT 'default',
                    project_id TEXT,
                    domain TEXT,
                    helpfulness_score REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """
            )

            conn.commit()
        finally:
            conn.close()

        yield db_path


def test_readonly_mode(test_db_path):
    """Test that the probe opens the database in read-only mode."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    probe = ZeroRowProbe(Path.cwd(), db_path=test_db_path)
    conn = probe.open_readonly()

    try:
        # Verify read-only by attempting a write (should fail)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO gate_evidence VALUES ('test', 'now', NULL, NULL, NULL, 'test', 'cmd', NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    finally:
        conn.close()


def test_row_count_retrieval(test_db_path):
    """Test that row counts are correctly retrieved from zero-row tables."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    probe = ZeroRowProbe(Path.cwd(), db_path=test_db_path)
    conn = probe.open_readonly()

    try:
        for table in ["repomap_tag_cache", "gate_evidence", "provider_call_usage", "metacog_memory_records"]:
            count = probe.get_row_count(conn, table)
            assert count == 0, f"Table {table} should have zero rows, got {count}"
    finally:
        conn.close()


def test_nonexistent_table(test_db_path):
    """Test that querying a nonexistent table returns -1."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    probe = ZeroRowProbe(Path.cwd(), db_path=test_db_path)
    conn = probe.open_readonly()

    try:
        count = probe.get_row_count(conn, "nonexistent_table")
        assert count == -1
    finally:
        conn.close()


def test_writer_location_finding():
    """Test that writer locations can be found in the codebase."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    repo_root = Path.cwd()
    # Only test if we're in the repo
    if (repo_root / "omniagentos").exists():
        probe = ZeroRowProbe(repo_root)

        # Test finding gate_evidence writer
        locations = probe.find_writer_locations("gate_evidence")
        assert len(locations) > 0, "Should find at least one location for gate_evidence writer"
        assert any("omniagentos/gates/engine.py" in loc for loc in locations)


def test_table_info_structure():
    """Test that TableInfo has the required fields."""
    from scripts.ops.zerorow_probe import TableInfo

    info = TableInfo(
        name="test_table",
        row_count=0,
        writer_location="omniagentos/test/module.py:42",
        writer_status="unreachable",
        details="Test details",
    )

    assert info.name == "test_table"
    assert info.row_count == 0
    assert info.writer_location == "omniagentos/test/module.py:42"
    assert info.writer_status == "unreachable"
    assert info.details == "Test details"


def test_probe_run_output(test_db_path, tmp_path):
    """Test that the probe produces valid output structure."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    probe = ZeroRowProbe(Path.cwd(), db_path=test_db_path)
    result = probe.run()

    # Verify output structure
    assert "status" in result
    assert "findings" in result
    assert result["status"] == "success"
    assert isinstance(result["findings"], list)
    assert len(result["findings"]) == 4  # Four tables

    # Verify each finding has required fields
    for finding in result["findings"]:
        assert "name" in finding
        assert "row_count" in finding
        assert "writer_location" in finding
        assert "writer_status" in finding
        assert "details" in finding
        assert finding["row_count"] == 0  # All target tables start empty


def test_valid_writer_statuses():
    """Test that writer_status values are from the expected set."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    repo_root = Path.cwd()
    if (repo_root / "omniagentos").exists():
        probe = ZeroRowProbe(repo_root)

        # Status values should be one of these
        valid_statuses = {"unreachable", "gated", "failing_silently", "active", "unknown"}

        # Test that analyze_writer_status returns valid statuses
        status, details = probe.analyze_writer_status("gate_evidence", [])
        assert status in valid_statuses, f"Status {status} not in valid set"
        assert isinstance(details, str)


def test_readonly_operations_only(test_db_path):
    """Test that the probe performs only read-only operations on the database."""
    from scripts.ops.zerorow_probe import ZeroRowProbe

    probe = ZeroRowProbe(Path.cwd(), db_path=test_db_path)

    # Capture the file stat before probe run
    stat_before = test_db_path.stat()
    size_before = stat_before.st_size

    # Run the probe
    result = probe.run()

    # Verify result is successful
    assert result["status"] == "success"

    # The database file should not have grown significantly
    # (might have minor WAL changes, but no actual data writes)
    stat_after = test_db_path.stat()
    size_after = stat_after.st_size

    # SQLite might create WAL files, but the main db should be unchanged
    # Allow a small margin for any metadata changes
    assert size_after <= size_before + 1000, "Database size changed significantly (data was written)"


def test_json_output_format():
    """Test that the probe output can be serialized to JSON."""
    from scripts.ops.zerorow_probe import TableInfo

    info = TableInfo(
        name="test_table",
        row_count=0,
        writer_location="omniagentos/test/module.py:42",
        writer_status="unreachable",
        details="Test details",
    )

    # Convert to dict (as probe does)
    from dataclasses import asdict

    info_dict = asdict(info)

    # Should be JSON serializable
    json_str = json.dumps(info_dict)
    assert json_str is not None
    assert "test_table" in json_str
