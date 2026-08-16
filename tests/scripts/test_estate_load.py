"""estate_load.py — ratio math, verdict exit codes, stale-machine UNKNOWN, DB degrade.

Every load source is monkeypatched or injected: none of these tests depends on
the real sysctl/procfs/getloadavg of the box running the suite — a load gate
whose own tests flake under load would be a joke at its own expense.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ops import estate_load

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_db(path: Path, rows: list[tuple[str, int | None, float | None, str | None, int]]) -> None:
    """Minimal wq_machines with exactly the columns estate_load reads."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE wq_machines ("
        " machine_id TEXT PRIMARY KEY, ncpu INTEGER, last_load1 REAL,"
        " last_seen_at TEXT, drain INTEGER NOT NULL DEFAULT 0)"
    )
    conn.executemany("INSERT INTO wq_machines VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


# ------------------------------------------------------------- verdict math --
@pytest.mark.parametrize(
    ("ratio", "verdict"),
    [
        (0.0, "green"),
        (0.59, "green"),
        (0.6, "amber"),  # boundary: 0.6 is amber, not green
        (0.75, "amber"),
        (0.8, "amber"),  # boundary: 0.8 is amber, not red
        (0.81, "red"),
        (5.0, "red"),
        (None, "unknown"),
    ],
)
def test_verdict_thresholds(ratio: float | None, verdict: str) -> None:
    assert estate_load.verdict_for(ratio) == verdict


def test_local_report_ratio_math() -> None:
    report = estate_load.local_report(load1=12.0, cores=24)
    assert report == {"load1": 12.0, "cores": 24, "ratio": 0.5, "verdict": "green"}


def test_local_report_unknown_load() -> None:
    report = estate_load.local_report(load1=None, cores=8)
    assert report["ratio"] is None
    assert report["verdict"] == "unknown"


# --------------------------------------------------------------- exit codes --
@pytest.mark.parametrize(
    ("load1", "cores", "code"),
    [
        (2.0, 10, 0),  # 0.2 green
        (7.0, 10, 1),  # 0.7 amber
        (9.0, 10, 2),  # 0.9 red
        (None, 10, 1),  # unmeasurable NEVER exits green
    ],
)
def test_main_exit_codes(
    monkeypatch: pytest.MonkeyPatch, load1: float | None, cores: int, code: int
) -> None:
    monkeypatch.setattr(estate_load, "read_load1", lambda: load1)
    monkeypatch.setattr(estate_load, "read_cores", lambda: cores)
    assert estate_load.main([]) == code


def test_main_prints_load_cores_ratio_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(estate_load, "read_load1", lambda: 4.6)
    monkeypatch.setattr(estate_load, "read_cores", lambda: 24)
    assert estate_load.main([]) == 0
    assert capsys.readouterr().out.strip() == "4.60 24 0.19 green"


# -------------------------------------------------------------------- fleet --
def test_fleet_stale_machine_is_unknown_and_never_best(tmp_path: Path) -> None:
    db = tmp_path / "wq.sqlite3"
    _make_db(
        db,
        [
            # fresh + busy
            ("busy-box", 10, 9.0, _iso(NOW - timedelta(seconds=30)), 0),
            # fresh + idle -> best
            ("idle-box", 16, 0.5, _iso(NOW - timedelta(seconds=30)), 0),
            # idlest ratio of all, but STALE (>10 min): UNKNOWN, never best
            ("stale-box", 64, 0.1, _iso(NOW - timedelta(seconds=601)), 0),
            # never seen at all
            ("silent-box", 8, 1.0, None, 0),
        ],
    )
    machines, error = estate_load.fleet_report(db, now=NOW)
    assert error is None
    by_id = {m["machine"]: m for m in machines}
    assert by_id["busy-box"]["verdict"] == "red"
    assert by_id["idle-box"]["verdict"] == "green"
    assert by_id["stale-box"]["verdict"] == "unknown"
    assert by_id["stale-box"]["stale"] is True
    assert by_id["silent-box"]["verdict"] == "unknown"
    assert estate_load.best_machine(machines) == "idle-box"


def test_fleet_draining_machine_is_never_best(tmp_path: Path) -> None:
    db = tmp_path / "wq.sqlite3"
    _make_db(
        db,
        [
            ("draining-idle", 16, 0.2, _iso(NOW - timedelta(seconds=10)), 1),
            ("normal", 16, 4.0, _iso(NOW - timedelta(seconds=10)), 0),
        ],
    )
    machines, _ = estate_load.fleet_report(db, now=NOW)
    assert estate_load.best_machine(machines) == "normal"


def test_fleet_no_fresh_machines_yields_no_best(tmp_path: Path) -> None:
    db = tmp_path / "wq.sqlite3"
    _make_db(db, [("stale-box", 8, 0.5, _iso(NOW - timedelta(hours=2)), 0)])
    machines, _ = estate_load.fleet_report(db, now=NOW)
    assert estate_load.best_machine(machines) is None


def test_fleet_missing_db_degrades_to_local_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(estate_load, "read_load1", lambda: 1.0)
    monkeypatch.setattr(estate_load, "read_cores", lambda: 10)
    missing = tmp_path / "nope" / "wq.sqlite3"
    code = estate_load.main(["--fleet", "--db", str(missing)])
    captured = capsys.readouterr()
    assert code == 0  # the LOCAL verdict still decides
    assert "unavailable" in captured.err
    assert "best:" in captured.out  # section still rendered, honestly empty


def test_fleet_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "wq.sqlite3"
    _make_db(db, [("idle-box", 16, 0.5, _iso(datetime.now(UTC)), 0)])
    monkeypatch.setattr(estate_load, "read_load1", lambda: 9.0)
    monkeypatch.setattr(estate_load, "read_cores", lambda: 10)
    code = estate_load.main(["--fleet", "--json", "--db", str(db)])
    assert code == 2  # local red wins the exit code even with an idle fleet
    payload = json.loads(capsys.readouterr().out)
    assert payload["local"]["verdict"] == "red"
    assert payload["best"] == "idle-box"
    assert payload["fleet"][0]["machine"] == "idle-box"
    assert payload["fleet_error"] is None
