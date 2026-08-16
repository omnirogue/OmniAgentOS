"""A2 idle/hang reaper coverage (supervisor._reap_if_needed and friends).

Style matches test_supervisor.py: a real SessionsDal on a tmp DB, the real
SessionSupervisor with injected liveness/pgid/notifier fakes, driven through
run_once so the reaper is exercised on the same path production uses.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    IDLE_MINUTES_ENV,
    MAX_PARK_MINUTES_ENV,
    REAPER_ENFORCE_ENV,
    SessionSupervisor,
)

SESSION_REF = "11111111-2222-3333-4444-555555555555"


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(
    dal: SessionsDal,
    project_dir: Path,
    *,
    session_id: str = "ses_reap",
    state: str = "running",
    pid: int | None = 4242,
    title: str | None = None,
    age_seconds: float = 0.0,
    budget: float | None = None,
    cost: float = 0.0,
    idle_minutes: float | None = None,
) -> str:
    stamp = _iso_ago(age_seconds)
    dal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(project_dir),
            "provider": "claude",
            "session_ref": SESSION_REF,
            "state": state,
            "pid": pid,
            "model": "haiku",
            "title": title,
            "budget_usd_max": budget,
            "cost_usd": cost,
            "idle_minutes": idle_minutes,
            "last_activity_at": stamp,
            "created_at": stamp,
            "updated_at": stamp,
        }
    )
    return session_id


def _supervisor(
    dal: SessionsDal,
    tmp_path: Path,
    notifications: list[tuple[str, str]],
    *,
    children: int = 0,
) -> SessionSupervisor:
    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: children,
        notifier=lambda title, body: notifications.append((title, body)),
    )


@pytest.fixture(autouse=True)
def _reaper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic reaper env: 1-minute threshold, dry-run default, no ambient
    CLAUDE_CONFIG_DIR leaking a real transcript directory into mtime resolution."""
    monkeypatch.setenv(IDLE_MINUTES_ENV, "1")
    monkeypatch.delenv(REAPER_ENFORCE_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _would_kill_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "would-kill" in r.getMessage()]


def test_dry_run_logs_structured_would_kill_once_and_never_kills(
    sessions_dal: SessionsDal, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = _seed(sessions_dal, tmp_path, age_seconds=120)
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)

    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()
        supervisor.run_once()  # dedupe: the second pass must not repeat the line

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0
    assert notifications == []
    lines = _would_kill_lines(caplog)
    assert len(lines) == 1
    assert session_id in lines[0]
    assert '"reason": "idle"' in lines[0]
    assert '"idle_seconds"' in lines[0]
    assert '"enforce": false' in lines[0]


def test_fresh_activity_is_not_idle(
    sessions_dal: SessionsDal, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = _seed(sessions_dal, tmp_path, age_seconds=0)
    supervisor = _supervisor(sessions_dal, tmp_path, [])

    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()

    assert _would_kill_lines(caplog) == []
    assert sessions_dal.get_session(session_id)["state"] == "running"  # type: ignore[index]


def test_idle_uses_max_of_activity_and_transcript_mtime(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan A2: idle = now - max(last_activity_at, transcript mtime). A stale DB
    mark with a fresh on-disk CLI transcript (the adopted-session shape) must
    survive; once the transcript is old too, the session is reaped."""
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    session_id = _seed(sessions_dal, project_dir, age_seconds=600)

    config_dir = tmp_path / "claude-config"
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))
    transcript = config_dir / "projects" / munged / f"{SESSION_REF}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")  # fresh mtime = live session
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)
    supervisor.run_once()
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0

    # Age the transcript past the 3x hard ceiling: now genuinely idle -> reaped.
    old = datetime.now(UTC).timestamp() - 600
    os.utime(transcript, (old, old))
    supervisor.run_once()
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["kill_requested"] == 1
    assert row["killed_by"] == "idle-reaper"


def test_live_pgid_children_defer_kill_until_hard_ceiling(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    # Idle past the threshold (60s) but inside the 3x ceiling (180s), with a live
    # tool subprocess: deferred, not killed.
    deferred = _seed(sessions_dal, tmp_path, session_id="ses_defer", age_seconds=120)
    supervisor = _supervisor(sessions_dal, tmp_path, [], children=1)
    supervisor.run_once()
    row = sessions_dal.get_session(deferred)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0

    # Past the 3x hard ceiling the kill proceeds even with live children.
    ceiling = _seed(sessions_dal, tmp_path, session_id="ses_ceiling", age_seconds=600)
    supervisor.run_once()
    row = sessions_dal.get_session(ceiling)
    assert row is not None
    assert row["kill_requested"] == 1
    assert row["killed_by"] == "idle-reaper"
    # The deferred session is still deferred (idle grew only by the pass runtime).
    assert sessions_dal.get_session(deferred)["kill_requested"] == 0  # type: ignore[index]


def test_enforce_kills_notifies_and_attributes_idle_reaper(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=600)
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)

    supervisor.run_once()  # requests the kill
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["kill_requested"] == 1
    assert row["killed_by"] == "idle-reaper"
    assert len(notifications) == 1
    assert "reaped" in notifications[0][0]
    assert session_id in notifications[0][1]

    supervisor.run_once()  # services the kill flag
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "killed"
    assert row["killed_by"] == "idle-reaper"
    assert len(notifications) == 1  # no repeat notification


def test_cas_guard_noops_when_session_terminalizes_between_check_and_kill(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=600)
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)

    original = sessions_dal.request_kill

    def race(sid: str, **kwargs: Any) -> bool:
        # The session completes in the window between the reaper's idle check
        # and its kill request; the non-terminal WHERE makes this a no-op.
        sessions_dal.terminalize_session(sid, "completed", void_note="raced to terminal")
        return original(sid, **kwargs)

    monkeypatch.setattr(sessions_dal, "request_kill", race)
    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "completed"
    assert row["kill_requested"] == 0
    assert notifications == []


def test_longhaul_marked_sessions_are_reaped_with_their_override(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog retirement (P3): longhaul sessions are reaped like any other
    bridge session, but under the per-lane idle_minutes override the engine
    persists at spawn — never the (much tighter) global default. Respawn stays
    the engine's: it sees killed_by='idle-reaper' and spawns the successor."""
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    # 40 min idle with a 45-min override: far past the 1-min env default, but
    # inside the lane threshold — must be untouched (override honored).
    inside = _seed(
        sessions_dal,
        tmp_path,
        session_id="ses_lh_inside",
        title="[longhaul:att_123] refactor storage",
        age_seconds=40 * 60,
        idle_minutes=45.0,
    )
    # 50 min idle with the same override: genuinely idle — reaped, with the
    # idle-reaper attribution the longhaul engine's D7 respawn branch keys on.
    beyond = _seed(
        sessions_dal,
        tmp_path,
        session_id="ses_lh_beyond",
        title="[longhaul:att_456] refactor storage",
        age_seconds=50 * 60,
        idle_minutes=45.0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    supervisor.run_once()

    row = sessions_dal.get_session(inside)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0
    row = sessions_dal.get_session(beyond)
    assert row is not None
    assert row["kill_requested"] == 1
    assert row["killed_by"] == "idle-reaper"


def test_longhaul_marked_sessions_get_dry_run_would_kill_lines(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In dry-run (enforce unset) an idle longhaul session produces a would-kill
    line — the pre-P3 unconditional marker skip logged nothing at all."""
    session_id = _seed(
        sessions_dal,
        tmp_path,
        title="[longhaul:att_789] long build",
        age_seconds=50 * 60,
        idle_minutes=45.0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0  # dry-run never kills
    lines = _would_kill_lines(caplog)
    assert len(lines) == 1
    assert session_id in lines[0]


def test_awaiting_approval_is_excluded(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IDLE reaper never touches a parked session, however idle it looks.

    Bounding a park is a SEPARATE mechanism (the max-park ceiling, covered in
    test_max_park.py) with its own honest reason and its own knob; it is
    disabled here so this test keeps testing exactly what it names — that
    ``awaiting_approval`` is outside the idle/hang reaper's remit.
    """
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "0")
    session_id = _seed(sessions_dal, tmp_path, state="awaiting_approval", age_seconds=6000)
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "awaiting_approval"
    assert row["kill_requested"] == 0


def test_budget_breach_respects_dry_run(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Budget enforcement is opt-in since 2026-07-24 (advisory by default, so a
    # session over budget is never killed mid-task). Pin the escape hatch: with
    # enforcement ON the reaper still reports the would-kill under dry-run.
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=0, budget=1.0, cost=1.5)
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "running"
    assert row["kill_requested"] == 0
    lines = _would_kill_lines(caplog)
    assert len(lines) == 1
    assert '"reason": "budget"' in lines[0]


def test_budget_breach_enforced_kills_with_budget_attribution(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=0, budget=1.0, cost=1.0)
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)
    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["kill_requested"] == 1
    assert row["killed_by"] == "budget"
    assert len(notifications) == 1
    assert "budget" in notifications[0][0]


def test_starting_over_five_minutes_without_pid_fails(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    stalled = _seed(
        sessions_dal,
        tmp_path,
        session_id="ses_stalled",
        state="starting",
        pid=None,
        age_seconds=600,
    )
    fresh = _seed(
        sessions_dal,
        tmp_path,
        session_id="ses_freshstart",
        state="starting",
        pid=None,
        age_seconds=10,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    supervisor.run_once()

    row = sessions_dal.get_session(stalled)
    assert row is not None
    assert row["state"] == "failed"
    assert "stalled in starting" in (row["error"] or "")
    assert sessions_dal.get_session(fresh)["state"] == "starting"  # type: ignore[index]


def test_starting_with_queued_spawn_is_left_for_the_ingress(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = _seed(
        sessions_dal,
        tmp_path,
        session_id="ses_queued",
        state="starting",
        pid=None,
        age_seconds=600,
    )
    sessions_dal.enqueue_spawn(
        session_id=session_id,
        project_dir=str(tmp_path),
        model="haiku",
        prompt="queued work",
        budget_usd_max=None,
        title=None,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, [])
    # Service the session directly (run_once would drain the queue first and
    # launch a real process; the guard under test is the reaper's skip).
    row = sessions_dal.get_session(session_id)
    assert row is not None
    supervisor._service_session(session_id, row)  # noqa: SLF001

    assert sessions_dal.get_session(session_id)["state"] == "starting"  # type: ignore[index]


def test_dead_pid_is_left_to_reconcile_paths(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A RUNNING row whose pid is dead is the reconcile/adopted-pid path's job;
    the idle reaper must not double-handle it."""
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=6000)
    notifications: list[tuple[str, str]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,  # dead
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda title, body: notifications.append((title, body)),
    )
    with caplog.at_level(logging.WARNING, logger="omniagentos.sessions.supervisor"):
        supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["kill_requested"] == 0
    assert row["killed_by"] is None


def test_budget_breach_is_advisory_by_default(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default posture: an over-budget session keeps running.

    the operator, 2026-07-24: budgets must not be hard blockers. Killing a live session on
    a cost line loses the in-flight work; the overshoot is logged instead. The
    reaper's IDLE duty is unaffected, so this session (age 0) survives.
    """
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    monkeypatch.setenv(REAPER_ENFORCE_ENV, "1")
    session_id = _seed(sessions_dal, tmp_path, age_seconds=0, budget=1.0, cost=99.0)
    notifications: list[tuple[str, str]] = []
    supervisor = _supervisor(sessions_dal, tmp_path, notifications)
    supervisor.run_once()

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["kill_requested"] == 0, "an over-budget session was killed"
    assert row["state"] == "running"
    assert not any("budget" in title for title, _ in notifications)
