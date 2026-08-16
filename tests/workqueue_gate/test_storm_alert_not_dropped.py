"""A refusal storm is announced exactly once, and never zero times.

Two guards fire for one storm: ``wq_refusals.alerted_at`` (won inside
``refusal_record`` the moment the count reaches the cap) and ``wq_units.alerted_at``
(won inside ``record_result`` when the unit storm-parks a round later). The
shipped worker discarded ``refusal_record``'s return value entirely and relied on
the unit park to carry the news — which works right up until the unit does not
park:

* the unit is ``cancel``-ed while the 5th attempt is in flight, so
  ``record_result`` routes to CANCELLED — no park, no alert;
* or the lease is lost between the ledger write and the result, so
  ``record_result`` raises and the worker writes nothing at all.

In both cases the refusal CAS has already been spent, so nobody else will ever
raise that alert: the storm goes completely unannounced while the ledger sits at
count=5 refusing the input forever. That is favourable absence — the pool looks
quiet precisely because something is wrong.

So: the worker sends what ``refusal_record`` hands it, immediately, and
``record_result`` suppresses the second notification for the same input_key. Net
contract, proven below and in ``test_storm_park_via_resubmits.py`` (the six-submit
end-to-end, which is case (a) of the same rule):

  storm → park              : exactly ONE alert
  storm → cancelled instead : exactly ONE alert  (the same one)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omniagentos.workqueue.alert as alert_module
from omniagentos.workqueue.schema import REFUSAL_STORM_CAP
from omniagentos.workqueue.store import WorkQueueStore
from omniagentos.workqueue.worker import UnitResult, Worker
from tests.workqueue.conftest import at, submit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((REPO_ROOT / "configs" / "workqueue.yaml").read_text())
MACHINE = "test-machine"
KEY = "storm-key"
GATE = "raw"


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    worker = Worker(store, MACHINE, config=CONFIG, home=tmp_path / "wq")
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_module, "send_alert", sent.append)
    return {"store": store, "worker": worker, "sent": sent}


def _refusal(key: str = KEY) -> UnitResult:
    return UnitResult(
        outcome="candidate-defect",
        exit_code=1,
        input_key=key,
        retryable=0,
        remedy="the test it added fails",
    )


def _fill_ledger_to(store: WorkQueueStore, count: int, key: str = KEY) -> None:
    """Bring the refusal row to ``count`` WITHOUT reaching the cap."""
    assert count < REFUSAL_STORM_CAP
    for _ in range(count):
        store.refusal_record(key, GATE, "candidate-defect", 0, "the test it added fails")
    assert int(store.refusal_check(key, GATE)["count"]) == count


def test_the_storm_alert_survives_a_cancel_that_erases_the_park(
    bench: dict[str, Any],
) -> None:
    """(b) — cancelled between the 5th refusal and the park: still ONE alert."""
    store: WorkQueueStore = bench["store"]
    worker: Worker = bench["worker"]
    sent: list[dict[str, Any]] = bench["sent"]

    unit_id, _ = store.enqueue(submit("storm-cancelled"))
    claimed = store.claim(MACHINE, worker.worker_id, [], now=at(0))
    assert claimed is not None
    _fill_ledger_to(store, REFUSAL_STORM_CAP - 1)

    # The human cancels while the attempt is in flight. The lease is NOT broken
    # (§3.4), so the worker still reports — into a unit that is on its way out.
    store.cancel(unit_id)
    worker._record(store.get_unit(unit_id), claimed["lease_generation"], GATE, _refusal())

    assert int(store.refusal_check(KEY, GATE)["count"]) == REFUSAL_STORM_CAP
    unit = store.get_unit(unit_id)
    assert (unit["state"], unit["terminal_reason"]) == ("cancelled", "cancelled"), (
        "the cancel must still win — this test is about the alert, not about "
        "keeping cancelled work alive"
    )
    assert unit["alerted_at"] is None, "no park happened, so the unit's guard never fired"

    assert len(sent) == 1, f"the storm must be announced exactly once; got {sent}"
    assert sent[0]["kind"] == "refusal-storm"
    assert sent[0]["input_key"] == KEY
    assert sent[0]["count"] == REFUSAL_STORM_CAP
    assert sent[0]["remedy"] == "the test it added fails", "the ORIGINAL remedy, not 'you asked'"
    # ...and `wq alerts` agrees with what was actually sent, which is the ratio
    # §6 reads to detect a park nobody was told about.
    assert [row["kind"] for row in store.alerts()] == ["refusal-storm"]


def test_the_storm_alert_survives_a_lost_lease(bench: dict[str, Any]) -> None:
    """The other way the park never arrives: the result is fenced out.

    The refusal CAS is spent before record_result is even called, so a worker
    that alerted only on the park would drop this one too.
    """
    store: WorkQueueStore = bench["store"]
    worker: Worker = bench["worker"]
    sent: list[dict[str, Any]] = bench["sent"]

    unit_id, _ = store.enqueue(submit("storm-fenced"))
    claimed = store.claim(MACHINE, worker.worker_id, [], now=at(0))
    _fill_ledger_to(store, REFUSAL_STORM_CAP - 1)

    stale = int(claimed["lease_generation"]) - 1  # adopted: this worker was fenced out
    worker._record(store.get_unit(unit_id), stale, GATE, _refusal())

    assert store.get_unit(unit_id)["state"] == "claimed", "the zombie wrote no result"
    assert len(sent) == 1 and sent[0]["kind"] == "refusal-storm", sent


def test_the_park_does_not_re_announce_a_storm_already_sent(bench: dict[str, Any]) -> None:
    """(a) in miniature — the storm and the park it causes are ONE event.

    The six-submit end-to-end in test_storm_park_via_resubmits.py drives the real
    gate through the same rule; this pins the mechanism directly, including the
    second half no end-to-end reaches: the SAME unit parking again later still
    never doubles the alert.
    """
    store: WorkQueueStore = bench["store"]
    worker: Worker = bench["worker"]
    sent: list[dict[str, Any]] = bench["sent"]

    unit_id, _ = store.enqueue(submit("storm-parked-once"))
    _fill_ledger_to(store, REFUSAL_STORM_CAP - 1)

    # Round N: the cap is reached. The unit soft-parks (unchanged-retry), so the
    # only thing that can announce this is the ledger CAS.
    claimed = store.claim(MACHINE, worker.worker_id, [], now=at(0))
    worker._record(
        store.get_unit(unit_id),
        claimed["lease_generation"],
        GATE,
        UnitResult(outcome="unchanged-retry", input_key=KEY, retryable=0, remedy="nothing changed"),
    )
    assert len(sent) == 1 and sent[0]["kind"] == "refusal-storm"
    assert store.get_unit(unit_id)["terminal_reason"] is None, "soft park"

    # Round N+1: the unit reaches its terminal storm park. Same input, same
    # event, no second alert — but it IS terminal, and `wq alerts` still shows 1.
    store.requeue(unit_id)
    claimed = store.claim(MACHINE, worker.worker_id, [], now=at(60))
    worker._record(
        store.get_unit(unit_id),
        claimed["lease_generation"],
        GATE,
        UnitResult(outcome="storm-parked", input_key=KEY, retryable=0, remedy="5 refusals"),
    )

    unit = store.get_unit(unit_id)
    assert (unit["state"], unit["terminal_reason"]) == ("parked", "storm-parked")
    assert len(sent) == 1, f"one event, one alert; got {sent}"
    status = store.status()
    assert status["parks"] == status["alerts_sent"] == 1, (
        "§6 reads alerts_sent == parks 1:1 to detect a park nobody was told about"
    )

    # A park for an UNRELATED input still alerts: the suppression is keyed on the
    # storm that was announced, not on 'this unit has been noisy'.
    other_id, _ = store.enqueue(submit("unrelated-park"))
    claimed = store.claim(MACHINE, worker.worker_id, [], now=at(120))
    worker._record(
        store.get_unit(other_id),
        claimed["lease_generation"],
        GATE,
        UnitResult(outcome="storm-parked", input_key="a-different-key", retryable=0, remedy="x"),
    )
    assert [row["kind"] for row in sent] == ["refusal-storm", "unit-park"], sent


def test_a_worker_over_http_does_not_double_send_what_the_server_sent(
    bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the holder of the transport announces.

    ``HttpQueueClient`` hands back the alert payload so the CALLER can log it —
    the server already sent it on the way through (its result and refusal routes
    both do). A worker that sent it again would report two alerts for one park
    and break the ratio in the opposite direction.
    """
    store: WorkQueueStore = bench["store"]
    sent: list[dict[str, Any]] = bench["sent"]

    class _FakeHttpQueue:
        base_url = "http://127.0.0.1:8487"  # what makes it remote

        def __init__(self, inner: WorkQueueStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    remote_worker = Worker(_FakeHttpQueue(store), MACHINE, config=CONFIG, home=Path("/tmp/wq-none"))
    assert remote_worker.owns_alert_transport is False

    unit_id, _ = store.enqueue(submit("http-storm"))
    claimed = store.claim(MACHINE, remote_worker.worker_id, [], now=at(0))
    _fill_ledger_to(store, REFUSAL_STORM_CAP - 1)
    remote_worker._record(store.get_unit(unit_id), claimed["lease_generation"], GATE, _refusal())

    assert sent == [], "the server owns the wire; the worker must not speak twice"
    assert [row["kind"] for row in store.alerts()] == ["refusal-storm"], (
        "the alert is still RECORDED — it is the transport that differs, not the CAS"
    )
