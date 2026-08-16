"""Which approval row is "the" one: state, never row recency.

``get_latest_session_approval`` orders by ``(created_at DESC, id DESC)``. An
expiry requeue leaves the expired row and its pending replacement with the SAME
one-second ``created_at``, so the tiebreak is a comparison of two random hex
ids -- a coin toss, re-thrown on every process. Three supervision decisions were
reading that coin:

  * ``_reap_parked_if_needed`` treated the expired half as "decided, not our
    failure mode" and returned False forever, so the park escaped BOTH the
    normal and the outer ceiling -- the leak the whole max-park work exists to
    close;
  * ``_notify_awaiting`` skipped its delivery stamp, recording an approval that
    WAS announced as never-delivered;
  * the adopted-PID-died branch failed a session that still had an outstanding
    approval, instead of parking it.

Each of these tests forces the adversarial ordering rather than hoping for it.
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

_EXPIRED_ID = "apr_zzzzzzzzzzzzzzzzzzzz"  # sorts ABOVE the pending id
_PENDING_ID = "apr_aaaaaaaaaaaaaaaaaaaa"


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _reaper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "1")
    monkeypatch.setenv(IDLE_MINUTES_ENV, "60")
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _requeued_pair(dal: SessionsDal, session_id: str, *, created_at: str) -> None:
    """An expired approval and its pending replacement, same second, expired id
    sorting last -- exactly what get_latest_session_approval mis-picks."""
    expired = dal.create_session_approval(
        session_id, "hash-1", "consequential", "risky", "{}", "high", "", None
    )
    dal.expire_approval(expired, "approval expired")
    pending = dal.create_session_approval(
        session_id, "hash-2", "consequential", "requeued", "{}", "high", "", None
    )
    dal._connection.execute(  # noqa: SLF001 - force the adversarial tiebreak
        "UPDATE approvals SET id = ?, created_at = ? WHERE id = ?",
        (_EXPIRED_ID, created_at, expired),
    )
    dal._connection.execute(  # noqa: SLF001
        "UPDATE approvals SET id = ?, created_at = ? WHERE id = ?",
        (_PENDING_ID, created_at, pending),
    )
    dal._connection.commit()  # noqa: SLF001
    latest = dal.get_latest_session_approval(session_id)
    assert latest is not None and str(latest["id"]) == _EXPIRED_ID, (
        "arrange failed: the expired row must be the one row-recency picks"
    )


def _supervisor(
    dal: SessionsDal, tmp_path: Path, alerts: list[str] | None = None
) -> SessionSupervisor:
    sink = alerts if alerts is not None else []
    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda title, _body: sink.append(title),
    )


def test_a_requeued_park_past_the_ceiling_is_still_reaped(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_requeued_pair"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="awaiting_approval")
    sessions_dal._connection.execute(  # noqa: SLF001 - arrange a durable park age
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso_ago(300), session_id)
    )
    sessions_dal._connection.commit()  # noqa: SLF001
    _requeued_pair(sessions_dal, session_id, created_at=_iso_ago(300))

    _supervisor(sessions_dal, tmp_path).run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["killed_by"] == "max-park", (
        "a park whose newest ROW is the expired half of a requeued pair escaped both ceilings"
    )
    assert row["state"] == "failed"


def test_the_delivery_stamp_lands_on_the_outstanding_row(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_stamp_pair"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="awaiting_approval")
    _requeued_pair(sessions_dal, session_id, created_at=_iso_ago(1))
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda _title, _body: True,
    )

    supervisor._notify_awaiting(session_id)  # noqa: SLF001 - production alert path

    rows = {str(row["id"]): row for row in sessions_dal.list_session_approvals(session_id)}
    assert rows[_PENDING_ID]["delivery_state"] == "delivered", (
        "the approval the alert was about was recorded as never-delivered"
    )
    assert rows[_EXPIRED_ID]["delivery_state"] == "unattempted"


def test_an_adopted_pid_death_parks_a_session_that_still_owes_a_decision(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_adopted_pair"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="running", pid=4242)
    _requeued_pair(sessions_dal, session_id, created_at=_iso_ago(1))
    supervisor = _supervisor(sessions_dal, tmp_path)
    supervisor._adopted[session_id] = 4242  # noqa: SLF001 - restart-owned shape

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval", (
        "a session with an outstanding approval was shredded, not parked"
    )
