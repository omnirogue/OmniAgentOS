"""``health_sentinel.check_slack_socket`` — the OBSERVER half of the hybrid.

A hybrid whose repair mechanism cannot be seen is a hybrid with no repair
mechanism: the socket row stays green while the sweep is dead, or a real catch
is erased before anyone reads it. Every test here is a regression for a way this
check said "healthy" about something that was not.

The script is standalone (stdlib + sqlite3, no package import) so it is loaded
by path, exactly the way launchd runs it.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "health_sentinel.py"
)


def _load() -> Any:
    name = "health_sentinel_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves its own module out of
    # sys.modules, and an unregistered one raises during class creation.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sentinel = _load()


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE comms_sources (name TEXT PRIMARY KEY, kind TEXT, status TEXT,"
        " config_json TEXT, last_poll_at TEXT, last_error TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(path))
    return path


def _source(
    db_path: Path,
    name: str,
    *,
    kind: str,
    status: str = "active",
    config: dict[str, Any] | None = None,
    last_poll_at: str | None = None,
    last_error: str = "",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO comms_sources (name, kind, status, config_json, last_poll_at, last_error)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, status=excluded.status,"
        " config_json=excluded.config_json, last_poll_at=excluded.last_poll_at,"
        " last_error=excluded.last_error",
        (
            name,
            kind,
            status,
            json.dumps(config or {}),
            last_poll_at if last_poll_at is not None else _iso(datetime.now(UTC)),
            last_error,
        ),
    )
    conn.commit()
    conn.close()


def _healthy(db_path: Path, **overrides: Any) -> dict[str, Any]:
    """Both halves green: a live socket heartbeat and a fresh sweep over 2 channels."""
    socket_config = {
        "disconnects": 0,
        "store_failures": 0,
        "store_slow_writes": 0,
        "last_disconnect_reason": "",
        "store_latency_ms_max": 12,
    }
    socket_config.update(overrides.pop("socket_config", {}))
    poller_config = {
        "reconciled_total": 0,
        "reconciled_last_count": 0,
        "reconciled_last_at": "",
        "member_channels": 2,
        "channels_swept": 2,
        "channel_error_count": 0,
        "channel_errors": [],
    }
    poller_config.update(overrides.pop("poller_config", {}))
    _source(db_path, "slack-socket", kind="slack_socket", config=socket_config)
    _source(db_path, "slack", kind="slack", config=poller_config, **overrides)
    return {"socket": socket_config, "poller": poller_config}


def test_both_halves_healthy_is_ok(db: Path, tmp_path: Path) -> None:
    _healthy(db)
    result = sentinel.check_slack_socket(watermark_path=tmp_path / "wm.json")
    assert result.status == sentinel.OK, result.evidence


def test_a_catch_is_reported_even_when_a_later_clean_sweep_has_erased_the_count(
    db: Path, tmp_path: Path
) -> None:
    """THE defect this watermark exists for.

    ``reconciled_last_count`` is rewritten by every sweep. The sweep runs at 300s
    and this sentinel at 1800s, so five real catches in six were overwritten
    before anyone looked — the most important operator signal in the design was
    wired to a field that self-clears.
    """
    watermark = tmp_path / "wm.json"
    _healthy(db, poller_config={"reconciled_total": 0})
    baseline = sentinel.check_slack_socket(watermark_path=watermark)
    assert baseline.status == sentinel.OK

    # A sweep catches 3 messages the socket missed... and then five clean sweeps
    # run before the sentinel wakes up, resetting last_count to 0.
    _healthy(
        db,
        poller_config={
            "reconciled_total": 3,
            "reconciled_last_count": 0,
            "reconciled_last_at": _iso(datetime.now(UTC) - timedelta(hours=9)),
        },
    )

    result = sentinel.check_slack_socket(watermark_path=watermark)
    assert result.status == sentinel.FAIL
    assert "caught 3" in result.evidence
    assert result.detail["reconciled_total"] == 3


def test_the_watermark_does_not_re_alarm_on_history_it_already_reported(
    db: Path, tmp_path: Path
) -> None:
    watermark = tmp_path / "wm.json"
    _healthy(db, poller_config={"reconciled_total": 0})
    sentinel.check_slack_socket(watermark_path=watermark)
    _healthy(db, poller_config={"reconciled_total": 3, "reconciled_last_at": ""})
    assert sentinel.check_slack_socket(watermark_path=watermark).status == sentinel.FAIL
    assert sentinel.check_slack_socket(watermark_path=watermark).status == sentinel.OK


def test_the_first_run_records_a_baseline_rather_than_alarming_on_history(
    db: Path, tmp_path: Path
) -> None:
    """Deploying the sentinel next to an existing counter must not fire a FAIL
    about messages that were reconciled before it was watching."""
    _healthy(db, poller_config={"reconciled_total": 41, "reconciled_last_at": ""})
    watermark = tmp_path / "wm.json"
    assert sentinel.check_slack_socket(watermark_path=watermark).status == sentinel.OK
    assert json.loads(watermark.read_text())["reconciled_total"] == 41


def test_a_flapping_socket_is_not_healthy(db: Path, tmp_path: Path) -> None:
    """A socket that drops every 20s never exceeds the supervisor's 30s grace, so
    status stays ``active`` and the heartbeat stays seconds old — while every gap
    loses thread replies the sweep can never backfill."""
    watermark = tmp_path / "wm.json"
    _healthy(db)
    sentinel.check_slack_socket(watermark_path=watermark)

    _healthy(
        db,
        socket_config={"disconnects": 90, "last_disconnect_reason": "websocket close code=1006"},
    )
    result = sentinel.check_slack_socket(watermark_path=watermark)

    assert result.status == sentinel.FAIL
    assert "FLAPPING" in result.evidence
    assert "1006" in result.evidence, "the reason distinguishes a refresh from a transport error"


def test_ordinary_hourly_connection_refresh_is_not_an_alarm(db: Path, tmp_path: Path) -> None:
    watermark = tmp_path / "wm.json"
    _healthy(db)
    sentinel.check_slack_socket(watermark_path=watermark)
    _healthy(db, socket_config={"disconnects": 1, "last_disconnect_reason": "refresh_requested"})
    assert sentinel.check_slack_socket(watermark_path=watermark).status == sentinel.OK


def test_a_sweep_that_covers_zero_channels_is_a_failure(db: Path, tmp_path: Path) -> None:
    """The safety net exists or it does not. A sweep over no channels reconciles
    nothing, so `created == 0` proves nothing at all."""
    _healthy(db, poller_config={"member_channels": 0, "channels_swept": 0})
    result = sentinel.check_slack_socket(watermark_path=tmp_path / "wm.json")
    assert result.status == sentinel.FAIL
    assert "ZERO channels" in result.evidence


def test_channels_the_sweep_could_not_read_are_surfaced(db: Path, tmp_path: Path) -> None:
    _healthy(
        db,
        poller_config={
            "channel_error_count": 2,
            "channel_errors": ["C0AAA: Slack API error (HTTP 500)", "C0BBB: ratelimited"],
        },
    )
    result = sentinel.check_slack_socket(watermark_path=tmp_path / "wm.json")
    assert result.status == sentinel.WARN
    assert "C0AAA" in result.evidence


def test_a_failing_sweep_is_a_failure_even_with_a_perfect_socket(db: Path, tmp_path: Path) -> None:
    _healthy(db)
    _source(
        db,
        "slack",
        kind="slack",
        status="error",
        config={"member_channels": 2},
        last_error="slack poll failed: every member channel errored",
    )
    result = sentinel.check_slack_socket(watermark_path=tmp_path / "wm.json")
    assert result.status == sentinel.FAIL
    assert "SWEEP is failing" in result.evidence


def test_store_failures_and_slow_writes_are_watched_between_runs(db: Path, tmp_path: Path) -> None:
    watermark = tmp_path / "wm.json"
    _healthy(db)
    sentinel.check_slack_socket(watermark_path=watermark)

    _healthy(db, socket_config={"store_slow_writes": 4})
    warned = sentinel.check_slack_socket(watermark_path=watermark)
    assert warned.status == sentinel.WARN
    assert "store write(s) took over" in warned.evidence

    _healthy(db, socket_config={"store_slow_writes": 4, "store_failures": 1})
    failed = sentinel.check_slack_socket(watermark_path=watermark)
    assert failed.status == sentinel.FAIL
    assert "failed to store" in failed.evidence


def test_a_stale_sweep_still_fails_loudest(db: Path, tmp_path: Path) -> None:
    _healthy(db, last_poll_at=_iso(datetime.now(UTC) - timedelta(hours=3)))
    result = sentinel.check_slack_socket(watermark_path=tmp_path / "wm.json")
    assert result.status == sentinel.FAIL
    assert "SWEEP" in result.evidence


def test_the_check_is_registered_in_the_sentinel_run(db: Path) -> None:
    assert any(name == "slack_socket" for name, _fn in sentinel.CHECKS)
