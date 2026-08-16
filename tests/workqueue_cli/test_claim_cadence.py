"""Claim cadence scales with headroom (ROUTING-DECISIONS §3).

"Route to the machine with the MOST available compute." The mechanism is
statistical, not a placement decision: every machine still PULLS its own work
under ``BEGIN IMMEDIATE``, but an idle box re-asks 12× more often than a nearly
saturated one, so over any window the idle box wins most of the claims. What
this file pins is the property that makes that true — the delay is monotonic
non-decreasing in load, floored at 5 s and capped at 60 s.

The load average is faked rather than measured: a test that read the real
``getloadavg()`` would assert on the machine that happens to be running it.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.workqueue import worker as worker_module
from omniagentos.workqueue.worker import (
    IDLE_POLL_MAX_S,
    IDLE_POLL_MIN_S,
    Worker,
    idle_poll_delay_s,
)

CONFIG: dict[str, Any] = {"capacity": {"ceiling_fraction_default": 0.5}, "lease": {}}


class _Queue:
    """The three calls Worker.machine() makes, and nothing else."""

    def list_machines(self) -> list[dict[str, Any]]:
        return [
            {
                "machine_id": "mw0002",
                "ncpu": 10,
                "perf_cores": 10,
                "ceiling_fraction": 0.5,  # ceiling = 0.5 * 10 = 5.0 → ratio = load1 / 5
                "max_concurrent": 3,
                "labels": ["pytest"],
            }
        ]


@pytest.fixture
def worker(tmp_path) -> Worker:
    return Worker(_Queue(), "mw0002", config=CONFIG, home=tmp_path / "wq")


def _delay_at(worker: Worker, load: float, monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(worker_module, "load1", lambda: load)
    return worker.idle_delay()


def test_an_idle_box_polls_at_the_floor(worker: Worker, monkeypatch) -> None:
    # load1 0.5 on a ceiling of 5.0 → ratio 0.1.
    assert _delay_at(worker, 0.5, monkeypatch) == pytest.approx(IDLE_POLL_MIN_S)
    assert IDLE_POLL_MIN_S == 5.0


def test_a_nearly_saturated_box_backs_off_toward_the_cap(worker: Worker, monkeypatch) -> None:
    # load1 4.5 → ratio 0.9, most of the way to the ceiling.
    delay = _delay_at(worker, 4.5, monkeypatch)
    assert delay > 45.0, "a busy box must wait long enough for an idle one to win"
    assert delay < IDLE_POLL_MAX_S
    # At and above the ceiling the worker stops claiming entirely; the cadence
    # is pinned at the cap so a machine that recovers is not left waiting longer.
    assert _delay_at(worker, 5.0, monkeypatch) == pytest.approx(IDLE_POLL_MAX_S)
    assert _delay_at(worker, 40.0, monkeypatch) == pytest.approx(IDLE_POLL_MAX_S)


def test_the_delay_never_decreases_as_load_rises(worker: Worker, monkeypatch) -> None:
    delays = [_delay_at(worker, load / 4, monkeypatch) for load in range(0, 41)]
    assert delays == sorted(delays), f"cadence must be monotonic in load: {delays}"
    assert delays[0] == IDLE_POLL_MIN_S and delays[-1] == IDLE_POLL_MAX_S


def test_the_ratio_is_the_same_number_the_capacity_gate_uses(worker: Worker, monkeypatch) -> None:
    """One number, two consumers — they must not drift apart."""
    monkeypatch.setattr(worker_module, "load1", lambda: 4.9)
    assert worker.load_ratio() == pytest.approx(0.98)
    assert worker.capacity_ok() == (True, 4.9)

    monkeypatch.setattr(worker_module, "load1", lambda: 5.1)
    assert worker.load_ratio() == pytest.approx(1.02)
    ok, current = worker.capacity_ok()
    assert ok is False and current == 5.1


def test_unmeasurable_load_polls_fast_rather_than_slow(worker: Worker, monkeypatch) -> None:
    """Telemetry that could not be read must not throttle a box that may be free."""
    monkeypatch.setattr(worker_module, "load1", lambda: None)
    assert worker.load_ratio() is None
    assert worker.idle_delay() == pytest.approx(IDLE_POLL_MIN_S)
    assert idle_poll_delay_s(None) == IDLE_POLL_MIN_S
