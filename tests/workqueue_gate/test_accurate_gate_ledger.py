"""scripts/accurate-gate.py against a POOL-WIDE refusal ledger.

The port's whole reason to exist is that AccurateGate kept refusals in a local
state file, so a second machine would happily spend a full gate run on the input
the first one refused five times. These tests assert the ledger behaviours the
demo (SPEC §7, Phase 3) is graded on:

  run 1 executes the gate · runs 2-5 refuse as unchanged-retry in ~0.2s having
  spent NO gate run · run 6 is storm-parked · a pass DELETES the row · a gate
  upgrade re-admits · an instrument-error is NOT blocked from re-running, because
  its remedy requires re-running the same key.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.workqueue_cli.demorepo import make_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "accurate-gate.py"
GATE = "probe-gate"


@pytest.fixture(scope="module")
def gate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("accurate_gate_under_test", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bench(
    tmp_path: Path, gate_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    repo, _sha = make_repo(tmp_path / "wt")
    gates_d = tmp_path / "gates.d"
    gates_d.mkdir()
    counter = tmp_path / "gate-runs.txt"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')\n"
        "sys.exit(int(sys.argv[1]))\n"
    )
    (gates_d / f"{GATE}.yaml").write_text(
        f"command: {sys.executable} {probe} 1\n"
        "workdir: {workdir}\n"
        "input_key:\n"
        "  - tree:{workdir}\n"
        "  - cmd:probe\n"
        "invariants:\n"
        "  - git-clean {workdir}\n"
        "evidence:\n"
        f"  dir: {tmp_path / 'evidence'}\n"
        "retry:\n"
        "  unchanged_input: refuse\n"
        "  max_attempts: 5\n"
    )
    monkeypatch.setattr(gate_module, "GATES_D", gates_d)
    # The REAL ledger over the REAL store: the pool-wide refusal row is the whole
    # point of the port, so a double here would test the double.
    ledger = gate_module.Ledger("full", str(tmp_path / "wq.sqlite3"), None)
    try:
        yield {
            "repo": repo,
            "ledger": ledger,
            "counter": counter,
            "gates_d": gates_d,
            "evidence": tmp_path / "evidence",
        }
    finally:
        ledger.q.close()


def _run(gate_module: ModuleType, bench: dict[str, Any]) -> tuple[int, float]:
    started = time.time()
    with pytest.raises(SystemExit) as exc:
        gate_module.run(GATE, {"workdir": str(bench["repo"])}, ledger=bench["ledger"])
    return int(exc.value.code), time.time() - started


def _runs(bench: dict[str, Any]) -> int:
    counter = bench["counter"]
    return int(counter.read_text()) if counter.exists() else 0


def test_first_run_grades_then_the_input_is_refused_unspent(
    gate_module: ModuleType, bench: dict[str, Any]
) -> None:
    code, _ = _run(gate_module, bench)
    assert code == gate_module.EXIT_FAIL  # graded: the subject is at fault
    assert _runs(bench) == 1

    for attempt in range(2, 6):
        code, elapsed = _run(gate_module, bench)
        assert code == gate_module.EXIT_COULD_NOT_RUN, f"run {attempt} should refuse, not grade"
        # The timing is an assertion, not a vibe (SPEC §7 Phase 3).
        assert elapsed < 0.5, f"unchanged-retry took {elapsed:.2f}s — it must not run the gate"
        assert _runs(bench) == 1, "a refused input must never spend a gate run"

    row = bench["ledger"].q.refusal_check(_only_key(bench), GATE)
    assert row["count"] == 5
    # The ORIGINAL cause is never overwritten by unchanged-retry.
    assert row["refusal_class"] == "candidate-defect"

    code, _ = _run(gate_module, bench)
    assert code == gate_module.EXIT_COULD_NOT_RUN
    assert _runs(bench) == 1
    receipt = _latest_receipt(bench)
    assert receipt["class"] == "storm-parked"
    assert receipt["retryable"] is False
    # ONE exit-code story: the receipt and the process agree by construction.
    assert receipt["exit_code"] == gate_module.exit_code_for(receipt["class"]) == code


def test_a_retryable_class_falls_through_and_a_pass_deletes_the_row(
    gate_module: ModuleType, bench: dict[str, Any]
) -> None:
    """Two rules at once, because they are the same rule seen twice.

    An ``instrument-error`` row must NOT refuse the next run: the instrument is
    not part of the fingerprint, so a repaired instrument never changes the key
    and blocking it would make its own remedy unfollowable. And when the run then
    passes, the row is DELETED — the ledger can only ever refuse harder; it has
    no path that emits a cached pass.
    """
    config = (bench["gates_d"] / f"{GATE}.yaml").read_text().replace("probe.py 1", "probe.py 0")
    (bench["gates_d"] / f"{GATE}.yaml").write_text(config)
    key = gate_module.input_key(gate_module._load_cfg(GATE, {"workdir": str(bench["repo"])}))
    bench["ledger"].q.refusal_record(key, GATE, "instrument-error", 1, "seeded: repair the box")
    assert bench["ledger"].q.refusal_check(key, GATE)["count"] == 1

    code, _ = _run(gate_module, bench)
    assert code == gate_module.EXIT_PASS
    assert _runs(bench) == 1, "a retryable class must not be blocked from re-running"
    assert _refusal_keys(bench) == set(), "a pass must DELETE the refusal row"


def test_a_dirty_tree_is_an_instrument_error_that_may_be_retried(
    gate_module: ModuleType, bench: dict[str, Any]
) -> None:
    (bench["repo"] / "demo" / "junk").write_text("someone else's file\n")
    code, _ = _run(gate_module, bench)
    assert code == gate_module.EXIT_COULD_NOT_RUN
    receipt = _latest_receipt(bench)
    assert receipt["class"] == "instrument-error"
    assert receipt["retryable"] is True
    assert "INSTRUMENT" in receipt["remedy"]
    # gate_exit_code is NULL because the gate never ran: a fabricated measurement
    # is worse than an absent one.
    assert receipt["gate_exit_code"] is None
    assert _runs(bench) == 0, "the preflight refused BEFORE the candidate was graded"


def test_caller_supplied_input_key_is_recorded_as_such(
    gate_module: ModuleType, bench: dict[str, Any]
) -> None:
    with pytest.raises(SystemExit):
        gate_module.run(
            GATE,
            {"workdir": str(bench["repo"])},
            ledger=bench["ledger"],
            key_override="deadbeef" * 8,
        )
    receipt = _latest_receipt(bench)
    assert receipt["input_key"] == "deadbeef" * 8
    assert receipt["input_key_source"] == "caller"
    assert bench["ledger"].q.refusal_check("deadbeef" * 8, GATE) is not None


def _refusal_keys(bench: dict[str, Any]) -> set[str]:
    """Every input_key in the shared ledger, read back out of the real DB."""
    with sqlite3.connect(bench["ledger"].q.db_path) as conn:
        return {str(row[0]) for row in conn.execute("SELECT input_key FROM wq_refusals")}


def _only_key(bench: dict[str, Any]) -> str:
    keys = _refusal_keys(bench)
    assert len(keys) == 1, keys
    return keys.pop()


def _latest_receipt(bench: dict[str, Any]) -> dict[str, Any]:
    root = bench["evidence"] / GATE
    newest = max(root.glob("*/receipt.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text())
