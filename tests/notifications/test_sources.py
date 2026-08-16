"""Each real notification source persists a linked, inspectable notification."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.notifications import service
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.steward.alerts.monitor import _persist_candidate
from omniagentos.steward.alerts.rules import AlertCandidate
from omniagentos.steward.config import AlertsConfig, StewardConfig
from omniagentos.steward.store import StewardStore


@pytest.fixture(autouse=True)
def _no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_push", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _clear_dal_cache() -> None:
    service._DAL_CACHE.clear()


def _candidate(severity: str) -> AlertCandidate:
    return AlertCandidate(
        rule="roas_floor",
        severity=severity,
        title=f"{severity} alert",
        body="body text",
        evidence={"k": "v"},
        cooldown_key=f"k-{severity}",
    )


def test_high_alert_persists_notification(tmp_path: Path) -> None:
    database = SqliteStore(str(tmp_path / "alerts.db"))
    steward = StewardStore(database)
    cfg = StewardConfig(alerts=AlertsConfig())
    assert _persist_candidate(_candidate("high"), steward=steward, database=database, cfg=cfg)

    notes = NotificationsDal(database._connection).list()
    assert len(notes) == 1
    assert notes[0]["kind"] == "alert"
    assert notes[0]["ref_type"] == "alert"
    assert notes[0]["severity"] == "high"


def test_low_alert_does_not_persist_notification(tmp_path: Path) -> None:
    database = SqliteStore(str(tmp_path / "alerts2.db"))
    steward = StewardStore(database)
    cfg = StewardConfig(alerts=AlertsConfig())
    assert _persist_candidate(_candidate("info"), steward=steward, database=database, cfg=cfg)
    assert NotificationsDal(database._connection).list() == []


def test_runner_default_notifier_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagentos.runner import core

    db = str(tmp_path / "runner.db")
    monkeypatch.setattr(core, "default_db_path", lambda: db)
    core._default_approval_notifier(
        {
            "id": "apr_5",
            "run_id": "run_1",
            "proposed_action": "deploy",
            "action_class": "consequential",
            "risk": "high",
        }
    )
    notes = NotificationsDal(db).list()
    assert len(notes) == 1
    assert notes[0]["ref_type"] == "approval"
    assert notes[0]["ref_id"] == "apr_5"
    assert notes[0]["severity"] == "high"


def test_runner_dependencies_load_wires_notifier() -> None:
    from omniagentos.runner.core import RunnerDependencies, _default_approval_notifier

    deps = RunnerDependencies.load()
    assert deps.notify_approval is _default_approval_notifier
