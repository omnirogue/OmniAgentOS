"""WP10: the supervisor's ONE-SHOT startup swarm resume sweep.

``serve_forever`` runs ``resume_swarms_once`` exactly once at startup (after
PID reconcile, before the poll loop); ``run_once`` never triggers it — the
per-pass stale handling stays A5's ``_mark_stale_runs``. Best-effort: a sweep
fault must never block supervisor startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
import omniagentos.swarm.scheduler as scheduler_module
from omniagentos.intake import service
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import SessionSupervisor, _dal_db_path


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    service._reset_reconcile_stale_throttle()
    yield
    service._reset_reconcile_stale_throttle()


def _make_supervisor(sessions_dal: SessionsDal, tmp_path: Path) -> SessionSupervisor:
    return SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _title, _body: None,
    )


def test_serve_forever_runs_resume_sweep_once(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_module,
        "resume_stale_swarms",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    supervisor = _make_supervisor(sessions_dal, tmp_path)
    supervisor._stop.set()  # startup only: skip the poll loop entirely

    supervisor.serve_forever()

    assert len(calls) == 1
    assert calls[0]["db_path"] == _dal_db_path(sessions_dal)

    # Idempotent: a second explicit call is a no-op (once per lifetime).
    supervisor.resume_swarms_once()
    assert len(calls) == 1


def test_run_once_never_triggers_the_sweep(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_module,
        "resume_stale_swarms",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.run_once()
    supervisor.run_once()

    assert calls == []


def test_sweep_fault_never_blocks_startup(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(scheduler_module, "resume_stale_swarms", _boom)
    supervisor = _make_supervisor(sessions_dal, tmp_path)

    supervisor.resume_swarms_once()  # must not raise
    assert supervisor._swarm_resume_done is True
