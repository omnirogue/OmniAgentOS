"""P1.4: the T-24h approval reminder.

An approval notification fires once, at creation. If the operator misses it,
nothing ever mentions the approval again -- it just expires and the gated work
is voided. These tests pin the reminder that closes that gap, and specifically
the two ways a naive implementation loses it:

* the write seam's ``(ref_type, ref_id)`` dedupe swallows a re-fire while the
  ORIGINAL row is still unread -- which is exactly the state a reminder exists
  for -- so the reminder must carry a distinct ref; and
* the guard must be READ-agnostic, or a reminder the operator has already read
  is re-sent on the next tick, forever.

The review round added a third: the guard and the insert must be ONE
transaction. Two sweeps overlapping otherwise both read "no reminder yet" and
both write one, which is the same double-buzz the rail exists to prevent.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import new_id
from omniagentos.db.store import SqliteStore
from omniagentos.notifications import approval_reminders
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.notifications.service import (
    ACTIONABLE_KINDS,
    PUSH_KINDS,
    resolve_target,
    should_push,
)
from tests.support.db_template import make_store

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _approval(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": overrides.pop("id", new_id("apr")),
        "state": "pending",
        "action_class": "consequential",
        "proposed_action": "Delete the production bucket",
        "expires_at": _iso(NOW + timedelta(hours=6)),
        "session_id": "ses_1",
        "run_id": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def dal(tmp_path: Path) -> NotificationsDal:
    return NotificationsDal(str(tmp_path / "reminders.db"))


# --- window selection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (_iso(NOW + timedelta(hours=1)), True),
        (_iso(NOW + timedelta(hours=23, minutes=59)), True),
        (_iso(NOW + timedelta(hours=24)), True),  # inclusive upper edge
        (_iso(NOW + timedelta(hours=24, seconds=1)), False),
        (_iso(NOW + timedelta(days=7)), False),
        (_iso(NOW), False),  # already at the deadline, not "T-24h"
        (_iso(NOW - timedelta(hours=1)), False),  # lapsed, not imminent
        (None, False),
        ("", False),
        ("not-a-timestamp", False),
    ],
)
def test_is_expiring_soon_window(expires_at: str | None, expected: bool) -> None:
    assert approval_reminders.is_expiring_soon(expires_at, NOW) is expected


def test_naive_timestamp_is_read_as_utc() -> None:
    naive = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    assert approval_reminders.is_expiring_soon(naive, NOW) is True


# --- once and only once -------------------------------------------------------


def test_reminder_fires_once_and_only_once(dal: NotificationsDal) -> None:
    approval = _approval(id="apr_once")

    first = approval_reminders.remind_expiring_approvals([approval], dal=dal, now=NOW)
    second = approval_reminders.remind_expiring_approvals([approval], dal=dal, now=NOW)

    assert first == {"considered": 1, "reminded": 1, "already_reminded": 0, "failed": 0}
    assert second == {"considered": 1, "reminded": 0, "already_reminded": 1, "failed": 0}
    rows = [row for row in dal.list() if row["ref_type"] == approval_reminders.REMINDER_REF_TYPE]
    assert len(rows) == 1, f"reminder emitted more than once: {rows!r}"


def test_guard_is_evaluated_inside_the_insert_transaction(
    dal: NotificationsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review fix: check and insert must be ONE transaction, not two.

    Pinned structurally rather than by timing -- if the once-only read happens
    while the connection is NOT already in a transaction, there is a window in
    which a second sweep can read the same "no reminder yet" and both insert.
    """
    observed: list[bool] = []
    real_has = dal.has_for_ref_kind

    def spy(ref_type: str, ref_id: str, kind: str) -> bool:
        observed.append(dal._connection.in_transaction)  # noqa: SLF001 - the point of the test
        return real_has(ref_type, ref_id, kind)

    monkeypatch.setattr(dal, "has_for_ref_kind", spy)

    approval_reminders.remind_expiring_approvals([_approval(id="apr_tx")], dal=dal, now=NOW)

    assert observed == [True], (
        "the once-only guard ran outside the insert transaction -- "
        "check-then-insert is racy (two sweeps can both remind)"
    )


def test_concurrent_sweeps_emit_exactly_one_reminder(tmp_path: Path) -> None:
    """Two sweeps racing on the same approval, each on its own connection.

    Best-effort simulation of the real overlap (a re-entered steward cycle, a
    manual sweep, a restarted host racing the running one). ``BEGIN IMMEDIATE``
    makes the loser wait for the winner's commit, so its guard then SEES the
    reminder and declines instead of writing a second one.
    """
    database = str(tmp_path / "race.db")
    # Both DALs are built (and therefore migrated) before any thread starts.
    dals = [NotificationsDal(database), NotificationsDal(database)]
    approval = _approval(id="apr_race")
    barrier = threading.Barrier(len(dals))
    summaries: list[dict[str, int]] = []
    lock = threading.Lock()

    def sweep(sweeper: NotificationsDal) -> None:
        barrier.wait(timeout=10)
        summary = approval_reminders.remind_expiring_approvals([approval], dal=sweeper, now=NOW)
        with lock:
            summaries.append(summary)

    threads = [threading.Thread(target=sweep, args=(sweeper,)) for sweeper in dals]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    rows = [
        row for row in dals[0].list() if row["ref_type"] == approval_reminders.REMINDER_REF_TYPE
    ]
    assert len(rows) == 1, f"concurrent sweeps double-reminded: {rows!r}"
    assert sum(summary["reminded"] for summary in summaries) == 1
    assert sum(summary["already_reminded"] for summary in summaries) == 1
    assert sum(summary["failed"] for summary in summaries) == 0


def test_reminder_is_not_re_sent_after_being_read(dal: NotificationsDal) -> None:
    """Read-agnostic guard: an acknowledged reminder must not come back."""
    approval = _approval(id="apr_read")
    approval_reminders.remind_expiring_approvals([approval], dal=dal, now=NOW)
    reminder = next(
        row for row in dal.list() if row["ref_type"] == approval_reminders.REMINDER_REF_TYPE
    )
    dal.mark_read(reminder["id"])

    later = approval_reminders.remind_expiring_approvals(
        [approval], dal=dal, now=NOW + timedelta(hours=1)
    )

    assert later["reminded"] == 0
    assert later["already_reminded"] == 1
    assert len(dal.list()) == 1


def test_reminder_survives_the_unread_original_notification(dal: NotificationsDal) -> None:
    """The known gotcha: the seam's ref dedupe must not eat the reminder.

    An UNREAD ``approval``/``apr_x`` row is precisely the state that makes a
    reminder necessary; a reminder written against the same ref would be
    silently deduped away (and worse, would still fire its banner).
    """
    from omniagentos.notifications import service

    approval = _approval(id="apr_gotcha")
    service.notify_approval_requested(
        approval_id="apr_gotcha",
        proposed_action="Delete the production bucket",
        action_class="consequential",
        source="runner",
        dal=dal,
        push=False,
    )

    summary = approval_reminders.remind_expiring_approvals([approval], dal=dal, now=NOW)

    assert summary["reminded"] == 1, "reminder was swallowed by the original row's dedupe"
    kinds = sorted(row["ref_type"] for row in dal.list())
    assert kinds == ["approval", "approval_reminder"]


# --- selection rules ----------------------------------------------------------


def test_only_pending_approvals_are_reminded(dal: NotificationsDal) -> None:
    decided = [
        _approval(id="apr_ok", state="approved"),
        _approval(id="apr_no", state="rejected"),
        _approval(id="apr_gone", state="expired"),
    ]
    summary = approval_reminders.remind_expiring_approvals(decided, dal=dal, now=NOW)
    assert summary == {"considered": 0, "reminded": 0, "already_reminded": 0, "failed": 0}
    assert dal.list() == []


def test_approval_without_expiry_is_left_alone(dal: NotificationsDal) -> None:
    summary = approval_reminders.remind_expiring_approvals(
        [_approval(id="apr_forever", expires_at=None)], dal=dal, now=NOW
    )
    assert summary["reminded"] == 0
    assert dal.list() == []


def test_one_bad_row_does_not_stop_the_sweep(
    dal: NotificationsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_has = dal.has_for_ref_kind

    def flaky(ref_type: str, ref_id: str, kind: str) -> bool:
        if ref_id == "apr_bad":
            raise RuntimeError("dedupe read exploded")
        return real_has(ref_type, ref_id, kind)

    monkeypatch.setattr(dal, "has_for_ref_kind", flaky)

    summary = approval_reminders.remind_expiring_approvals(
        [_approval(id="apr_bad"), _approval(id="apr_good")], dal=dal, now=NOW
    )

    assert summary["failed"] == 1
    assert summary["reminded"] == 1


# --- the reminder is actionable ----------------------------------------------


def test_reminder_row_is_actionable_and_deliverable(dal: NotificationsDal) -> None:
    approval_reminders.remind_expiring_approvals([_approval(id="apr_live")], dal=dal, now=NOW)
    row = dal.list()[0]

    assert row["kind"] in ACTIONABLE_KINDS, "reminder does not count in the actionable badge"
    assert row["kind"] in PUSH_KINDS, "reminder would not reach the device"
    assert should_push(row["kind"], row["severity"]) is True

    target = resolve_target(dict(row), lambda _id: {"id": "apr_live", "state": "pending"})
    assert target["approval_id"] == "apr_live"
    assert target["href"] == "/approvals"
    assert target["actionable"] is True


def test_reminder_body_names_the_deadline(dal: NotificationsDal) -> None:
    approval_reminders.remind_expiring_approvals([_approval(id="apr_body")], dal=dal, now=NOW)
    row = dal.list()[0]
    assert _iso(NOW + timedelta(hours=6)) in row["body"]
    assert "Delete the production bucket" in row["body"]


# --- host wiring (steward alert monitor, NOT routines_tick) -------------------


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "monitor.db")


def _seed_pending_approval(database: SqliteStore, *, expires_at: str) -> str:
    approval_id = new_id("apr")
    database._connection.execute(
        "INSERT INTO approvals (id, action_class, proposed_action, state, expires_at, created_at)"
        " VALUES (?, 'consequential', 'Delete the production bucket', 'pending', ?, ?)",
        (approval_id, expires_at, _iso(NOW - timedelta(days=1))),
    )
    database._connection.commit()
    return approval_id


def test_remind_from_store_reads_and_writes_the_same_database(database: SqliteStore) -> None:
    approval_id = _seed_pending_approval(database, expires_at=_iso(NOW + timedelta(hours=3)))
    _seed_pending_approval(database, expires_at=_iso(NOW + timedelta(days=10)))

    summary = approval_reminders.remind_from_store(database, now=NOW)

    assert summary["reminded"] == 1
    rows = NotificationsDal(database._connection).list()
    assert [(row["ref_type"], row["ref_id"]) for row in rows] == [
        (approval_reminders.REMINDER_REF_TYPE, approval_id)
    ]


def test_monitor_cycle_emits_the_reminder(database: SqliteStore) -> None:
    """The periodic host is the steward alert monitor (never routines_tick)."""
    from omniagentos.steward.alerts import monitor
    from omniagentos.steward.config import AlertsConfig, StewardConfig

    approval_id = _seed_pending_approval(database, expires_at=_iso(NOW + timedelta(hours=2)))
    config = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=["urgent"]))

    summary = monitor.monitor_once(database, cfg=config, now=NOW)

    # The Summary contract is unchanged — the reminder is not folded into it.
    assert set(summary) == {"evaluated", "created", "suppressed", "triaged"}
    rows = NotificationsDal(database._connection).list()
    assert [(row["ref_type"], row["ref_id"]) for row in rows] == [
        (approval_reminders.REMINDER_REF_TYPE, approval_id)
    ]


def test_monitor_cycle_survives_a_failing_reminder_sweep(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.notifications import approval_reminders as reminders_module
    from omniagentos.steward.alerts import monitor
    from omniagentos.steward.config import AlertsConfig, StewardConfig

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("approvals table unreadable")

    monkeypatch.setattr(reminders_module, "remind_from_store", boom)
    config = StewardConfig(alerts=AlertsConfig(vip_senders=[], urgent_patterns=["urgent"]))

    summary = monitor.monitor_once(database, cfg=config, now=NOW)

    assert summary["created"] == 0
