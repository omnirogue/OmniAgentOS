"""FH-001 — graph_runtime.complete_node status read outside the write txn.

``GraphRuntimeService.complete_node`` reads the node status via
``store.get_run`` BEFORE opening the ``graph_complete_node`` transaction.
Two racers that both observe ``status='ready'`` for the same node then both
enter the (RLock-serialized) write transaction and each inserts its own
artifact row and re-marks the node completed — the M-39 idempotency and
fail-closed gates are bypassed by the stale read.

The interleaving is forced deterministically: ``store.get_run`` is
monkeypatched (the "or equivalent" of patching update_node — it is the exact
call whose result is the stale status) so each racing thread parks on a
2-party ``threading.Barrier`` immediately after its FIRST status read.  Both
threads are therefore guaranteed to have read ``ready`` before either one
writes.

EXPECTED RED today (docs/testing/KNOWN-ISSUES.yaml FH-001): the duplicate
artifact assertion fails with 2 rows for (fan_a, finding).  Fix shape is a
status CAS inside the write txn (WHERE status IN ('ready','running') +
rowcount check); once that lands this test goes green and fh.py issues flags
it for promotion.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omniagentos.graph_runtime.service import GraphRuntimeService


@pytest.mark.fh_known_issue(id="FH-001")
def test_racing_complete_node_single_winner_single_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GraphRuntimeService(db_path=str(tmp_path / "graph.db"))
    run = service.start_diamond(title="FH-001 race")
    run_id = run["id"]

    barrier = threading.Barrier(2, timeout=30)
    local = threading.local()
    original_get_run = service.store.get_run

    def racing_get_run(target_run_id: str) -> Any:
        result = original_get_run(target_run_id)
        # Park each racing thread exactly once: right after its FIRST status
        # read (the read complete_node's ready/idempotency gates rely on).
        if getattr(local, "racing", False) and not getattr(local, "synced", False):
            local.synced = True
            barrier.wait()
        return result

    monkeypatch.setattr(service.store, "get_run", racing_get_run)

    results: dict[str, Any] = {}

    def racer(label: str, payload: dict[str, Any]) -> None:
        local.racing = True
        try:
            service.complete_node(run_id, "fan_a", outputs={"finding": payload})
            results[label] = "completed"
        except Exception as exc:  # noqa: BLE001 — losing racer's rejection is recorded
            results[label] = exc

    t1 = threading.Thread(
        target=racer, args=("a", {"claim": "payload-A", "score": 0.8, "source": "racer-a"})
    )
    t2 = threading.Thread(
        target=racer, args=("b", {"claim": "payload-B", "score": 0.9, "source": "racer-b"})
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), "racing threads deadlocked"

    monkeypatch.setattr(service.store, "get_run", original_get_run)
    final = service.store.get_run(run_id)
    assert final is not None

    fan_a_artifacts = [
        a
        for a in final["artifacts"]
        if a["node_key"] == "fan_a" and a["port"] == "finding"
    ]
    hashes = sorted({a["content_hash"] for a in fan_a_artifacts})

    # THE defect: both racers insert an artifact for the same (node, port) with
    # divergent content — downstream consumers cannot tell which one is real.
    assert len(fan_a_artifacts) == 1, (
        "FH-001: duplicate graph_artifacts rows for (fan_a, finding) — "
        f"got {len(fan_a_artifacts)} rows with content hashes {hashes}; "
        "complete_node read the node status outside the write txn so both "
        "racers passed the ready gate and both committed"
    )

    # Exactly one completion may win; the loser must be rejected (or absorbed
    # idempotently WITHOUT writing a second artifact).
    winners = [label for label, outcome in results.items() if outcome == "completed"]
    assert len(winners) == 1, (
        f"FH-001: both racers completed the same ready node (results={results})"
    )
