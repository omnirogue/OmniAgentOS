"""The kill drill's assertions: what a process that stopped existing left behind.

Every test here spawns a real child interpreter that runs one real tick against
real sqlite and dies with ``os._exit(9)`` at a named instant. Nothing is mocked
and nothing is patched — the only difference between a drilled tick and a
scheduled one is when the process ends.

Three claims, and they are the package's whole crash-safety story:

1. A crash between the irreversible write and the receipt completion leaves the
   effect's state UNKNOWN. The next process settles ``ABORTED`` carrying
   ``EffectStateUnknown`` and **does not re-send** — not on the next tick, and
   not ever, until a human reconciles it.
2. A crash between the receipt completion and the checkpoint commit is
   recoverable in the good direction. The next process re-enters the effect node,
   the receipt turns that re-entry into a replay, and the tick settles
   ``COMPLETED`` with ``resumed=True``.
3. Three clean processes, each running the same tick from scratch, produce
   exactly ONE effect.

The oracle is an append-only file that lives outside sqlite and outside the
checkpoint (see :mod:`tests.drills.kill_drill`). It is fsynced before each kill
point, so an assertion about "how many times did the world get touched" does not
depend on the dying interpreter having flushed anything.

**A killed child must exit 9.** That is asserted directly rather than assumed:
a drill whose child exited 0 did not crash, and every conclusion drawn from that
run would be about a tidy shutdown instead of a kill.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

try:  # pytest's rootdir insertion differs depending on whether tests/ is a package
    from drills import kill_drill
except ImportError:  # pragma: no cover - taken only under the other layout
    from tests.drills import kill_drill

from selfloop.lease import flock_available

pytestmark = pytest.mark.skipif(
    not flock_available(),
    reason=(
        "the kill drill holds a FlockLease, which needs POSIX fcntl.flock. On a host "
        "without it the drill would run under a lease that protects nothing between "
        "processes, and a green result would mean nothing"
    ),
)


def receipt_rows(workdir: Path) -> list[dict[str, object]]:
    """Every receipt row in the drill's database, read directly.

    Read with plain ``sqlite3`` rather than through ``SqliteReceiptStore``,
    because the question is what is DURABLE on disk after a process was killed,
    and asking the package's own reader would let a bug in that reader answer a
    question about the file.
    """
    path = workdir / kill_drill.DB_NAME
    if not path.is_file():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM receipts ORDER BY key")]
    finally:
        conn.close()


def run(workdir: Path, kill_point: str) -> tuple[int, str]:
    """Spawn one drill child; return ``(exit status, its stderr)``."""
    completed = kill_drill.run_child(workdir, kill_point)
    return completed.returncode, completed.stderr


def test_a_clean_tick_touches_the_world_once_and_settles_completed(tmp_path: Path) -> None:
    """The control case. Without it, every other test here proves only that nothing ran."""
    status, stderr = run(tmp_path, kill_drill.KILL_NONE)

    assert status == 0, f"the control tick did not survive: {stderr}"
    assert len(kill_drill.effects(tmp_path)) == 1
    tick = kill_drill.ticks(tmp_path)[-1]
    assert tick["status"] == "completed"
    assert tick["resumed"] is False


def test_three_clean_processes_produce_exactly_one_effect(tmp_path: Path) -> None:
    """The exactly-once claim, in the case with no crash in it at all.

    Every tick observes the same subject and therefore derives the same business
    key, so ticks two and three reach the effect node with a succeeded receipt
    already in the store. Each of them completes — the loop is not stuck — and
    neither of them touches the world again.
    """
    for _ in range(3):
        status, stderr = run(tmp_path, kill_drill.KILL_NONE)
        assert status == 0, stderr

    assert len(kill_drill.effects(tmp_path)) == 1
    recorded = kill_drill.ticks(tmp_path)
    assert len(recorded) == 3
    assert [tick["status"] for tick in recorded] == ["completed"] * 3
    # Ticks 2 and 3 are FRESH ticks, not resumes: nothing was in flight. The
    # receipt is what makes them no-ops, not the checkpoint.
    assert [tick["resumed"] for tick in recorded] == [False, False, False]


def test_a_killed_child_dies_without_running_any_cleanup(tmp_path: Path) -> None:
    """``os._exit(9)``: no ``finally``, no ``atexit``, no buffered write.

    Pinned explicitly because it is the drill's own load-bearing assumption. The
    tick line is written after ``run_once`` returns, inside the same function
    whose ``finally`` closes the database; a child that produced one had unwound
    normally, and the whole suite below it would be measuring a graceful
    shutdown wearing a crash's name.
    """
    status, _ = run(tmp_path, kill_drill.KILL_INSIDE_TOOL)

    assert status == kill_drill.KILL_STATUS
    assert kill_drill.ticks(tmp_path) == []
    assert len(kill_drill.effects(tmp_path)) == 1


def test_a_crash_before_completion_leaves_a_claim_with_no_result(tmp_path: Path) -> None:
    """The UNKNOWN state, on disk: claimed, never completed.

    This is the row that every later refusal is derived from, so it is asserted
    on its own. ``result_json IS NULL`` is the absence of a stored value rather
    than a stored value, which is exactly what makes it survive a process that
    had no chance to write anything.
    """
    status, _ = run(tmp_path, kill_drill.KILL_INSIDE_TOOL)
    assert status == kill_drill.KILL_STATUS

    rows = receipt_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["result_json"] is None
    assert rows[0]["claimed_at"]


def test_a_crash_before_completion_resumes_aborted_and_does_not_resend(tmp_path: Path) -> None:
    """Claim 1. The effect MAY have happened, so no later process may repeat it.

    The resumed tick reports ``ABORTED`` — adverse, so it trips the acceptance
    floor and reaches an operator — and names ``EffectStateUnknown`` in its
    detail. Then a THIRD process runs, and the refusal is asserted again: this
    fails closed *forever*, not once. A guard that unsticks itself after a tick
    is the double-billing bug with a delay in front of it.
    """
    killed, _ = run(tmp_path, kill_drill.KILL_INSIDE_TOOL)
    assert killed == kill_drill.KILL_STATUS

    resumed_status, stderr = run(tmp_path, kill_drill.KILL_NONE)
    assert resumed_status == 0, stderr

    resumed = kill_drill.ticks(tmp_path)[-1]
    assert resumed["status"] == "aborted"
    assert resumed["resumed"] is True
    assert "EffectStateUnknown" in str(resumed["detail"])
    assert len(kill_drill.effects(tmp_path)) == 1

    again_status, stderr = run(tmp_path, kill_drill.KILL_NONE)
    assert again_status == 0, stderr
    assert kill_drill.ticks(tmp_path)[-1]["status"] == "aborted"
    assert len(kill_drill.effects(tmp_path)) == 1, "a later tick re-sent an unknown effect"


def test_a_crash_after_completion_replays_the_receipt_and_settles_completed(
    tmp_path: Path,
) -> None:
    """Claim 2. The effect happened, the receipt says so, the checkpoint never landed.

    The child dies between ``ReceiptStore.complete`` returning and the executor
    committing the checkpoint that would have named the next node, so the
    resumed process re-enters the effect node. That re-entry is not a defect —
    it is the price of durability — and the receipt is the thing that turns it
    into a replay rather than a second send.
    """
    killed, _ = run(tmp_path, kill_drill.KILL_AFTER_RECEIPT)
    assert killed == kill_drill.KILL_STATUS
    assert kill_drill.ticks(tmp_path) == []

    landed = [
        entry
        for entry in kill_drill.read_ledger(tmp_path)
        if entry.get("event") == kill_drill.RECEIPT_COMPLETED
    ]
    assert len(landed) == 1, "the drill did not reach its own kill point"

    rows = receipt_rows(tmp_path)
    assert len(rows) == 1
    envelope = json.loads(str(rows[0]["result_json"]))
    assert envelope["state"] == "succeeded"

    resumed_status, stderr = run(tmp_path, kill_drill.KILL_NONE)
    assert resumed_status == 0, stderr

    resumed = kill_drill.ticks(tmp_path)[-1]
    assert resumed["status"] == "completed"
    assert resumed["resumed"] is True
    assert len(kill_drill.effects(tmp_path)) == 1, "the resumed tick re-sent a completed effect"


def test_the_ground_truth_ledger_is_neither_sqlite_nor_the_checkpoint(tmp_path: Path) -> None:
    """The oracle's independence, asserted rather than assumed.

    If the evidence lived in the store under test, a defect that lost both would
    render as a loop that behaved perfectly. This checks the separation is real:
    the ledger is its own file, it survives a kill, and the effect it records is
    present before any receipt row was completed.
    """
    status, _ = run(tmp_path, kill_drill.KILL_INSIDE_TOOL)
    assert status == kill_drill.KILL_STATUS

    ledger = kill_drill.ledger_path(tmp_path)
    assert ledger.is_file()
    assert ledger.name != kill_drill.DB_NAME
    assert ledger.read_text(encoding="utf-8").strip(), "the fsync before the kill did not land"

    rows = receipt_rows(tmp_path)
    assert [row["result_json"] for row in rows] == [None]
    assert len(kill_drill.effects(tmp_path)) == 1
