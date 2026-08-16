"""Tests for the recovery module (W2, RF2).

Covers honest recovery semantics (codex #4): recovery never reports success without a
persisted record. A recoverable transient event either RECOVERS (a safe requeue action
succeeded, event → 'recovered') or is ESCALATED (no safe action, event → 'proposed' with
the reason recorded). Non-recoverable classes are never claimed. CAS via claim_recovery
guarantees a single worker acts per event.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.recovery import attempt_recovery, run_recovery_cycle
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import EventStatus, FailureClass, Severity


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


def _ok_requeue(store, event):
    """A mock safe-recovery action that always succeeds."""
    return {"ok": True, "run_id": "run_followup_1"}


def _no_action_requeue(store, event):
    """A mock action that finds nothing safe to do."""
    return {"ok": False, "reason": "test_no_safe_action"}


def _rate_limit_event(store, key="key_rl_1"):
    return store.insert_reliability_event(
        failure_class=FailureClass.RATE_LIMIT.value,
        severity=Severity.WARNING.value,
        signature="sig_rl",
        occurrence_key=key,
        source="detector_rate_limit",
        ref_type="run",
        ref_id="run_123",
    )


def test_attempt_recovery_recovers_with_action(tmp_db):
    """A recoverable event with a successful action → 'recovered' + durable record."""
    event_id = _rate_limit_event(tmp_db)
    event = tmp_db.get_event(event_id)

    result = attempt_recovery(tmp_db, event, requeue_fn=_ok_requeue)

    assert result is True
    after = tmp_db.get_event(event_id)
    assert after.status == EventStatus.RECOVERED.value
    # HONEST: the action is durably recorded (codex #4).
    assert after.recovery_json.get("action") == "requeue"
    assert after.recovery_json.get("recovery_count") == 1
    assert after.recovery_json.get("new_run_id") == "run_followup_1"


def test_attempt_recovery_no_safe_action_escalates(tmp_db):
    """A recoverable event with no safe action → 'proposed' with reason recorded."""
    event_id = _rate_limit_event(tmp_db, key="key_rl_noact")
    event = tmp_db.get_event(event_id)

    result = attempt_recovery(tmp_db, event, requeue_fn=_no_action_requeue)

    assert result is False
    after = tmp_db.get_event(event_id)
    assert after.status == EventStatus.PROPOSED.value
    assert after.recovery_json.get("no_safe_action") == "test_no_safe_action"
    assert after.recovery_json.get("recovery_count") == 0


def test_attempt_recovery_timeout(tmp_db):
    """TIMEOUT is a recoverable class."""
    event_id = tmp_db.insert_reliability_event(
        failure_class=FailureClass.TIMEOUT.value,
        severity=Severity.WARNING.value,
        signature="sig_timeout",
        occurrence_key="key_timeout_1",
        source="detector_timeout",
        ref_type="run",
        ref_id="run_456",
    )
    event = tmp_db.get_event(event_id)
    assert attempt_recovery(tmp_db, event, requeue_fn=_ok_requeue) is True
    assert tmp_db.get_event(event_id).status == EventStatus.RECOVERED.value


def test_attempt_recovery_non_recoverable(tmp_db):
    """Non-recoverable classes are never claimed and stay 'open'."""
    event_id = tmp_db.insert_reliability_event(
        failure_class=FailureClass.AUTH_FAILURE.value,
        severity=Severity.CRITICAL.value,
        signature="sig_auth",
        occurrence_key="key_auth_1",
        source="detector_auth_failure",
        ref_type="session",
        ref_id="ses_789",
    )
    event = tmp_db.get_event(event_id)

    result = attempt_recovery(tmp_db, event, requeue_fn=_ok_requeue)

    assert result is False
    assert tmp_db.get_event(event_id).status == EventStatus.OPEN.value


def test_never_reports_success_without_record(tmp_db):
    """No recoverable event may end 'open' after an attempt (codex #4)."""
    event_id = _rate_limit_event(tmp_db, key="key_record")
    event = tmp_db.get_event(event_id)
    attempt_recovery(tmp_db, event, requeue_fn=_no_action_requeue)
    after = tmp_db.get_event(event_id)
    assert after.status != EventStatus.OPEN.value
    assert after.recovery_json  # a durable record was persisted


def test_claim_recovery_cas_exclusive(tmp_db):
    """claim_recovery() CAS should ensure only one worker acts."""
    event_id = _rate_limit_event(tmp_db, key="key_cas_1")
    assert tmp_db.claim_recovery(event_id) is True
    assert tmp_db.claim_recovery(event_id) is False


def test_attempt_recovery_idempotent(tmp_db):
    """Attempting recovery twice is idempotent: the second attempt returns False."""
    event_id = _rate_limit_event(tmp_db, key="key_idem")
    event = tmp_db.get_event(event_id)

    assert attempt_recovery(tmp_db, event, requeue_fn=_ok_requeue) is True

    # Reload — the event has left 'open', so a second claim cannot win.
    event2 = tmp_db.get_event(event_id)
    assert attempt_recovery(tmp_db, event2, requeue_fn=_ok_requeue) is False


def test_recovery_cycle_summary(tmp_db):
    """run_recovery_cycle() processes every open transient event honestly."""
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.RATE_LIMIT.value,
        severity=Severity.WARNING.value,
        signature="sig_rl1",
        occurrence_key="key_rl1",
        source="detector_rate_limit",
        ref_type="run",
        ref_id="run_a",
    )
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.TIMEOUT.value,
        severity=Severity.WARNING.value,
        signature="sig_to1",
        occurrence_key="key_to1",
        source="detector_timeout",
        ref_type="run",
        ref_id="run_b",
    )
    # Non-recoverable event → skipped.
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.AUTH_FAILURE.value,
        severity=Severity.CRITICAL.value,
        signature="sig_auth1",
        occurrence_key="key_auth1",
        source="detector_auth_failure",
    )

    summary = run_recovery_cycle(tmp_db, requeue_fn=_ok_requeue)

    assert summary["recovered_count"] == 2
    assert summary["skipped_count"] == 1
    assert summary["proposed_count"] == 0
    assert summary["error_count"] == 0


def test_recovery_cycle_escalates_when_no_action(tmp_db):
    """Recoverable events with no safe action are escalated (proposed), not lost."""
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.RATE_LIMIT.value,
        severity=Severity.WARNING.value,
        signature="sig_rl2",
        occurrence_key="key_rl2",
        source="detector_rate_limit",
    )
    summary = run_recovery_cycle(tmp_db, requeue_fn=_no_action_requeue)
    assert summary["recovered_count"] == 0
    assert summary["proposed_count"] == 1


def test_default_requeue_no_run_ref_is_no_action(tmp_db):
    """The real default requeue records 'no_run_ref' when the event has no run ref."""
    event_id = tmp_db.insert_reliability_event(
        failure_class=FailureClass.RATE_LIMIT.value,
        severity=Severity.WARNING.value,
        signature="sig_noref",
        occurrence_key="key_noref",
        source="detector_rate_limit",
    )
    event = tmp_db.get_event(event_id)
    # No requeue_fn → the real _default_requeue runs; with no run ref it is a no-op.
    result = attempt_recovery(tmp_db, event)
    assert result is False
    after = tmp_db.get_event(event_id)
    assert after.status == EventStatus.PROPOSED.value
    assert after.recovery_json.get("no_safe_action") == "no_run_ref"
