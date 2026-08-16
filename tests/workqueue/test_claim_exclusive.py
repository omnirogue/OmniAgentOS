"""The exclusivity guarantee, under real concurrency (SPEC §7 Phase 1, item 5).

8 threads x 200 claim attempts against 400 units in a temp DB. Each thread opens
its OWN store, i.e. its own SQLite connection: an in-process lock would prove
nothing about two machines racing, and ``BEGIN IMMEDIATE`` + ``busy_timeout`` is
what actually has to hold.
"""

from __future__ import annotations

import threading

import pytest

from omniagentos.workqueue.schema import LEASE_TTL_S
from omniagentos.workqueue.store import WorkQueueStore
from tests.workqueue.conftest import at, submit

THREADS = 8
CLAIMS_PER_THREAD = 200
UNITS = 400


@pytest.mark.slow
def test_every_unit_is_claimed_exactly_once(db_path):
    seeder = WorkQueueStore(db_path)
    for index in range(UNITS):
        seeder.enqueue(submit(f"unit-{index:04d}"))
    seeder.close()

    claimed: list[tuple[str, str, int]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(THREADS)

    def worker(slot: int) -> None:
        store = WorkQueueStore(db_path)
        mine: list[tuple[str, str, int]] = []
        try:
            start.wait(timeout=30)
            for _ in range(CLAIMS_PER_THREAD):
                got = store.claim(f"machine-{slot}", f"w{slot}", [])
                if got is None:
                    continue
                mine.append((got["unit"]["id"], f"machine-{slot}", int(got["lease_generation"])))
        except BaseException as error:  # pragma: no cover - surfaced by the assert
            with lock:
                errors.append(error)
        finally:
            with lock:
                claimed.extend(mine)
            store.close()

    threads = [threading.Thread(target=worker, args=(slot,)) for slot in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)

    unit_ids = [unit_id for unit_id, _, _ in claimed]
    assert len(unit_ids) == UNITS, "every unit must be claimed"
    assert len(set(unit_ids)) == UNITS, "no unit may be claimed twice while its lease is live"

    # A live lease is never handed out again, so every unit is on generation 1.
    assert {generation for _, _, generation in claimed} == {1}

    audit = WorkQueueStore(db_path)
    try:
        rows = audit._connection.execute(
            "SELECT unit_id, COUNT(*) AS n FROM wq_attempts GROUP BY unit_id HAVING n > 1"
        ).fetchall()
        assert rows == [], "UNIQUE(unit_id, attempt) must keep one attempt row per execution"
        assert audit.status()["double_executions"] == 0
    finally:
        audit.close()


def test_lease_generation_is_strictly_increasing_per_unit(store):
    unit_id, _ = store.enqueue(submit("gen"))
    generations = []
    for cycle in range(3):
        now = at(cycle * (LEASE_TTL_S + 1))
        claimed = store.claim("mac-studio", f"w{cycle}", [], now=now)
        assert claimed is not None
        generations.append(claimed["lease_generation"])
    assert generations == [1, 2, 3]
    assert store.get_unit(unit_id)["lease_generation"] == 3
