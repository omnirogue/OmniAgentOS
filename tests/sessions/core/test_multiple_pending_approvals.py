"""A parked session can owe a human MORE THAN ONE decision.

``create_session_approval`` dedupes only rows that share an ``action_hash``, so
two unrelated tool calls from the same session are two pending rows by design.
Every max-park decision is therefore a fold over ALL of them, never a row picked
by order:

  * the ceiling is the window that expires LAST -- a delivered approval must not
    be able to spend an undelivered sibling's window at the normal ceiling, and
    so void a decision that was never announced to anybody;
  * the escalation records its result against every row it was sent for;
  * terminalization closes every outstanding row, not a representative;
  * an alert that names a ROW (the expiry requeue) stamps that row, and an alert
    that names the SESSION stamps everything outstanding when it fired.

Each test forces the adversarial id ordering rather than hoping for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    IDLE_MINUTES_ENV,
    MAX_PARK_MINUTES_ENV,
    REAPER_ENFORCE_ENV,
    SessionSupervisor,
)

from .conftest import seed_session

# The DAL lists approvals by (created_at ASC, id ASC), so the "zzz" row is the
# one any recency-flavoured pick returns.
_LAST_ID = "apr_zzzzzzzzzzzzzzzzzzzz"
_FIRST_ID = "apr_aaaaaaaaaaaaaaaaaaaa"


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _reaper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "1")
    monkeypatch.setenv(IDLE_MINUTES_ENV, "60")
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _supervisor(
    dal: SessionsDal, tmp_path: Path, alerts: list[str] | None = None, *, delivers: bool = False
) -> SessionSupervisor:
    sink = alerts if alerts is not None else []

    def notifier(title: str, _body: str) -> bool:
        sink.append(title)
        return delivers

    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
        notifier=notifier,
    )


def _two_unrelated_pending(
    dal: SessionsDal, session_id: str, *, created_at: str
) -> tuple[str, str]:
    """Two pending approvals for two different actions, ids forced.

    Returns ``(first_id, last_id)`` in the DAL's listing order.
    """
    first = dal.create_session_approval(
        session_id, "hash-first", "consequential", "first action", "{}", "high", "", None
    )
    last = dal.create_session_approval(
        session_id, "hash-last", "consequential", "last action", "{}", "high", "", None
    )
    dal._connection.execute(  # noqa: SLF001 - force the adversarial ordering
        "UPDATE approvals SET id = ?, created_at = ? WHERE id = ?", (_FIRST_ID, created_at, first)
    )
    dal._connection.execute(  # noqa: SLF001
        "UPDATE approvals SET id = ?, created_at = ? WHERE id = ?", (_LAST_ID, created_at, last)
    )
    dal._connection.commit()  # noqa: SLF001
    latest = dal.get_latest_session_approval(session_id)
    assert latest is not None and str(latest["id"]) == _LAST_ID, (
        "arrange failed: the second row must be the one row-recency picks"
    )
    return _FIRST_ID, _LAST_ID


def _parked(dal: SessionsDal, tmp_path: Path, session_id: str, *, seconds: float) -> None:
    seed_session(dal, tmp_path, session_id=session_id, state="awaiting_approval")
    dal._connection.execute(  # noqa: SLF001 - arrange a durable park age
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso_ago(seconds), session_id)
    )
    dal._connection.commit()  # noqa: SLF001


def test_a_delivered_approval_cannot_spend_an_undelivered_siblings_window(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_multi_mixed"
    _parked(sessions_dal, tmp_path, session_id, seconds=70)
    undelivered, delivered = _two_unrelated_pending(
        sessions_dal, session_id, created_at=_iso_ago(70)
    )
    sessions_dal.record_session_approval_delivery(delivered, delivered=True)

    _supervisor(sessions_dal, tmp_path).run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval", (
        "the normal ceiling reaped a park whose second approval was never delivered"
    )
    assert sessions_dal.max_park_extension_count(session_id) == 1, (
        "the undelivered approval earned no escalation before its window was judged"
    )
    rows = {str(entry["id"]): entry for entry in sessions_dal.list_session_approvals(session_id)}
    assert rows[undelivered]["delivery_attempted_at"] is not None, (
        "the escalation was not recorded against the row it was sent for"
    )


def test_the_normal_ceiling_still_fires_when_every_approval_was_delivered(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The fold must not become 'a multi-approval park never dies'."""
    session_id = "ses_multi_delivered"
    _parked(sessions_dal, tmp_path, session_id, seconds=70)
    first, last = _two_unrelated_pending(sessions_dal, session_id, created_at=_iso_ago(70))
    sessions_dal.record_session_approval_delivery(first, delivered=True)
    sessions_dal.record_session_approval_delivery(last, delivered=True)

    _supervisor(sessions_dal, tmp_path).run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert (row["state"], row["killed_by"]) == ("failed", "max-park")
    assert "delivered but undecided" in str(row["error"] or "")
    assert first in str(row["error"] or "") and last in str(row["error"] or ""), (
        "the terminal reason named one approval for a reap that voided two"
    )
    states = {
        str(entry["id"]): str(entry["state"])
        for entry in sessions_dal.list_session_approvals(session_id)
    }
    assert states == {first: "expired", last: "expired"}, (
        "a reap left an approval pending on a terminal session"
    )


def test_the_park_alert_stamps_every_outstanding_approval(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_multi_stamp"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="awaiting_approval")
    first, last = _two_unrelated_pending(sessions_dal, session_id, created_at=_iso_ago(1))

    _supervisor(sessions_dal, tmp_path, delivers=True)._notify_awaiting(session_id)  # noqa: SLF001

    rows = {str(entry["id"]): entry for entry in sessions_dal.list_session_approvals(session_id)}
    assert rows[first]["delivery_state"] == "delivered", (
        "an approval the park alert announced was recorded as never delivered"
    )
    assert rows[last]["delivery_state"] == "delivered"


def test_a_requeue_alert_stamps_only_the_row_it_minted(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requeue knows its new row's id; an unrelated sibling must not absorb
    the delivery result of an alert that was sent for a different action."""
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "0")  # isolate the requeue path
    session_id = "ses_multi_requeue"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="awaiting_approval")
    unrelated = sessions_dal.create_session_approval(
        session_id, "unrelated", "consequential", "unrelated action", "{}", "high", "", None
    )
    expired = sessions_dal.create_session_approval(
        session_id, "expiring", "consequential", "expiring action", "{}", "high", "", None
    )
    sessions_dal.expire_approval(expired, "approval expired")

    _supervisor(sessions_dal, tmp_path, delivers=True).run_once()

    rows = {str(entry["id"]): entry for entry in sessions_dal.list_session_approvals(session_id)}
    assert rows[unrelated]["delivery_state"] == "unattempted", (
        "an unrelated pending approval was marked delivered by a requeue alert about "
        "a different action"
    )
    requeued = [
        entry
        for entry in rows.values()
        if str(entry["state"]) == "pending" and str(entry["id"]) != unrelated
    ]
    assert len(requeued) == 1
    assert requeued[0]["delivery_state"] == "delivered"


def test_a_park_with_no_approval_row_still_escalates(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """Absence of a carrier stays the never-delivered case, not 'all delivered'."""
    session_id = "ses_multi_norow"
    _parked(sessions_dal, tmp_path, session_id, seconds=70)

    _supervisor(sessions_dal, tmp_path).run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval"
    assert sessions_dal.max_park_extension_count(session_id) == 1
