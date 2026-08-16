"""SPEC §7 Phase-3 demo #1, end to end: six submits of ONE unchanged unit.

This is the acceptance test that caught a live defect. ``wq submit`` used to put a
SOFT-parked unit back through ``store.unpark()`` — and unpark DELETES the refusal
row for the unit's last ``input_key`` and zeroes ``attempt``, because it is the
human amnesty for a TERMINAL park. Applied to a soft park it erases the only
counter that can ever reach the storm cap: the sequence observed live was
candidate-defect (a real 1-second gate run!) / unchanged-retry, alternating
forever, ``wq_refusals.count`` oscillating 1↔2, no storm park, no alert. The
failure is SILENT — the pool looks busy while it re-buys the same refusal — which
is exactly the class §4 exists to prevent.

The fix separates the two verbs: ``requeue`` (soft parks; the ledger keeps its
memory) and ``unpark`` (the amnesty; needs a human ``--because``). What follows
drives the REAL store and the REAL worker internals over a throwaway git repo, so
what is asserted is the actual §4.2 → §4.5 chain and not a re-description of it:

  run 1  → the gate really runs, exits 1        → candidate-defect, count=1
  runs 2-5 → refused from the ledger, nothing spent → unchanged-retry, count→5
  run 6  → count >= cap                          → storm-parked, TERMINAL, 1 alert

The load-bearing negative is the run counter: the acceptance command appends a
line to a file outside the worktree, so "the gate ran exactly once across six
submits" is a fact on disk rather than an inference from timing.
"""

from __future__ import annotations

import shlex
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import omniagentos.workqueue.alert as alert_module
import omniagentos.workqueue.cli as cli
from omniagentos.workqueue.schema import REFUSAL_STORM_CAP
from omniagentos.workqueue.store import WorkQueueStore
from omniagentos.workqueue.worker import Worker
from tests.workqueue_cli.demorepo import make_repo, unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((REPO_ROOT / "configs" / "workqueue.yaml").read_text())
MACHINE = "test-machine"

#: The refusal path must cost nothing: no clone, no gate, no agent (§4.2, and the
#: Phase-3 demo asserts it in seconds rather than trusting the shape of the code).
REFUSAL_BUDGET_S = 0.5


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[dict[str, Any]]:
    repo, sha = make_repo(tmp_path / "src")
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    worker = Worker(store, MACHINE, config=CONFIG, home=tmp_path / "wq")
    try:
        yield {"repo": repo, "sha": sha, "store": store, "worker": worker, "tmp": tmp_path}
    finally:
        store.close()


def _always_fails(tmp_path: Path) -> tuple[str, Path]:
    """An acceptance command that always exits 1 and RECORDS that it ran.

    The marker lives outside the worktree on purpose: a write inside it would
    dirty the tree, change the fingerprint, and quietly re-admit every refused
    input — which would make this test prove the opposite of what it claims.
    """
    marker = tmp_path / "gate-runs.log"
    script = tmp_path / "always_fail.py"
    script.write_text(
        "import pathlib, sys\n"
        f"with pathlib.Path({str(marker)!r}).open('a') as fh:\n"
        "    fh.write('ran\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    return f"python3 {shlex.quote(str(script))}", marker


def _rows(store: WorkQueueStore, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(store.db_path) as conn:
        return list(conn.execute(sql, params))


def test_six_submits_of_an_unchanged_unit_storm_park_with_exactly_one_alert(
    bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store: WorkQueueStore = bench["store"]
    worker: Worker = bench["worker"]
    sent: list[dict[str, Any]] = []
    # Patched on the TRANSPORT module, which is where every sender looks it up
    # (worker and server both), so this counts alerts however they were raised.
    monkeypatch.setattr(alert_module, "send_alert", sent.append)
    # The demo is six `wq submit` calls, so the resubmit runs through the CLI —
    # cmd_submit is where the defect actually lived.
    monkeypatch.setattr(cli, "open_queue", lambda server, db: store)

    acceptance_cmd, marker = _always_fails(bench["tmp"])
    submit = unit(bench["sha"], bench["repo"], acceptance_cmd=acceptance_cmd)
    submit["max_attempts"] = 5
    unit_id, _ = store.enqueue(submit)

    outcomes: list[str] = []
    counts: list[int] = []
    elapsed: list[float] = []
    for round_no in range(1, 7):
        stored = store.get_unit(unit_id)
        if stored["state"] == "parked":
            # Every park before the last is SOFT: nothing ran, so nothing is
            # forgiven, and `wq submit` re-queues without touching the ledger.
            assert stored["terminal_reason"] is None, f"round {round_no} parked terminally too soon"
            assert cli.main(["--db", str(store.db_path), "submit", "--unit", unit_id]) == 0
        claim = store.claim(MACHINE, worker.worker_id, [])
        assert claim is not None, f"round {round_no}: the unit must still be claimable"
        started = time.monotonic()
        result = worker.execute(claim)
        elapsed.append(time.monotonic() - started)
        outcomes.append(result.outcome)
        row = store.refusal_check(result.input_key, "raw")
        counts.append(int(row["count"]))

    # 1 — the sequence itself. Exactly one real gate run; four cheap refusals; a
    # terminal storm park on the sixth submit.
    assert outcomes == [
        "candidate-defect",
        "unchanged-retry",
        "unchanged-retry",
        "unchanged-retry",
        "unchanged-retry",
        "storm-parked",
    ], outcomes
    assert marker.read_text().count("ran") == 1, (
        "the acceptance command ran more than once for an unchanged input — the "
        "refusal ledger was cleared between submits"
    )
    assert [row[0] for row in _rows(store, "SELECT outcome FROM wq_attempts ORDER BY attempt")] == (
        outcomes
    ), "every submit must leave one honest attempt row"

    # 2 — ONE refusal row, counted UP across the whole sequence. This is the
    # oscillation the live defect produced (1,2,1,2,...) pinned as a monotone.
    assert counts == [1, 2, 3, 4, 5, 6], counts
    assert len(_rows(store, "SELECT input_key FROM wq_refusals")) == 1, "one input, one row"
    refusal = store.refusal_check(store.list_attempts(unit_id)[0]["input_key"], "raw")
    assert refusal["parked_at"] is not None, "the cap must park the KEY, not just the unit"
    assert refusal["refusal_class"] == "candidate-defect", (
        "the ORIGINAL cause is never overwritten by unchanged-retry/storm-parked "
        "(accurate-gate.py:342) — otherwise the remedy degrades to 'you already asked'"
    )
    assert counts[REFUSAL_STORM_CAP - 1] == REFUSAL_STORM_CAP, "the cap must be reachable at all"

    # 3 — the park itself, and the attempt budget it must NOT have spent.
    stored = store.get_unit(unit_id)
    assert (stored["state"], stored["terminal_reason"]) == ("parked", "storm-parked")
    assert stored["attempt"] == 1, (
        "only the single real run consumed a candidate attempt; a refusal costs "
        "nothing, and a unit that reaches a human with phantom attempts against "
        "it misreports why it stopped"
    )
    assert stored["instrument_retries"] == 0
    # ...and the loop is over: a seventh `wq submit` is refused in the CLI, and
    # nothing is claimable, so no worker can pick the unit up behind our back.
    with pytest.raises(SystemExit, match="TERMINAL"):
        cli.main(["--db", str(store.db_path), "submit", "--unit", unit_id])
    assert store.claim(MACHINE, worker.worker_id, []) is None

    # 4 — exactly one alert for the whole sequence. The storm flips two guards
    # (the refusal row's and the unit's) for ONE event, and the one that fires is
    # the refusal ledger's, on the round the cap is reached: that CAS happens
    # whether or not the unit ever gets to park (it does not when the unit is
    # cancelled mid-flight — tests/workqueue_gate/test_storm_alert_not_dropped.py),
    # so it is the announcement, and record_result suppresses the duplicate.
    # Soft parks announce nothing at all (§4.5).
    assert len(sent) == 1, f"one alert per park (§4.5); got {sent}"
    assert sent[0]["kind"] == "refusal-storm"
    assert sent[0]["count"] == REFUSAL_STORM_CAP
    assert sent[0]["unit_id"] == unit_id
    assert sent[0]["refusal_class"] == "candidate-defect", "the ORIGINAL cause, not the storm"
    assert len(store.alerts()) == 1, "wq alerts must show exactly 1 (Phase-3 demo #1)"
    status = store.status()
    assert status["parks"] == status["alerts_sent"] == 1

    # 5 — the timing, which is the promise a human actually feels: a refused
    # input costs a fraction of a second because NOTHING is spent on it.
    assert max(elapsed[1:]) < REFUSAL_BUDGET_S, (
        f"refusals must be sub-{REFUSAL_BUDGET_S}s (clone and gate both skipped); got {elapsed}"
    )


def test_requeue_keeps_the_ledger_and_unpark_forgives_it(bench: dict[str, Any]) -> None:
    """The two verbs, side by side on the same soft park.

    ``requeue`` is a statement about scheduling; ``unpark`` is a statement about
    the input. Collapsing them (which is what shipped) makes the cap unreachable.
    """
    store: WorkQueueStore = bench["store"]
    worker: Worker = bench["worker"]
    acceptance_cmd, _ = _always_fails(bench["tmp"])
    submit = unit(bench["sha"], bench["repo"], acceptance_cmd=acceptance_cmd)
    unit_id, _ = store.enqueue(submit)

    first = worker.execute(store.claim(MACHINE, worker.worker_id, []))
    assert first.outcome == "candidate-defect"
    second = worker.execute(store.claim(MACHINE, worker.worker_id, []))
    assert second.outcome == "unchanged-retry"
    assert store.get_unit(unit_id)["terminal_reason"] is None  # soft

    store.requeue(unit_id)
    after = store.get_unit(unit_id)
    assert after["state"] == "queued"
    assert after["attempt"] == 1, "requeue must not refund an attempt the code did spend"
    assert store.refusal_check(first.input_key, "raw")["count"] == 2, (
        "requeue must not touch wq_refusals — that count is the storm detector"
    )
    assert after["park_remedy"] is None and after["finished_at"] is None

    # Back to a soft park — same key, same row, one higher count...
    third = worker.execute(store.claim(MACHINE, worker.worker_id, []))
    assert third.outcome == "unchanged-retry"
    assert store.refusal_check(first.input_key, "raw")["count"] == 3

    # ...and only NOW the human amnesty, which is a different statement.
    store.unpark(unit_id, "landed the exemption on main, so the tree the gate reads changed")
    forgiven = store.get_unit(unit_id)
    assert store.refusal_check(first.input_key, "raw") is None, "unpark IS the amnesty"
    assert (forgiven["attempt"], forgiven["instrument_retries"]) == (0, 0)


def test_requeue_refuses_a_terminal_park_and_an_unknown_unit(bench: dict[str, Any]) -> None:
    store: WorkQueueStore = bench["store"]
    unit_id, _ = store.enqueue(unit(bench["sha"], bench["repo"]))

    with pytest.raises(ValueError, match="not parked"):
        store.requeue(unit_id)

    store.park(unit_id, "storm-parked", "change the input, then say what changed")
    with pytest.raises(ValueError, match="TERMINAL"):
        store.requeue(unit_id)
    assert store.get_unit(unit_id)["state"] == "parked", "a refused requeue changes nothing"
    assert store.get_unit(unit_id)["alerted_at"] is not None

    with pytest.raises(KeyError):
        store.requeue("wq_nope")
