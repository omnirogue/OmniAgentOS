"""Max-park ceiling on awaiting_approval (parked-approval hang, cause #2).

``awaiting_approval`` is parked BY DESIGN so a human decision can resume the
exact session (AC-13) — but the park had no ceiling, so an approval that was
never delivered (bench swr_8474e958870543388267: zero approval rows) or never
decided held the session, its slot and its swarm run open indefinitely.

The contract these tests pin:
  * inside the window the park is preserved and the resume path still works;
  * a delivered-but-undecided approval terminalizes at the normal ceiling;
  * an undelivered approval/no-row shape gets the bounded delivery-aware path;
  * it is NEVER auto-approved — failing closed is the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    DEFAULT_MAX_PARK_MINUTES,
    MAX_PARK_MINUTES_ENV,
    SessionSupervisor,
)

from .conftest import seed_session
from .test_supervisor import FakeProcess, fake_factory, wait_for_state


def _iso_ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _park(
    dal: SessionsDal,
    tmp_path: Path,
    *,
    minutes_parked: float,
    session_id: str = "ses_park",
    with_approval: bool = True,
    use_event: bool = False,
) -> str:
    """Seed a session parked in awaiting_approval for ``minutes_parked``.

    ``use_event`` writes the durable lifecycle event the supervisor prefers
    (and leaves ``updated_at`` fresh) so the two clock sources can be told
    apart; otherwise only the row stamp is backdated.
    """
    seed_session(dal, tmp_path, session_id=session_id, state="awaiting_approval")
    stamp = _iso_ago(minutes_parked)
    if use_event:
        dal._connection.execute(  # noqa: SLF001 - arrange the lifecycle event directly
            "INSERT INTO events "
            "(ts, type, actor, action, target_type, target_id, payload_json, trace_id) "
            "VALUES (?, 'audit', 'session-bridge', 'session.awaiting_approval', "
            "'session', ?, '{}', '')",
            (stamp, session_id),
        )
    else:
        dal._connection.execute(  # noqa: SLF001 - arrange the row stamp
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id)
        )
    dal._connection.commit()  # noqa: SLF001
    if with_approval:
        approval_id = dal.create_session_approval(
            session_id, "hash", "consequential", "risky", "{}", "high", "", None
        )
        # This fixture represents the ordinary established path where the
        # approval alert reached a human when the session was parked. Tests for
        # the never-delivered branch live in test_reaper_delivery_liveness.py.
        dal.record_session_approval_delivery(approval_id, delivered=True, attempted_at=stamp)
    return session_id


def _supervisor(
    dal: SessionsDal,
    tmp_path: Path,
    captures: list[tuple[list[str], dict[str, Any]]],
    notifications: list[tuple[str, str]] | None = None,
) -> SessionSupervisor:
    sink = notifications if notifications is not None else []
    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures, lambda: FakeProcess([{"type": "result", "subtype": "success"}])
        ),
        notifier=lambda title, body: sink.append((title, body)),
    )


def test_park_below_the_ceiling_is_preserved_and_still_resumes(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-13 intact: inside the window nothing is reaped and approval resumes."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    session_id = _park(sessions_dal, tmp_path, minutes_parked=DEFAULT_MAX_PARK_MINUTES - 5)
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, captures)

    supervisor.run_once()
    assert sessions_dal.get_session(session_id)["state"] == "awaiting_approval"  # type: ignore[index]
    assert captures == []

    approval = sessions_dal.get_latest_session_approval(session_id)
    assert approval is not None and approval["state"] == "pending"
    store = SqliteStore(str(tmp_path / "sessions.db"))
    assert store.decide_approval(str(approval["id"]), "approved", "owner")

    supervisor.run_once()
    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1  # the SAME session resumed, exactly once


def test_park_past_the_ceiling_fails_closed_with_an_honest_reason(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = _park(sessions_dal, tmp_path, minutes_parked=DEFAULT_MAX_PARK_MINUTES + 5)
    captures: list[tuple[list[str], dict[str, Any]]] = []
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, captures, notifications)

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["killed_by"] == "max-park"
    error = str(row["error"] or "")
    assert "approval delivered but undecided within 20m" in error
    assert "parked session reaped" in error
    # NEVER auto-approved: the approval is voided, not granted, and no process
    # was launched off the back of it.
    approval = sessions_dal.get_latest_session_approval(session_id)
    assert approval is not None
    assert approval["state"] == "expired"
    assert captures == []
    assert notifications and "approval never decided" in notifications[0][0]


def test_park_with_zero_approval_rows_is_reaped_too(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The measured bench shape: the approval request never reached this stack,
    so there is no approval row at all and nothing could ever resolve it."""
    session_id = _park(
        sessions_dal, tmp_path, minutes_parked=85, with_approval=False, session_id="ses_bench"
    )
    assert sessions_dal.list_session_approvals(session_id) == []
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, captures)

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert "none recorded" in str(row["error"] or "")


def test_a_decided_approval_past_the_ceiling_still_resumes(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling targets UNDELIVERED/UNDECIDED approvals. A decision that did
    arrive (late human click) is not the failure mode and must still resume."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    session_id = _park(sessions_dal, tmp_path, minutes_parked=DEFAULT_MAX_PARK_MINUTES + 30)
    approval = sessions_dal.get_latest_session_approval(session_id)
    assert approval is not None
    store = SqliteStore(str(tmp_path / "sessions.db"))
    assert store.decide_approval(str(approval["id"]), "approved", "owner")
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, captures)

    supervisor.run_once()

    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1


def test_ceiling_is_env_configurable_and_can_be_disabled(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _park(sessions_dal, tmp_path, minutes_parked=25)
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, captures)

    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "60")
    supervisor.run_once()
    assert sessions_dal.get_session(session_id)["state"] == "awaiting_approval"  # type: ignore[index]

    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "0")  # explicitly disabled
    supervisor.run_once()
    assert sessions_dal.get_session(session_id)["state"] == "awaiting_approval"  # type: ignore[index]

    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "10")
    supervisor.run_once()
    assert sessions_dal.get_session(session_id)["state"] == "failed"  # type: ignore[index]


def test_park_clock_comes_from_the_lifecycle_event_not_the_row_stamp(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """A row stamp moves on ANY field write; the park clock must not rewind with
    it, and must not be shortened by a stale one either."""
    old_event = _park(
        sessions_dal,
        tmp_path,
        minutes_parked=DEFAULT_MAX_PARK_MINUTES + 5,
        session_id="ses_old_event",
        use_event=True,
    )  # event backdated, updated_at fresh -> the event wins, so it is reaped
    fresh_event = _park(
        sessions_dal,
        tmp_path,
        minutes_parked=1,
        session_id="ses_fresh_event",
        use_event=True,
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - a stale row stamp must not reap it
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso_ago(90), fresh_event)
    )
    sessions_dal._connection.commit()  # noqa: SLF001

    captures: list[tuple[list[str], dict[str, Any]]] = []
    _supervisor(sessions_dal, tmp_path, captures).run_once()

    assert sessions_dal.get_session(old_event)["state"] == "failed"  # type: ignore[index]
    assert sessions_dal.get_session(fresh_event)["state"] == "awaiting_approval"  # type: ignore[index]
