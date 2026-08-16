"""`wq` end to end.

Two layers, on purpose, both against the REAL store on a tmp DB:
  * in-process — covers every command's argument handling and the refusals that
    protect a terminal park;
  * out-of-process — the SPEC §7 Phase-1 acceptance sequence (`init` → `enqueue`
    → `status`) actually shelled the way a human would shell it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import omniagentos.workqueue.cli as cli
from omniagentos.workqueue.store import WorkQueueStore

REPO_ROOT = Path(__file__).resolve().parents[2]

MACHINE = {
    "machine_id": "mac-studio",
    "hostname": "h",
    "os": "darwin",
    "labels": ["build"],
    "max_concurrent": 3,
    "ncpu": 24,
    "perf_cores": 16,
    "mem_gb": 64.0,
}

SUBMIT = {
    "idempotency_key": "cli-smoke-1",
    "repo_url": "https://example.invalid/repo.git",
    "repo_slug": "repo",
    "base_sha": "a" * 40,
    "branch": "wq/cli-smoke",
    "owned_paths": ["demo/**"],
    "agent_profile": "script",
    "acceptance_cmd": "python3 -c 'print(1)'",
    "risk_class": "mechanical",
}


@pytest.fixture
def wq(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[WorkQueueStore]:
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    monkeypatch.setattr(cli, "open_queue", lambda server, db: store)
    try:
        yield store
    finally:
        store.close()


def _run(*argv: str) -> int:
    return cli.main(["--db", "/tmp/ignored.sqlite3", *argv])


def _drain_of(wq: WorkQueueStore, machine_id: str) -> int:
    (row,) = [m for m in wq.list_machines() if m["machine_id"] == machine_id]
    return int(row["drain"])


def test_enqueue_is_idempotent(wq: WorkQueueStore, tmp_path: Path, capsys) -> None:
    path = tmp_path / "units.jsonl"
    path.write_text(json.dumps(SUBMIT) + "\n" + json.dumps(SUBMIT) + "\n")
    assert _run("enqueue", "--file", str(path)) == 0
    out = capsys.readouterr().out
    assert "-- 1 queued, 1 deduplicated" in out
    assert wq.status()["depth"]["queued"] == 1


def test_enqueue_json_and_machines_and_alerts(wq: WorkQueueStore, capsys) -> None:
    assert _run("enqueue", "--json", json.dumps(SUBMIT)) == 0
    wq.enroll_machine(dict(MACHINE))
    assert _run("machines") == 0
    assert "mac-studio" in capsys.readouterr().out
    assert _run("alerts", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_drain_and_undo(wq: WorkQueueStore, capsys) -> None:
    wq.enroll_machine(dict(MACHINE))
    assert _run("drain", "mac-studio") == 0
    assert _drain_of(wq, "mac-studio") == 1
    assert _run("drain", "mac-studio", "--undo") == 0
    assert _drain_of(wq, "mac-studio") == 0
    assert "resumed" in capsys.readouterr().out


def test_unpark_requires_a_reason(wq: WorkQueueStore) -> None:
    unit_id, _ = wq.enqueue(SUBMIT)
    wq.park(unit_id, "attempts-exhausted", "fix the cause")
    with pytest.raises(SystemExit):
        _run("unpark", unit_id, "--because", "   ")
    assert _run("unpark", unit_id, "--because", "fixed the failing assertion") == 0
    assert wq.get_unit(unit_id)["state"] == "queued"


def test_submit_refuses_a_terminal_park_without_because(wq: WorkQueueStore, capsys) -> None:
    """A storm-parked unit is terminal: re-queueing it silently is the busy-loop
    the ledger exists to stop."""
    unit_id, _ = wq.enqueue(SUBMIT)
    wq.park(unit_id, "storm-parked", "change the input")
    with pytest.raises(SystemExit) as exc:
        _run("submit", "--unit", unit_id)
    assert "TERMINAL" in str(exc.value)
    assert _run("submit", "--unit", unit_id, "--because", "landed the exemption on main") == 0
    assert wq.get_unit(unit_id)["state"] == "queued"


def test_submit_clears_a_soft_park_freely(wq: WorkQueueStore) -> None:
    unit_id, _ = wq.enqueue(SUBMIT)
    # A SOFT park is a park with no terminal_reason (§4.2 unchanged-retry).
    wq.park(unit_id, None, "change the input, then re-submit")
    assert wq.get_unit(unit_id)["terminal_reason"] is None
    assert _run("submit", "--unit", unit_id) == 0
    assert wq.get_unit(unit_id)["state"] == "queued"


def test_cancel_and_reap(wq: WorkQueueStore, capsys) -> None:
    unit_id, _ = wq.enqueue(SUBMIT)
    assert _run("cancel", unit_id) == 0
    assert wq.get_unit(unit_id)["state"] == "cancelled"
    assert _run("reap") == 0
    assert "reclaimed 0" in capsys.readouterr().out


def test_a_queue_with_no_home_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WQ_DB", raising=False)
    monkeypatch.delenv("WQ_SERVER", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["status"])


@pytest.mark.smoke
def test_phase1_acceptance_sequence_out_of_process(tmp_path: Path) -> None:
    db = tmp_path / "workqueue.sqlite3"
    units = tmp_path / "units.jsonl"
    units.write_text(json.dumps(SUBMIT) + "\n")

    def wq_cli(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "omniagentos.workqueue.cli", "--db", str(db), *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    assert wq_cli("init").returncode == 0
    # The queue DB must contain ONLY wq_* and schema_migrations (SPEC §1.2).
    import sqlite3

    tables = {
        row[0]
        for row in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables and all(t.startswith(("wq_", "schema_migrations", "sqlite_")) for t in tables), (
        tables
    )

    enqueued = wq_cli("enqueue", "--file", str(units))
    assert enqueued.returncode == 0, enqueued.stderr
    status = wq_cli("status", "--json")
    assert status.returncode == 0, status.stderr
    payload: dict[str, Any] = json.loads(status.stdout)
    assert payload["depth"].get("queued") == 1
    assert wq_cli("status").returncode == 0
