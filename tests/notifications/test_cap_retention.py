"""Tests for notification per-event cap + retention sweep."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.intake.run_card_reconcile import LIFECYCLE_RECONCILE_ENV
from omniagentos.notifications import service as notif_service
from omniagentos.notifications.cap_retention import (
    check_emission_cap,
    maybe_run_retention_sweep,
    reset_cap_state,
    run_retention_sweep,
    suppression_counts,
)
from omniagentos.notifications.dal import NotificationsDal


def setup_function() -> None:
    reset_cap_state()


def test_bursty_emissions_capped_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIFECYCLE_RECONCILE_ENV, "enforce")
    monkeypatch.setenv("OMNIAGENTOS_NOTIF_CAP_N", "3")
    monkeypatch.setenv("OMNIAGENTOS_NOTIF_CAP_WINDOW_SECONDS", "3600")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_CAP_N": "3",
        "OMNIAGENTOS_NOTIF_CAP_WINDOW_SECONDS": "3600",
    }
    key = "alert:run:run_1"
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    allowed = 0
    suppressed = 0
    for i in range(6):
        cap = check_emission_cap(key, now=now + timedelta(seconds=i), env=env, mode="enforce")
        if cap.allowed:
            allowed += 1
        if cap.suppressed:
            suppressed += 1
    assert allowed == 3
    assert suppressed == 3
    assert suppression_counts()[key] == 3


def test_shadow_cap_does_not_suppress() -> None:
    env = {
        LIFECYCLE_RECONCILE_ENV: "shadow",
        "OMNIAGENTOS_NOTIF_CAP_N": "1",
        "OMNIAGENTOS_NOTIF_CAP_WINDOW_SECONDS": "3600",
    }
    key = "info:x:y"
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    first = check_emission_cap(key, now=now, env=env, mode="shadow")
    second = check_emission_cap(key, now=now + timedelta(seconds=1), env=env, mode="shadow")
    assert first.allowed is True
    assert second.allowed is True  # shadow never suppresses
    assert second.suppressed is False
    assert suppression_counts().get(key, 0) == 0


def test_retention_removes_old_read_keeps_new() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Only already-read aged rows are eligible.
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_old', ?, 'old', ?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_new', ?, 'new', ?)",
        (new, new),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }
    result = run_retention_sweep(_Dal(conn), now=now, env=env, mode="enforce")
    assert result.deleted == 1
    rows = conn.execute("SELECT id FROM notifications").fetchall()
    assert [r["id"] for r in rows] == ["ntf_new"]


def test_unread_aged_notification_survives_retention() -> None:
    """BLOCKER regression: unread aged notifications must not be deleted."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_unread_old', ?, 'keep me', NULL)",
        (old,),
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_read_old', ?, 'delete me', ?)",
        (old, old),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    result = run_retention_sweep(
        _Dal(conn),
        now=now,
        env={
            LIFECYCLE_RECONCILE_ENV: "enforce",
            "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        },
        mode="enforce",
    )
    assert result.deleted == 1
    rows = {r["id"] for r in conn.execute("SELECT id FROM notifications").fetchall()}
    assert "ntf_unread_old" in rows
    assert "ntf_read_old" not in rows


def test_retention_shadow_no_writes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_old', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    result = run_retention_sweep(
        _Dal(conn),
        now=now,
        env={LIFECYCLE_RECONCILE_ENV: "shadow", "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30"},
        mode="shadow",
    )
    assert result.mode == "shadow"
    assert result.would_delete == 1
    assert result.deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_retention_batched_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(5):
        conn.execute(
            "INSERT INTO notifications VALUES (?, ?, 'old', ?)",
            (f"ntf_{i}", old, old),
        )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "2",
    }
    first = run_retention_sweep(_Dal(conn), now=now, env=env, mode="enforce")
    assert first.deleted == 5
    assert first.batches == 3  # 2+2+1
    second = run_retention_sweep(_Dal(conn), now=now, env=env, mode="enforce")
    assert second.deleted == 0


def test_retention_noop_delete_is_not_reported_as_deleted() -> None:
    """A non-result must not present as a successful retention delete.

    Counterfeit that would fake a weaker fix: always return ``deleted=0`` (even
    when rows actually vanish), or only special-case ``connection is None``
    while still counting optimistic ``len(batch)`` after a failed/no-op delete
    on a real connection. This test requires:
    - no delete capability → ``deleted == 0`` AND explicit failed/unknown status
    - rows still present after the sweep reports
    - a working delete path still reports a truthful positive count with status ok
    - genuinely empty sweep is distinguishable from no-delete-capability failure
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class NoopDal:
        """Has list (so ids are discovered) but no connection and no delete_ids."""

        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_orphan",
                    "created_at": old,
                    "read_at": old,
                    "title": "still here",
                }
            ]

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

    class EmptyDal:
        """No eligible rows — genuine empty success."""

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    assert empty.deleted == 0
    assert empty.status == "ok", (
        f"genuine empty reported status={empty.status!r}; expected measured ok"
    )
    assert empty.error is None

    noop = NoopDal()
    failed = run_retention_sweep(noop, now=now, env=env, mode="enforce")
    assert failed.deleted == 0, (
        f"noop delete reported deleted={failed.deleted}; non-result presented as success"
    )
    assert failed.batches == 0
    assert failed.status == "failed", (
        f"no delete capability reported status={failed.status!r}; "
        "failure must not look like empty success"
    )
    assert failed.error is not None
    assert [row["id"] for row in noop.rows] == ["ntf_orphan"]
    # The governing defect: empty success and failed delete must not be identical.
    assert (empty.status, empty.error) != (failed.status, failed.error)

    # Positive control: a real connection still reports actual deletions.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_real', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    ok = run_retention_sweep(_Dal(conn), now=now, env=env, mode="enforce")
    assert ok.deleted == 1
    assert ok.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_retention_commit_failure_is_not_reported_as_deleted() -> None:
    """Commit failure after DELETE must not claim deleted rows.

    Counterfeit that would fake a weaker fix: catch commit errors and still
    return ``len(batch)``, or zero the count without rolling back / verifying
    the rows remain. Asserts both the reported count and DB truth.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Explicit transaction mode so DELETE is not auto-committed before commit().
    conn.isolation_level = "DEFERRED"
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_commit_fail', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _FailCommitConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self._fail_commit = False

        def execute(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            return self._real.execute(*args, **kwargs)

        def commit(self) -> None:
            if self._fail_commit:
                raise sqlite3.OperationalError("disk full")
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    wrapped = _FailCommitConnection(conn)

    class _Dal:
        def __init__(self, c: _FailCommitConnection) -> None:
            self._connection = c

    # Arm failure only for the delete-path commit (list path may commit too).
    # List uses SELECT which may not commit; arm permanently after setup.
    wrapped._fail_commit = True
    result = run_retention_sweep(
        _Dal(wrapped),
        now=now,
        env={
            LIFECYCLE_RECONCILE_ENV: "enforce",
            "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        },
        mode="enforce",
    )
    assert result.deleted == 0, (
        f"commit failure reported deleted={result.deleted}; non-result presented as success"
    )
    assert result.status == "failed", (
        f"commit failure reported status={result.status!r}; must not look like empty ok"
    )
    assert result.error is not None
    # Row must still be present (rollback after failed commit).
    remaining = conn.execute("SELECT id FROM notifications WHERE id = 'ntf_commit_fail'").fetchone()
    assert remaining is not None


def test_retention_custom_deleter_unreported_count_is_not_deleted() -> None:
    """Custom ``delete_ids()`` that returns no count must not invent len(ids).

    Failing-on-revert target: ``return _DeleteOutcome(deleted=len(ids))`` (or
    ``return len(ids)``) when the deleter returns ``None``/non-int.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class UnreportedCountDal:
        """List discovers ids; delete_ids exists but returns None (no count)."""

        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_uncounted",
                    "created_at": old,
                    "read_at": old,
                    "title": "still here",
                }
            ]
            self.delete_calls: list[list[str]] = []

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

        def delete_ids(self, ids: list[str]) -> None:
            # Side-effect may or may not run; without a count we cannot claim it.
            self.delete_calls.append(list(ids))
            return None

    dal = UnreportedCountDal()
    result = run_retention_sweep(dal, now=now, env=env, mode="enforce")
    assert result.deleted == 0, (
        f"unreported custom-deleter count reported deleted={result.deleted}; "
        "non-result presented as success"
    )
    assert result.status == "failed", (
        f"unreported count reported status={result.status!r}; must not look like empty ok"
    )
    assert result.error is not None
    assert [row["id"] for row in dal.rows] == ["ntf_uncounted"]


def test_retention_no_read_capability_is_not_empty_success() -> None:
    """Missing read capability must not report as a genuinely empty ok sweep.

    Governing filter: a missing/unreadable source must not be identical to a
    measured empty result. Counterfeit: ``rows = []`` when connection is None
    and no list() exists, then status stays ok with deleted=0.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyReadableDal:
        """Can list; returns no rows — genuine empty success."""

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class NoReadDal:
        """No connection and no list — cannot measure eligibility at all."""

        pass

    empty = run_retention_sweep(EmptyReadableDal(), now=now, env=env, mode="enforce")
    assert empty.status == "ok"
    assert empty.deleted == 0
    assert empty.error is None

    no_read = run_retention_sweep(NoReadDal(), now=now, env=env, mode="enforce")
    assert no_read.deleted == 0
    assert no_read.status == "failed", (
        f"no read capability reported status={no_read.status!r}; must not look like empty success"
    )
    assert no_read.error is not None
    assert (empty.status, empty.error) != (no_read.status, no_read.error), (
        "no_read_capability must not be identical to genuine empty"
    )


def test_retention_malformed_created_at_is_not_empty_success() -> None:
    """Unparseable created_at must not collapse into a measured empty ok sweep.

    Counterfeit: lexical string compare silently skips ``not-a-date`` rows so
    the sweep reports the same status/error as a genuinely empty source.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class MalformedDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return [
                {
                    "id": "ntf_bad_ts",
                    "created_at": "not-a-date",
                    "read_at": "also-not-useful",
                    "title": "unreadable timestamp",
                }
            ]

        def delete_ids(self, ids: list[str]) -> int:
            return len(ids)

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    bad = run_retention_sweep(MalformedDal(), now=now, env=env, mode="enforce")
    assert empty.status == "ok"
    assert empty.error is None
    assert bad.deleted == 0
    assert bad.status == "failed", (
        f"malformed created_at reported status={bad.status!r}; "
        "unreadable source must not look like empty ok"
    )
    assert bad.error is not None
    assert (empty.status, empty.error) != (bad.status, bad.error), (
        "malformed_created_at must not be identical to genuine empty"
    )


def test_retention_connection_malformed_created_at_is_not_empty_success() -> None:
    """Production connection path must fail on unparseable created_at.

    The list-only fake DAL is not enough: real NotificationsDal exposes
    ``_connection``, and a lexical SQL age predicate silently skips
    ``created_at='not-a-date'`` so the sweep matches a genuinely empty source
    while the garbage row remains. Counterfeit: restore
    ``SELECT id ... WHERE created_at < ?`` without parsing created_at.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    empty_dal = NotificationsDal(":memory:")
    try:
        empty = run_retention_sweep(empty_dal, now=now, env=env, mode="enforce")
    finally:
        empty_dal.close()
    assert empty.status == "ok"
    assert empty.deleted == 0
    assert empty.error is None

    bad_dal = NotificationsDal(":memory:")
    try:
        stored = bad_dal.create(
            {
                "id": "ntf_conn_bad_ts",
                "kind": "info",
                "title": "garbage timestamp",
                "created_at": "not-a-date",
                "read_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        assert stored["created_at"] == "not-a-date"
        bad = run_retention_sweep(bad_dal, now=now, env=env, mode="enforce")
        remaining = bad_dal.list(limit=10)
    finally:
        bad_dal.close()

    assert bad.deleted == 0
    assert bad.status == "failed", (
        f"connection malformed created_at reported status={bad.status!r}; "
        "production path must not look like empty ok"
    )
    assert bad.error is not None
    assert (empty.status, empty.error) != (bad.status, bad.error), (
        "connection malformed_created_at must not be identical to genuine empty"
    )
    assert any(row["id"] == "ntf_conn_bad_ts" for row in remaining), (
        "malformed row must remain when the sweep cannot measure eligibility"
    )


def test_retention_fallback_list_raises_is_not_empty_success() -> None:
    """Fallback list() raising must not report as a measured empty ok sweep.

    Failing-on-revert target (reviewer mutation):
        return _ListOutcome(ids=[])  # MUTATION: list exception as empty success
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class RaisingListDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            raise RuntimeError("list exploded")

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    failed = run_retention_sweep(RaisingListDal(), now=now, env=env, mode="enforce")
    assert empty.status == "ok"
    assert empty.error is None
    assert failed.deleted == 0
    assert failed.status == "failed", (
        f"list() raise reported status={failed.status!r}; unreadable must not look like empty ok"
    )
    assert failed.error is not None
    assert (empty.status, empty.error) != (failed.status, failed.error), (
        "list() exception must not be identical to genuine empty"
    )


def test_retention_fallback_list_returns_none_is_not_empty_success() -> None:
    """Fallback list() returning None is unmeasured, not a measured empty ok.

    Failing-on-revert target (reviewer mutation):
        return _ListOutcome(ids=[])  # MUTATION: None rows as empty success
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class NoneListDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict] | None:
            return None

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    failed = run_retention_sweep(NoneListDal(), now=now, env=env, mode="enforce")
    assert empty.status == "ok"
    assert empty.error is None
    assert failed.deleted == 0
    assert failed.status == "failed", (
        f"list() None reported status={failed.status!r}; unreadable must not look like empty ok"
    )
    assert failed.error is not None
    assert (empty.status, empty.error) != (failed.status, failed.error), (
        "list() None must not be identical to genuine empty"
    )


def test_retention_connection_select_raises_is_not_empty_success() -> None:
    """Connection SELECT raising must not report as a measured empty ok sweep.

    Failing-on-revert target (reviewer mutation):
        return _ListOutcome(ids=[])  # MUTATION: SELECT failure as empty success
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    empty_dal = NotificationsDal(":memory:")
    try:
        empty = run_retention_sweep(empty_dal, now=now, env=env, mode="enforce")
    finally:
        empty_dal.close()
    assert empty.status == "ok"
    assert empty.error is None

    class _FailSelectConnection:
        def execute(self, sql: str, params: object = ()) -> object:
            raise sqlite3.OperationalError("database is locked")

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _Dal:
        def __init__(self) -> None:
            self._connection = _FailSelectConnection()

    failed = run_retention_sweep(_Dal(), now=now, env=env, mode="enforce")
    assert failed.deleted == 0
    assert failed.status == "failed", (
        f"SELECT raise reported status={failed.status!r}; unreadable must not look like empty ok"
    )
    assert failed.error is not None
    assert (empty.status, empty.error) != (failed.status, failed.error), (
        "connection SELECT exception must not be identical to genuine empty"
    )


def test_retention_connection_missing_created_at_is_not_empty_success() -> None:
    """Connection path rows with absent/empty created_at are unmeasured, not empty ok.

    Failing-on-revert target (reviewer mutation):
        continue  # MUTATION: connection missing timestamp as empty
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    empty_dal = NotificationsDal(":memory:")
    try:
        empty = run_retention_sweep(empty_dal, now=now, env=env, mode="enforce")
    finally:
        empty_dal.close()
    assert empty.status == "ok"
    assert empty.error is None

    # NULL created_at on an already-read row: production SELECT includes it
    # (read_at IS NOT NULL) and must not skip it as "not eligible".
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    read_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_null_ts', NULL, 'no ts', ?)",
        (read_at,),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    missing = run_retention_sweep(_Dal(conn), now=now, env=env, mode="enforce")
    assert missing.deleted == 0
    assert missing.status == "failed", (
        f"connection missing created_at reported status={missing.status!r}; "
        "must not look like empty ok"
    )
    assert missing.error is not None
    assert (empty.status, empty.error) != (missing.status, missing.error), (
        "connection missing created_at must not be identical to genuine empty"
    )
    remaining = conn.execute("SELECT id FROM notifications WHERE id = 'ntf_null_ts'").fetchone()
    assert remaining is not None

    # Empty-string created_at is the same defect class.
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    conn2.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn2.execute(
        "INSERT INTO notifications VALUES ('ntf_blank_ts', '', 'blank ts', ?)",
        (read_at,),
    )
    conn2.commit()
    blank = run_retention_sweep(_Dal(conn2), now=now, env=env, mode="enforce")
    assert blank.status == "failed"
    assert blank.error is not None
    assert (empty.status, empty.error) != (blank.status, blank.error)


def test_retention_custom_deleter_overcount_is_not_ok() -> None:
    """Custom deleter returning more deletions than requested is invalid, not ok.

    Counterfeit / probe: delete_ids returns 5 for a 1-id batch while no row is
    removed, and the sweep reports deleted=5 status=ok.
    Failing-on-revert target: accept any non-negative int without capping to
    ``len(ids)``.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class OvercountDal:
        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_over",
                    "created_at": old,
                    "read_at": old,
                    "title": "still here",
                }
            ]

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

        def delete_ids(self, ids: list[str]) -> int:
            # Impossible: claims more deletes than candidates, removes nothing.
            return len(ids) + 4

    dal = OvercountDal()
    result = run_retention_sweep(dal, now=now, env=env, mode="enforce")
    assert result.deleted == 0, (
        f"overcount custom-deleter reported deleted={result.deleted}; "
        "impossible count must not invent a favourable total"
    )
    assert result.status == "failed", (
        f"overcount reported status={result.status!r}; must not look like measured ok"
    )
    assert result.error is not None
    assert [row["id"] for row in dal.rows] == ["ntf_over"]


def test_retention_connection_overcount_is_not_ok() -> None:
    """Connection cursor rowcount > requested ids is invalid, not status=ok.

    Custom-deleter overcount is covered separately. This binds the *connection*
    DELETE path (reviewer probe): list succeeds with one eligible id, but the
    DELETE cursor claims rowcount=5. That must not invent deleted=5 status=ok.

    Failing-on-revert target: accept any non-negative rowcount without
    ``n > len(ids)`` rejection after commit.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class _Cursor:
        def __init__(self, *, rows: list | None = None, rowcount: int = 0) -> None:
            self._rows = rows or []
            self.rowcount = rowcount

        def fetchall(self) -> list:
            return list(self._rows)

    class _OvercountConnection:
        """SELECT returns one eligible row; DELETE reports impossible rowcount=5."""

        def execute(self, sql: str, params: object = ()) -> _Cursor:
            upper = sql.lstrip().upper()
            if upper.startswith("SELECT"):
                return _Cursor(rows=[("ntf_conn_over", old, old)])
            if upper.startswith("DELETE"):
                # Counterfeit: claims 5 deletes for the single requested id.
                return _Cursor(rowcount=5)
            raise AssertionError(f"unexpected SQL: {sql!r}")

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _OvercountConnDal:
        def __init__(self) -> None:
            self._connection = _OvercountConnection()

    result = run_retention_sweep(_OvercountConnDal(), now=now, env=env, mode="enforce")
    assert result.deleted == 0, (
        f"connection overcount reported deleted={result.deleted}; "
        "impossible rowcount must not invent a favourable total "
        f"(status={result.status!r} error={result.error!r})"
    )
    assert result.status == "failed", (
        f"connection overcount reported status={result.status!r}; must not look like measured ok"
    )
    assert result.error is not None
    assert result.error == "invalid_delete_count", (
        f"expected invalid_delete_count, got error={result.error!r}"
    )
    # The exact reviewer shape: deleted must never exceed the request size.
    assert result.deleted <= 1


def test_retention_fallback_missing_created_at_is_not_empty_success() -> None:
    """List-only rows with absent/empty created_at are unmeasured, not empty ok.

    Counterfeit: ``continue`` on missing/empty timestamps treats them as
    ineligible and reports the same status/error as a genuinely empty source.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class MissingCreatedAtDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return [
                {
                    "id": "ntf_no_ts",
                    "read_at": "2026-01-01T00:00:00Z",
                    "title": "missing created_at",
                }
            ]

        def delete_ids(self, ids: list[str]) -> int:
            return len(ids)

    class EmptyCreatedAtDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return [
                {
                    "id": "ntf_empty_ts",
                    "created_at": "",
                    "read_at": "2026-01-01T00:00:00Z",
                    "title": "empty created_at",
                }
            ]

        def delete_ids(self, ids: list[str]) -> int:
            return len(ids)

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    missing = run_retention_sweep(MissingCreatedAtDal(), now=now, env=env, mode="enforce")
    blank = run_retention_sweep(EmptyCreatedAtDal(), now=now, env=env, mode="enforce")

    assert empty.status == "ok"
    assert empty.error is None

    assert missing.deleted == 0
    assert missing.status == "failed", (
        f"missing created_at reported status={missing.status!r}; must not look like empty ok"
    )
    assert missing.error is not None
    assert (empty.status, empty.error) != (missing.status, missing.error), (
        "fallback missing created_at must not be identical to genuine empty"
    )

    assert blank.deleted == 0
    assert blank.status == "failed", (
        f"empty created_at reported status={blank.status!r}; must not look like empty ok"
    )
    assert blank.error is not None
    assert (empty.status, empty.error) != (blank.status, blank.error), (
        "fallback empty created_at must not be identical to genuine empty"
    )


def test_retention_negative_custom_delete_count_is_not_ok_zero() -> None:
    """Custom deleter returning a negative count is unmeasured, not deleted=0 ok.

    Counterfeit: ``return _DeleteOutcome(deleted=max(0, result))`` clamps -5 to
    0 and leaves status ok — invalid count presented as measured empty success.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class NegativeCountDal:
        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_neg",
                    "created_at": old,
                    "read_at": old,
                    "title": "still here",
                }
            ]

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

        def delete_ids(self, ids: list[str]) -> int:
            return -1

    dal = NegativeCountDal()
    result = run_retention_sweep(dal, now=now, env=env, mode="enforce")
    assert result.deleted == 0, (
        f"negative custom-deleter count reported deleted={result.deleted}; "
        "invalid count must not invent a favourable total"
    )
    assert result.status == "failed", (
        f"negative count reported status={result.status!r}; "
        "must not look like measured ok with deleted=0"
    )
    assert result.error is not None
    assert [row["id"] for row in dal.rows] == ["ntf_neg"]


def test_retention_unknown_rowcount_is_not_deleted() -> None:
    """Cursor rowcount None/negative must not invent len(ids) as deleted.

    Failing-on-revert target: treat unknown rowcount as ``return len(ids)``.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_rowcount', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _UnknownRowcountConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql: str, params: object = ()) -> object:
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                # Run the real delete so the row is gone — a counterfeit that
                # returns len(ids) would still claim success; we require status
                # failed because the count was unmeasured, and we also accept
                # deleted=0 even if the driver deleted underneath.
                class _Unknown:
                    rowcount = None

                # Do not actually delete: unknown count must not claim deletion.
                return _Unknown()
            return self._real.execute(sql, params)

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    wrapped = _UnknownRowcountConnection(conn)

    class _Dal:
        def __init__(self, c: _UnknownRowcountConnection) -> None:
            self._connection = c

    result = run_retention_sweep(
        _Dal(wrapped),
        now=now,
        env={
            LIFECYCLE_RECONCILE_ENV: "enforce",
            "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        },
        mode="enforce",
    )
    assert result.deleted == 0, (
        f"unknown rowcount reported deleted={result.deleted}; non-result as success"
    )
    assert result.status == "failed", (
        f"unknown rowcount reported status={result.status!r}; must not look like empty ok"
    )
    assert result.error is not None
    remaining = conn.execute("SELECT id FROM notifications WHERE id = 'ntf_rowcount'").fetchone()
    assert remaining is not None


def test_retention_execute_failure_is_not_reported_as_deleted() -> None:
    """DELETE execute raising must not invent len(ids) as deleted.

    Failing-on-revert target: ``except Exception: return len(ids)``.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_exec_fail', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _FailExecuteConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql: str, params: object = ()) -> object:
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, params)

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    wrapped = _FailExecuteConnection(conn)

    class _Dal:
        def __init__(self, c: _FailExecuteConnection) -> None:
            self._connection = c

    result = run_retention_sweep(
        _Dal(wrapped),
        now=now,
        env={
            LIFECYCLE_RECONCILE_ENV: "enforce",
            "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        },
        mode="enforce",
    )
    assert result.deleted == 0, (
        f"execute failure reported deleted={result.deleted}; non-result as success"
    )
    assert result.status == "failed", (
        f"execute failure reported status={result.status!r}; must not look like empty ok"
    )
    assert result.error is not None
    remaining = conn.execute("SELECT id FROM notifications WHERE id = 'ntf_exec_fail'").fetchone()
    assert remaining is not None


def test_retention_partial_batch_does_not_name_undeleted_ids() -> None:
    """Failed earlier batch must not appear in result.ids when a later batch succeeds.

    Counterfeit: ``ids=list(old_ids[:deleted])`` names the candidate prefix even
    when the first batch was a no-op. Require ids to be a subset of rows that
    are actually gone.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    for nid in ("ntf_a", "ntf_b"):
        conn.execute(
            "INSERT INTO notifications VALUES (?, ?, 'old', ?)",
            (nid, old, old),
        )
    conn.commit()

    class _SelectiveConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self._delete_calls = 0

        def execute(self, sql: str, params: object = ()) -> object:
            if isinstance(sql, str) and sql.strip().upper().startswith("DELETE"):
                self._delete_calls += 1
                if self._delete_calls == 1:
                    # First batch: pretend success path returns 0 (no-op).
                    class _Zero:
                        rowcount = 0

                    return _Zero()
            return self._real.execute(sql, params)

        def commit(self) -> None:
            self._real.commit()

        def rollback(self) -> None:
            self._real.rollback()

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    wrapped = _SelectiveConnection(conn)

    class _Dal:
        def __init__(self, c: _SelectiveConnection) -> None:
            self._connection = c

    result = run_retention_sweep(
        _Dal(wrapped),
        now=now,
        env={
            LIFECYCLE_RECONCILE_ENV: "enforce",
            "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
            "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "1",
        },
        mode="enforce",
    )
    assert result.deleted == 1
    remaining = {r["id"] for r in conn.execute("SELECT id FROM notifications").fetchall()}
    # The named ids must not include any row that remains.
    for named in result.ids:
        assert named not in remaining, (
            f"result.ids names {named!r} but that row remains; "
            f"ids={result.ids!r} remaining={sorted(remaining)!r}"
        )
    assert "ntf_a" in remaining  # first batch no-op
    assert "ntf_b" not in remaining  # second batch deleted


def test_maybe_run_retention_sweep_is_production_callable() -> None:
    """maybe_run_retention_sweep is the production entry; throttles and runs."""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE notifications (id TEXT PRIMARY KEY, created_at TEXT, title TEXT, read_at TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications VALUES ('ntf_old', ?, 'old', ?)",
        (old, old),
    )
    conn.commit()

    class _Dal:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._connection = c

    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
    }
    first = maybe_run_retention_sweep(
        _Dal(conn), now=now, env=env, mode="enforce", min_interval_seconds=3600
    )
    assert first is not None
    assert first.deleted == 1
    # Immediately again: throttled → None (not a fake successful delete).
    second = maybe_run_retention_sweep(
        _Dal(conn),
        now=now + timedelta(seconds=1),
        env=env,
        mode="enforce",
        min_interval_seconds=3600,
    )
    assert second is None


def test_record_notification_wires_retention_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production path: record_notification_result invokes retention sweep.

    Counterfeit: export maybe_run_retention_sweep but never call it from a
    production module. This binds the write seam to the sweep.
    """
    import omniagentos.notifications.cap_retention as cap_mod

    calls: list[object] = []

    def _spy(dal: object, **_kwargs: object) -> object:
        calls.append(dal)
        return None

    monkeypatch.setenv(LIFECYCLE_RECONCILE_ENV, "enforce")
    # Local import inside record_notification_result resolves this attribute.
    monkeypatch.setattr(cap_mod, "maybe_run_retention_sweep", _spy)

    dal = NotificationsDal(str(tmp_path / "wire.db"))
    result = notif_service.record_notification_result(
        kind="info",
        title="wire-retention",
        dal=dal,
        push=False,
    )
    assert result.status == "persisted"
    assert len(calls) >= 1, "record_notification_result did not call maybe_run_retention_sweep"


def test_retention_bool_custom_deleter_count_is_not_deleted() -> None:
    """Custom deleter returning bool must not invent a truthful delete count.

    ``bool`` is an ``int`` subclass, so ``isinstance(True, int)`` is True and
    ``True`` becomes deleted=1 even when no row was removed. Counterfeit:
    accept bool as a measured count. Failing-on-revert target: drop the
    ``not isinstance(result, bool)`` guard.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class BoolDeleterDal:
        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_bool",
                    "created_at": old,
                    "read_at": old,
                    "title": "still here",
                }
            ]

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

        def delete_ids(self, ids: list[str]) -> bool:
            # Success marker only — removes nothing and returns no real count.
            return True

    dal = BoolDeleterDal()
    result = run_retention_sweep(dal, now=now, env=env, mode="enforce")
    assert result.deleted == 0, (
        f"bool deleter reported deleted={result.deleted}; "
        "boolean success marker must not invent a confirmed deletion count"
    )
    assert result.status == "failed", (
        f"bool deleter reported status={result.status!r}; must not look like measured ok"
    )
    assert result.error is not None
    assert result.ids == [] or "ntf_bool" not in result.ids
    assert [row["id"] for row in dal.rows] == ["ntf_bool"], (
        "bool deleter must not claim a row is gone when it remains"
    )


def test_retention_malformed_read_at_is_not_eligible() -> None:
    """Unparseable read_at must not become favourable retention eligibility.

    Governing filter: only aged *and already-read* rows may be deleted.
    ``read_at='not-a-date'`` is not trustworthy evidence of a read. Counterfeit:
    ``if read_at is not None and str(read_at):`` truthiness alone.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }

    class EmptyDal:
        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return []

    class MalformedReadAtDal:
        def __init__(self) -> None:
            self.rows = [
                {
                    "id": "ntf_bad_read",
                    "created_at": old,
                    "read_at": "not-a-date",
                    "title": "garbage read_at",
                }
            ]
            self.delete_calls: list[list[str]] = []

        def list(self, *, limit: int = 1000, unread_only: bool = False) -> list[dict]:
            return list(self.rows)[:limit]

        def delete_ids(self, ids: list[str]) -> int:
            self.delete_calls.append(list(ids))
            self.rows = [r for r in self.rows if r["id"] not in ids]
            return len(ids)

    empty = run_retention_sweep(EmptyDal(), now=now, env=env, mode="enforce")
    dal = MalformedReadAtDal()
    bad = run_retention_sweep(dal, now=now, env=env, mode="enforce")

    assert empty.status == "ok"
    assert empty.error is None

    assert bad.deleted == 0, (
        f"malformed read_at reported deleted={bad.deleted}; "
        "unparseable timestamp must not become favourable eligibility"
    )
    assert bad.status == "failed", (
        f"malformed read_at reported status={bad.status!r}; "
        "must not look like measured empty/ok success"
    )
    assert bad.error is not None
    assert (empty.status, empty.error) != (bad.status, bad.error), (
        "malformed_read_at must not be identical to genuine empty"
    )
    assert dal.delete_calls == [], "malformed read_at must not reach the deleter"
    assert [row["id"] for row in dal.rows] == ["ntf_bad_read"]


def test_retention_connection_malformed_read_at_is_not_eligible() -> None:
    """Production connection path must not delete on unparseable read_at.

    SQL ``WHERE read_at IS NOT NULL`` treats any non-null string as read,
    including ``not-a-date``. Real NotificationsDal must fail measurement
    rather than delete. Counterfeit: SELECT without parsing read_at.
    """
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    env = {
        LIFECYCLE_RECONCILE_ENV: "enforce",
        "OMNIAGENTOS_NOTIF_RETENTION_DAYS": "30",
        "OMNIAGENTOS_NOTIF_RETENTION_BATCH": "50",
    }
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

    empty_dal = NotificationsDal(":memory:")
    try:
        empty = run_retention_sweep(empty_dal, now=now, env=env, mode="enforce")
    finally:
        empty_dal.close()
    assert empty.status == "ok"
    assert empty.deleted == 0
    assert empty.error is None

    bad_dal = NotificationsDal(":memory:")
    try:
        stored = bad_dal.create(
            {
                "id": "ntf_bad_read",
                "kind": "info",
                "title": "garbage read_at",
                "created_at": old,
                "read_at": "not-a-date",
            }
        )
        assert stored["read_at"] == "not-a-date"
        bad = run_retention_sweep(bad_dal, now=now, env=env, mode="enforce")
        remaining = bad_dal.list(limit=10)
    finally:
        bad_dal.close()

    assert bad.deleted == 0, (
        f"connection malformed read_at reported deleted={bad.deleted}; "
        "unparseable read_at must not become a confirmed deletion"
    )
    assert bad.status == "failed", (
        f"connection malformed read_at reported status={bad.status!r}; "
        "production path must not look like ok with a deleted row"
    )
    assert bad.error is not None
    assert (empty.status, empty.error) != (bad.status, bad.error), (
        "connection malformed_read_at must not be identical to genuine empty"
    )
    assert any(row["id"] == "ntf_bad_read" for row in remaining), (
        "malformed read_at row must remain when eligibility cannot be measured"
    )
