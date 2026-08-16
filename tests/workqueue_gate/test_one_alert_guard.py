"""SPEC §4.5 — exactly one alert per park, and §6 — alerts:parks must be 1:1.

The guard is a compare-and-swap in the STORE (``alerted_at``), performed inside
the transaction that decides an alert is owed. The rule this test defends is the
one that makes the guard work at all: **the store returns a payload exactly when
it won the CAS, and the caller sends an alert exactly when it received one.**
``>1`` means the guard leaks; ``<1`` means a park went unnoticed, which is worse.

Three parks race on one row. Exactly one payload may come back, whichever thread
wins — asserted against the REAL store, because the CAS being tested is a SQL
UPDATE guarded by ``alerted_at IS NULL`` and only the real transaction can prove
it holds under contention.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omniagentos.workqueue import alert as alert_module
from omniagentos.workqueue.store import WorkQueueStore

SUBMIT = {
    "idempotency_key": "one-alert-probe",
    "repo_url": "https://example.invalid/repo.git",
    "repo_slug": "repo",
    "base_sha": "0" * 40,
    "branch": "wq/one-alert",
    "owned_paths": ["demo/**"],
    "agent_profile": "script",
    "acceptance_cmd": "python3 -c 'raise SystemExit(1)'",
    "risk_class": "mechanical",
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WorkQueueStore]:
    queue = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    try:
        yield queue
    finally:
        queue.close()


def test_three_racing_parks_yield_exactly_one_alert(store: WorkQueueStore) -> None:
    unit_id, _ = store.enqueue(SUBMIT)
    payloads: list[dict[str, Any]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def park() -> None:
        barrier.wait()
        result = store.park(unit_id, "attempts-exhausted", "fix the named cause, then unpark")
        if result:
            with lock:
                payloads.append(result)

    threads = [threading.Thread(target=park) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(payloads) == 1, f"one-alert guard leaked: {len(payloads)} payloads"
    assert store.get_unit(unit_id)["state"] == "parked"


def test_the_caller_sends_exactly_the_payloads_it_receives(
    store: WorkQueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_module, "send_alert", sent.append)
    unit_id, _ = store.enqueue(SUBMIT)
    for _ in range(3):
        payload = store.park(unit_id, "attempts-exhausted", "remedy text")
        if payload:
            alert_module.send_alert(payload)
    assert len(sent) == 1, "alerts sent must equal parks announced (§6: the ratio must be 1:1)"
