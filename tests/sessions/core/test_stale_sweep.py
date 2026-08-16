"""A5: supervisor-side latency fix for stale orchestration/swarm-run detection.

Headless coverage of the stale sweep already existed via the routines tick
(every 300s) -> ``board_sweep.sweep_board`` -> ``OrchestrationsDal.mark_stale_failed``.
This adds the SAME sweep (plus ``SwarmDal.mark_stale_failed``, which previously had
no periodic caller at all) to ``SessionSupervisor.run_once``'s sub-second poll loop,
sharing ONE 30s-throttled claim (``_claim_reconcile_stale_check``, imported from
``omniagentos.intake.service`` rather than copy-pasted) with board reads
(``reconcile_board``) and the routines tick, so the three call sites never run
duplicate sweeps in the same window.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.intake import service
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import SessionSupervisor, _dal_db_path
from omniagentos.swarm.dal import SwarmDal


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    """The claim is a process-global dict; isolate every test from the others."""
    service._reset_reconcile_stale_throttle()
    yield
    service._reset_reconcile_stale_throttle()


def _make_supervisor(sessions_dal: SessionsDal, tmp_path: Path) -> SessionSupervisor:
    return SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _title, _body: None,
    )


def _spy_both(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    calls: dict[str, list[int]] = {"orch": [], "swarm": []}

    def fake_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        calls["orch"].append(stale_minutes)
        return []

    def fake_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        calls["swarm"].append(stale_minutes)
        return []

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", fake_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", fake_swarm)
    return calls


def test_run_once_marks_both_orchestrations_and_swarm_stale(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single run_once claims the sweep and invokes BOTH dals' marks."""
    monkeypatch.setenv("OMNIAGENTOS_ORCH_STALE_MINUTES", "10")
    calls = _spy_both(monkeypatch)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    assert calls["orch"] == [10]
    assert calls["swarm"] == [10]


def test_run_once_throttles_repeat_sweeps_within_30s(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two run_once passes back-to-back (well within the 30s window) -> one sweep."""
    calls = _spy_both(monkeypatch)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()
    supervisor.run_once()

    assert len(calls["orch"]) == 1
    assert len(calls["swarm"]) == 1


def test_claim_contention_another_caller_wins_the_sweep(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers racing the same db path: whichever claims first wins the sweep.

    Simulates a board-read (``reconcile_board``) or routines-tick sweep that
    already claimed the same db path an instant earlier -- the supervisor's own
    call must see the claim already taken and skip its sweep entirely.
    """
    calls = _spy_both(monkeypatch)
    supervisor = _make_supervisor(sessions_dal, tmp_path)
    db_path = _dal_db_path(sessions_dal)

    assert service._claim_reconcile_stale_check(db_path) is True  # the "other caller"

    supervisor.run_once()

    assert calls["orch"] == []
    assert calls["swarm"] == []


def test_orchestration_sweep_failure_is_isolated_from_swarm_and_the_loop(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OrchestrationsDal.mark_stale_failed raising must not stop the swarm mark
    or escape run_once."""
    calls: dict[str, list[int]] = {"swarm": []}

    def raising_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("database is locked")

    def fake_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        calls["swarm"].append(stale_minutes)
        return []

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", raising_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", fake_swarm)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()  # must not raise

    assert len(calls["swarm"]) == 1


def test_swarm_sweep_failure_is_isolated_from_orchestrations_and_the_loop(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SwarmDal.mark_stale_failed raising must not stop the orchestrations mark
    (already run first) or escape run_once."""
    calls: dict[str, list[int]] = {"orch": []}

    def fake_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        calls["orch"].append(stale_minutes)
        return []

    def raising_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", fake_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", raising_swarm)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()  # must not raise

    assert len(calls["orch"]) == 1


def test_intake_first_import_order_does_not_crash() -> None:
    """P3 circular-import regression pin: the sessions daemon's first sweep pass
    imports intake BEFORE anything has imported the API package. Pre-fix,
    intake.service's module-level ``from omniagentos.api.services import ...``
    dragged in api.main -> api.routes.intake -> back into the partially
    initialized intake.service, and every daemon pass died with "cannot import
    name 'ClarifyLLM'". Must run in a FRESH interpreter — in-process imports are
    already cached by other tests."""
    script = (
        "from omniagentos.intake.orchestrations import OrchestrationsDal\n"
        "from omniagentos.intake.service import (\n"
        "    _claim_reconcile_stale_check,\n"
        "    _orchestration_stale_minutes,\n"
        ")\n"
        "from omniagentos.swarm.dal import SwarmDal\n"
        "import omniagentos.api  # the API package must still initialize afterwards\n"
        "from omniagentos.api.routes.intake import ClarifyLLMDep\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# --- failure bell for sweep-failed swarm runs (silent-failure class D6) --------


def _swept_row(run_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": run_id,
        "status": "failed",
        "goal": "Overnight goal",
        "board_task_id": "btk_1",
        "working_dir": "/w",
        "error": "stale heartbeat — swarm coordinator died",
    }
    row.update(overrides)
    return row


def _capture_failure_bell(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import omniagentos.notifications.service as notif_service

    calls: list[dict[str, Any]] = []

    def fake_notify(**kwargs: Any) -> str | None:
        calls.append(kwargs)
        return "ntf_fake"

    monkeypatch.setattr(notif_service, "notify_run_terminal_failure", fake_notify)
    return calls


def test_sweep_failed_swarm_runs_ring_the_failure_bell_per_run(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs failed by the sweep (dead coordinator) never reach the coordinator's
    own terminal emit — the sweep must ring the same swarm_failed bell, once
    per swept run, with the run's identity/goal/card threaded through."""

    def fake_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return []

    def fake_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return [
            _swept_row("swr_a"),
            _swept_row("swr_b", board_task_id=None, working_dir="", goal=""),
        ]

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", fake_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", fake_swarm)
    bell = _capture_failure_bell(monkeypatch)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    assert len(bell) == 2
    first, second = bell
    assert first["run_id"] == "swr_a"
    assert first["status"] == "failed"
    assert first["goal"] == "Overnight goal"
    assert first["board_task_id"] == "btk_1"
    assert first["workspace"] == "/w"
    assert first["db_path"] == _dal_db_path(sessions_dal)
    # Card-less / bare rows still surface via the run-ref fallback.
    assert second["run_id"] == "swr_b"
    assert second["board_task_id"] is None
    assert second["workspace"] is None


def test_sweep_with_no_stale_runs_rings_no_bell(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spy_both(monkeypatch)  # both marks return []
    bell = _capture_failure_bell(monkeypatch)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()

    assert bell == []


def test_failure_bell_raising_is_isolated_per_run_and_from_the_loop(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run's bell exploding must not silence the next run's bell or escape
    run_once (best-effort contract)."""
    import omniagentos.notifications.service as notif_service

    def fake_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return []

    def fake_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return [_swept_row("swr_boom"), _swept_row("swr_ok")]

    seen: list[str] = []

    def exploding_notify(**kwargs: Any) -> str | None:
        seen.append(kwargs["run_id"])
        if kwargs["run_id"] == "swr_boom":
            raise RuntimeError("bell exploded")
        return "ntf_ok"

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", fake_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", fake_swarm)
    monkeypatch.setattr(notif_service, "notify_run_terminal_failure", exploding_notify)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()  # must not raise

    assert seen == ["swr_boom", "swr_ok"]


def test_sweep_and_coordinator_double_emission_is_deduped_end_to_end(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the REAL notifier against a tmp DB: a coordinator
    finally-block emit followed by the sweep's emit (or a re-run sweep) yields
    exactly ONE swarm_failed row — the kind-aware read-agnostic dedupe makes
    double-bells structurally impossible."""
    from omniagentos.notifications.dal import NotificationsDal
    from omniagentos.notifications.service import notify_run_terminal_failure

    db_path = _dal_db_path(sessions_dal)

    def fake_orch(self: OrchestrationsDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return []

    def fake_swarm(self: SwarmDal, *, stale_minutes: int) -> list[dict[str, Any]]:
        return [_swept_row("swr_dupe", board_task_id="btk_dupe")]

    monkeypatch.setattr(OrchestrationsDal, "mark_stale_failed", fake_orch)
    monkeypatch.setattr(SwarmDal, "mark_stale_failed", fake_swarm)

    # The coordinator's finally-block already rang the bell for this card...
    assert (
        notify_run_terminal_failure(
            run_id="swr_dupe",
            status="failed",
            goal="Overnight goal",
            board_task_id="btk_dupe",
            db_path=db_path,
            push=False,
        )
        is not None
    )

    supervisor = _make_supervisor(sessions_dal, tmp_path)
    supervisor.run_once()  # ...so the sweep's emit must no-op.

    dal = NotificationsDal(db_path)
    try:
        rows = [r for r in dal.list() if r["ref_id"] == "btk_dupe" and r["kind"] == "swarm_failed"]
    finally:
        dal.close()
    assert len(rows) == 1
