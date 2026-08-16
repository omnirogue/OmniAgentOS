"""Regression coverage for delivery-aware max-park and adopted-session liveness."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.sessions import notify
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    IDLE_MINUTES_ENV,
    MAX_PARK_MINUTES_ENV,
    REAPER_ENFORCE_ENV,
    SessionSupervisor,
)

from .conftest import seed_session


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parked(
    dal: SessionsDal,
    tmp_path: Path,
    *,
    session_id: str,
    age_seconds: float,
    approval: bool,
) -> str | None:
    seed_session(dal, tmp_path, session_id=session_id, state="awaiting_approval")
    dal._connection.execute(  # noqa: SLF001 - arrange a durable park age
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso_ago(age_seconds), session_id)
    )
    dal._connection.commit()  # noqa: SLF001
    if not approval:
        return None
    return dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )


def _supervisor(dal: SessionsDal, tmp_path: Path, *, children: int = 0) -> SessionSupervisor:
    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: children,
        notifier=lambda _title, _body: None,
    )


@pytest.fixture(autouse=True)
def _reaper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "1")
    monkeypatch.setenv(IDLE_MINUTES_ENV, "1")
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_undelivered_park_rearms_before_the_normal_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "ses_undelivered_extend"
    _parked(sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=False)
    supervisor = _supervisor(sessions_dal, tmp_path)

    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval"
    assert row["killed_by"] is None
    assert any("reaper.max_park_extended" in record.getMessage() for record in caplog.records)


def test_default_notifier_does_not_record_a_local_banner_as_remote_delivery(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production notifier's banner must not make the R-1 carrier constant."""
    monkeypatch.setattr(notify, "_push_terminal_notifier", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify, "_push_ntfy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(notify, "_push_slack", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(notify, "_ntfy_allowed", lambda *_args, **_kwargs: True)

    session_id = "ses_banner_not_remote"
    approval_id = _parked(
        sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=True
    )
    assert approval_id is not None
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
    )

    supervisor._notify_awaiting(session_id)  # noqa: SLF001 - production alert path
    approval = sessions_dal.get_latest_session_approval(session_id)
    assert approval is not None
    assert approval["delivery_state"] == "failed"

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval"
    assert row["killed_by"] is None


def test_undelivered_park_still_reaps_at_the_outer_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_undelivered_outer"
    _parked(sessions_dal, tmp_path, session_id=session_id, age_seconds=4 * 60 + 10, approval=False)
    supervisor = _supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["killed_by"] == "max-park"


def test_undelivered_park_is_alive_at_three_times_and_terminal_at_outer_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_undelivered_multiplier"
    _parked(sessions_dal, tmp_path, session_id=session_id, age_seconds=3 * 60 + 5, approval=False)
    supervisor = _supervisor(sessions_dal, tmp_path)

    supervisor.run_once()
    alive = sessions_dal.get_session(session_id)
    assert alive is not None
    assert alive["state"] == "awaiting_approval"

    sessions_dal._connection.execute(  # noqa: SLF001 - arrange the outer boundary
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso_ago(4 * 60 + 1), session_id)
    )
    sessions_dal._connection.commit()  # noqa: SLF001
    supervisor.run_once()

    terminal = sessions_dal.get_session(session_id)
    assert terminal is not None
    assert terminal["state"] == "failed"
    assert terminal["killed_by"] == "max-park"
    assert "4m outer ceiling" in str(terminal["error"] or "")


def test_delivered_but_undecided_park_reaps_at_the_normal_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_delivered"
    approval_id = _parked(
        sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=True
    )
    assert approval_id is not None
    sessions_dal.record_session_approval_delivery(
        approval_id, delivered=True, attempted_at=_iso_ago(70)
    )
    supervisor = _supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["killed_by"] == "max-park"


def test_a_late_delivery_that_was_not_an_escalation_keeps_the_normal_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The 4x window is bought by THIS reaper's escalation, not by a stamp.

    ``delivered_at`` has four writers -- the park alert, the max-park
    escalation, the expiry requeue and the hook route -- and three of them run
    at times unrelated to the ceiling. A delivery recorded after the park began
    but with no ``reaper.max_park_extended`` receipt behind it is an ordinary
    delivery, so the ordinary deadline stands.
    """
    session_id = "ses_late_plain_delivery"
    approval_id = _parked(
        sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=True
    )
    assert approval_id is not None
    # Stamped 5s ago: well after the park started, well inside the ceiling --
    # the exact shape a per-poll requeue re-stamp produces.
    sessions_dal.record_session_approval_delivery(
        approval_id, delivered=True, attempted_at=_iso_ago(5)
    )
    assert sessions_dal.max_park_extended_since(session_id) is None
    supervisor = _supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed", "a plain late delivery must not buy the outer window"
    assert row["killed_by"] == "max-park"
    assert "delivered but undecided" in str(row["error"] or "")


def test_an_escalation_delivery_does_earn_one_more_window(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The converse of the test above: when the late delivery IS the reaper's
    own escalation, the human who just received it gets a window to act in."""
    session_id = "ses_escalated_delivery"
    approval_id = _parked(
        sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=True
    )
    assert approval_id is not None
    supervisor = _supervisor(sessions_dal, tmp_path)

    # Pass 1: undelivered past the ceiling -> escalate, re-arm, stay parked.
    supervisor.run_once()
    parked = sessions_dal.get_session(session_id)
    assert parked is not None and parked["killed_by"] is None
    assert sessions_dal.max_park_extended_since(session_id) is not None

    # Pass 2: the escalation reached a human this time.
    sessions_dal.record_session_approval_delivery(approval_id, delivered=True)
    supervisor.run_once()

    still = sessions_dal.get_session(session_id)
    assert still is not None
    assert still["state"] == "awaiting_approval"
    assert still["killed_by"] is None


def test_a_second_park_episode_escalates_again(
    sessions_dal: SessionsDal, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The escalation dedupe is per park EPISODE, not per session lifetime.

    On a persistently broken delivery channel -- the condition the re-arm
    exists for -- every episode after the first used to be silent: no operator
    alert, no durable receipt, and an ``extensions`` count that under-reported
    the terminal reason.
    """
    session_id = "ses_two_episodes"
    _parked(sessions_dal, tmp_path, session_id=session_id, age_seconds=70, approval=False)
    alerts: list[str] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda title, _body: alerts.append(title),
    )

    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()
    assert len(alerts) == 1
    assert sessions_dal.max_park_extension_count(session_id) == 1

    # The approval is decided, the session resumes and parks again later. Age
    # episode 1's receipt past the ceiling so the DURABLE dedupe provably is not
    # what is being tested here.
    sessions_dal._connection.execute(  # noqa: SLF001 - arrange the episode boundary
        "UPDATE events SET ts = ? WHERE action = 'reaper.max_park_extended' AND target_id = ?",
        (_iso_ago(3000), session_id),
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - re-park
        "UPDATE sessions SET updated_at = ?, state = 'awaiting_approval' WHERE id = ?",
        (_iso_ago(70), session_id),
    )
    sessions_dal._connection.commit()  # noqa: SLF001

    supervisor.run_once()

    assert len(alerts) == 2, "the second undelivered park episode reached nobody"
    assert sessions_dal.max_park_extension_count(session_id) == 2


def test_adopted_unknown_transcript_without_children_defers_but_still_reaps_later(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = "ses_adopted_unknown"
    dead = "ses_adopted_dead"
    for session_id, age in ((active, 120), (dead, 600)):
        seed_session(sessions_dal, tmp_path, session_id=session_id, state="running", pid=4242)
        stamp = _iso_ago(age)
        sessions_dal._connection.execute(  # noqa: SLF001 - arrange stale known signals
            "UPDATE sessions SET last_activity_at = ?, updated_at = ?, created_at = ? WHERE id = ?",
            (stamp, stamp, stamp, session_id),
        )
    sessions_dal._connection.commit()  # noqa: SLF001
    supervisor = _supervisor(sessions_dal, tmp_path, children=0)
    supervisor._adopted.update({active: 4242, dead: 4242})  # noqa: SLF001 - restart-owned shape
    monkeypatch.setattr(supervisor, "_transcript_mtime", lambda _session: None)

    supervisor.run_once()

    active_row = sessions_dal.get_session(active)
    dead_row = sessions_dal.get_session(dead)
    assert active_row is not None and active_row["kill_requested"] == 0
    assert dead_row is not None
    assert dead_row["kill_requested"] == 1
    assert dead_row["killed_by"] == "idle-reaper"


def test_slack_transport_is_allowlisted_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
    monkeypatch.setattr(notify, "_ntfy_allowed", lambda _kind, _severity: True)
    monkeypatch.setattr(notify, "_push_terminal_notifier", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(notify, "_push_ntfy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        notify.httpx,
        "post",
        lambda endpoint, **kwargs: calls.append((endpoint, kwargs)) or httpx.Response(200),
    )

    notify.push("Approval required", "session ses_slack", kind="approval", severity="warning")

    assert calls == [
        (
            "https://hooks.slack.com/services/test",
            {
                "json": {
                    "text": "Approval required\nhttp://127.0.0.1:3003"
                },
                "timeout": 3.0,
            },
        )
    ]


def test_slack_unset_logs_once_and_failed_post_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(notify, "_SLACK_DISABLED_LOGGED", False)
    monkeypatch.delenv("OPS_ALERT_SLACK_WEBHOOK_URL", raising=False)
    with caplog.at_level(logging.INFO, logger=notify.logger.name):
        notify._push_slack("title", "https://dashboard.invalid")
        notify._push_slack("title", "https://dashboard.invalid")
    assert len([r for r in caplog.records if "OPS_ALERT_SLACK_WEBHOOK_URL unset" in r.getMessage()]) == 1

    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
    monkeypatch.setattr(
        notify.httpx,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    assert notify._push_slack("title", "https://dashboard.invalid") is False


def test_invalid_slack_webhook_is_never_posted_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(notify, "_SLACK_URL_WARNED", False)
    monkeypatch.setenv("OPS_ALERT_SLACK_WEBHOOK_URL", "https://slack.invalid/webhook")
    calls: list[object] = []
    monkeypatch.setattr(notify.httpx, "post", lambda *_args, **_kwargs: calls.append(object()))

    with caplog.at_level(logging.WARNING, logger=notify.logger.name):
        notify._push_slack("title", "https://dashboard.invalid")
        notify._push_slack("title", "https://dashboard.invalid")

    assert calls == []
    warnings = [r for r in caplog.records if "OPS_ALERT_SLACK_WEBHOOK_URL" in r.getMessage()]
    assert len(warnings) == 1
