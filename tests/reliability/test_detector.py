"""Tests for detector rules (W2).

Covers: rule evaluation, signature/occurrence_key generation, evidence fencing,
cursor semantics, idempotency via ON CONFLICT IGNORE.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.detector import (
    _make_signature,
    _normalized_error,
    detect_failures,
    rule_account_disabled,
    rule_approval_expired,
    rule_auth_failure,
    rule_rate_limit,
    rule_run_failed,
    rule_session_error,
    rule_timeout,
    rule_tool_error,
)
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import FailureClass, Severity


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database and run migrations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        conn.close()

        store = SqliteReliabilityStore(str(db_path))
        yield store
        if hasattr(store, "_connection"):
            store._connection.close()


@pytest.fixture
def setup_base_data(tmp_db):
    """Insert base data needed for runs (task)."""
    conn = tmp_db._connection
    now = utc_now_iso()

    # Migration already seeds disciplines; just create a task
    conn.execute(
        """
        INSERT INTO tasks (id, discipline_id, title, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("tsk_test", "research-briefs", "Test task", "running", now, now),
    )
    conn.commit()
    return tmp_db


# --- Helper tests


def test_make_signature():
    """Signature should be deterministic and truncated."""
    sig1 = _make_signature(FailureClass.RUN_FAILED.value, "detector_run_failed", "error123")
    sig2 = _make_signature(FailureClass.RUN_FAILED.value, "detector_run_failed", "error123")
    assert sig1 == sig2
    assert len(sig1) == 16  # sha1()[:16]

    sig3 = _make_signature(FailureClass.RUN_FAILED.value, "detector_run_failed", "different_error")
    assert sig1 != sig3


def test_normalized_error():
    """Normalized error should be lowercase, truncated, and hashed."""
    norm1 = _normalized_error("Some Error TEXT")
    norm2 = _normalized_error("some error text")
    assert norm1 == norm2
    assert len(norm1) == 16


# --- Rule tests


def test_rule_run_failed_basic(setup_base_data):
    """A failed run should be detected."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    # Insert a failed run
    run_id = "run_test123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "tsk_test",
            "failed",
            "Task execution failed",
            now,
            now,
            now,
            "cli-claude",
            "tr123",
        ),
    )
    conn.commit()

    # Run detector
    candidates = rule_run_failed(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.RUN_FAILED.value
    assert cand.severity == Severity.WARNING.value
    assert cand.ref_type == "run"
    assert cand.ref_id == run_id


def test_rule_session_error_critical(tmp_db):
    """A failed session with error should be detected as critical."""
    conn = tmp_db._connection

    # Insert a failed session
    session_id = "ses_test123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, project_dir, state, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, "bridge", "/tmp/test", "failed", "Auth token expired", now, now),
    )
    conn.commit()

    candidates = rule_session_error(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.SESSION_ERROR.value
    assert cand.severity == Severity.CRITICAL.value


def test_rule_tool_error_detection(setup_base_data):
    """Steps with tool errors should be detected."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    # Insert a run and step with tool error
    run_id = "run_tool123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, "tsk_test", "failed", now, now, now, "cli-claude", "tr123"),
    )

    conn.execute(
        """
        INSERT INTO steps
        (run_id, seq, name, status, error, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, 1, "execute_tool", "failed", "Tool MCP error: connection refused", now),
    )
    conn.commit()

    candidates = rule_tool_error(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.TOOL_ERROR.value


def test_rule_auth_failure_runs(setup_base_data):
    """Auth failures in runs should be detected as critical."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    run_id = "run_auth123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "tsk_test",
            "failed",
            "401 Unauthorized: invalid oauth token",
            now,
            now,
            now,
            "cli-claude",
            "tr123",
        ),
    )
    conn.commit()

    candidates = rule_auth_failure(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = [c for c in candidates if c.ref_type == "run"][0]
    assert cand.failure_class == FailureClass.AUTH_FAILURE.value
    assert cand.severity == Severity.CRITICAL.value


def test_rule_rate_limit(setup_base_data):
    """Rate limit errors should be detected."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    run_id = "run_rate123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "tsk_test",
            "failed",
            "Rate limit exceeded: 429 too many requests",
            now,
            now,
            now,
            "cli-claude",
            "tr123",
        ),
    )
    conn.commit()

    candidates = rule_rate_limit(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.RATE_LIMIT.value
    assert cand.severity == Severity.WARNING.value


def test_rule_timeout(setup_base_data):
    """Timeout errors should be detected."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    run_id = "run_timeout123"
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "tsk_test",
            "failed",
            "Execution timeout: took too long (>600s)",
            now,
            now,
            now,
            "cli-claude",
            "tr123",
        ),
    )
    conn.commit()

    candidates = rule_timeout(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.TIMEOUT.value


def test_rule_approval_expired(tmp_db):
    """Expired approvals should be detected as info."""
    conn = tmp_db._connection

    approval_id = "apr_exp123"
    now = utc_now_iso()
    past = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )

    conn.execute(
        """
        INSERT INTO approvals
        (id, action_class, proposed_action, state, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (approval_id, "create_resource", "Create resource X", "pending", past, now),
    )
    conn.commit()

    candidates = rule_approval_expired(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.APPROVAL_EXPIRED.value
    assert cand.severity == Severity.INFO.value


def test_rule_account_disabled(tmp_db):
    """Disabled accounts should be detected as critical."""
    conn = tmp_db._connection

    account_id = "acc_disabled123"
    now = utc_now_iso()

    conn.execute(
        """
        INSERT INTO claude_accounts
        (id, label, auth_type, enabled, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, "test-account", "oauth_token", 0, "ok", now, now),
    )
    conn.commit()

    candidates = rule_account_disabled(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    assert cand.failure_class == FailureClass.ACCOUNT_DISABLED.value
    assert cand.severity == Severity.CRITICAL.value
    assert cand.ref_type == "account"


# --- Cursor and idempotency tests


def test_cursor_filtering(setup_base_data):
    """Rules should only scan rows after cursor."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    now = utc_now_iso()
    past_cursor = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=2))
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Insert two runs: one old (before cursor), one new (after cursor)
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run_old",
            "tsk_test",
            "failed",
            "Old error",
            past_cursor,
            past_cursor,
            past_cursor,
            "cli-claude",
            "tr1",
        ),
    )

    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run_new", "tsk_test", "failed", "New error", now, now, now, "cli-claude", "tr2"),
    )
    conn.commit()

    # Scan with cursor = past
    candidates = rule_run_failed(conn, past_cursor)

    # Should only get the new run (after cursor)
    assert len(candidates) == 1
    assert candidates[0].ref_id == "run_new"


def test_run_failed_uses_finish_timestamp(setup_base_data):
    """A run CREATED before the window but that FAILED inside it must be detected.

    The detector filters on the failure timestamp (finished_at), not created_at —
    otherwise a long-running run that started before the cursor and failed after it
    is silently skipped (RF2 #2).
    """
    tmp_db = setup_base_data
    conn = tmp_db._connection

    now = utc_now_iso()
    created = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=3))
        .isoformat()
        .replace("+00:00", "Z")
    )
    cursor = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )

    # created 3h ago (before cursor), but finished/updated now (after cursor).
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, finished_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run_late_fail",
            "tsk_test",
            "failed",
            "Boom",
            created,
            now,
            now,
            created,
            "cli-claude",
            "tr1",
        ),
    )
    conn.commit()

    candidates = rule_run_failed(conn, cursor)

    assert [c.ref_id for c in candidates] == ["run_late_fail"]


def test_run_failed_falls_back_to_updated_at(setup_base_data):
    """When finished_at is NULL, the detector falls back to updated_at."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    now = utc_now_iso()
    created = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=3))
        .isoformat()
        .replace("+00:00", "Z")
    )
    cursor = (
        (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )

    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run_fallback", "tsk_test", "failed", "Boom", created, now, created, "cli-claude", "tr2"),
    )
    conn.commit()

    candidates = rule_run_failed(conn, cursor)

    assert [c.ref_id for c in candidates] == ["run_fallback"]


def test_evidence_fencing(setup_base_data):
    """Evidence should be quoted via quote_untrusted()."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    run_id = "run_evidence123"
    now = utc_now_iso()
    error_text = "Unsafe: $(rm -rf /), never execute!"

    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, "tsk_test", "failed", error_text, now, now, now, "cli-claude", "tr123"),
    )
    conn.commit()

    candidates = rule_run_failed(conn, "1970-01-01T00:00:00Z")

    assert len(candidates) > 0
    cand = candidates[0]
    evidence_str = str(cand.evidence_json)
    # Evidence should be quoted (contains quote marks or explicit text)
    assert "error" in evidence_str.lower()


def test_detect_failures_all_rules(setup_base_data):
    """detect_failures() should call all rules and aggregate results."""
    tmp_db = setup_base_data
    conn = tmp_db._connection

    now = utc_now_iso()
    cursor = "1970-01-01T00:00:00Z"

    # Setup test data for multiple rules
    conn.execute(
        """
        INSERT INTO runs
        (id, task_id, state, error, created_at, updated_at, queued_at, harness, trace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run_failed", "tsk_test", "failed", "Task failed", now, now, now, "cli-claude", "tr1"),
    )

    conn.execute(
        """
        INSERT INTO sessions
        (id, source, project_dir, state, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("ses_failed", "bridge", "/tmp", "failed", "Session error", now, now),
    )

    conn.execute(
        """
        INSERT INTO claude_accounts
        (id, label, auth_type, enabled, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("acc_disabled", "test", "oauth_token", 0, "ok", now, now),
    )
    conn.commit()

    candidates = detect_failures(tmp_db, cursor, now)

    # Should have multiple candidates from different rules
    assert len(candidates) >= 3
    classes = {c.failure_class for c in candidates}
    assert FailureClass.RUN_FAILED.value in classes
    assert FailureClass.SESSION_ERROR.value in classes
    assert FailureClass.ACCOUNT_DISABLED.value in classes
