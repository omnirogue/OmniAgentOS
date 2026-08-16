"""`wq status` — the SPEC §6 layout, plus the 2026-08-11 capacity columns.

The numbers on this page were each chosen because their absence hid a real
failure: oldest-unclaimed (work sitting while capacity idles is the problem this
queue exists to fix), unclaimable (a queue that looks idle because nothing can
run must never read as healthy), double-executions (must be 0), alerts-vs-parks
(must be 1:1). If a renderer drops one of them the operator stops seeing it, so
they are asserted here by name.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omniagentos.workqueue.cli import main, render_machines, render_status

PAYLOAD: dict[str, Any] = {
    "machines": [
        {
            "machine_id": "mac-studio",
            "os": "darwin",
            "in_flight": 2,
            "max_concurrent": 3,
            "ncpu": 24,
            "perf_cores": 16,
            "mem_gb": 64,
            "last_load1": 4.1,
            "ceiling_fraction": 0.75,
            "done_1h": 7,
            "done_6h": 41,
            "attempts_per_completion": 1.2,
            "last_seen_s": 3,
            "workers": 3,
            "labels": ["build", "gate"],
            "drain": 0,
        },
        {
            "machine_id": "initech-roi-calculator",
            "os": "linux",
            "in_flight": 1,
            "max_concurrent": 2,
            "ncpu": 16,
            "perf_cores": 16,
            "mem_gb": 125,
            "last_load1": 12.3,
            "ceiling_fraction": 0.6,
            "done_1h": 3,
            "done_6h": 14,
            "attempts_per_completion": 1.4,
            "last_seen_s": 8,
            "workers": 2,
            "labels": ["build", "linux"],
            "drain": 1,
        },
    ],
    "depth": {"queued": 34, "claimed": 2, "running": 3, "review": 6, "done": 12, "parked": 4},
    "oldest_unclaimed_s": 664,
    "unclaimable": [{"id": "wq_x", "labels": ["browser"]}],
    "refusals_24h": {
        "candidate-defect": 12,
        "instrument-error": 3,
        "environment": 2,
        "unchanged-retry": 9,
        "storm-parked": 1,
        "instrument_share": 0.22,
    },
    "lease_reclaims_1h": 1,
    "double_executions": 0,
    "alerts_sent": 1,
    "parks": 1,
    "capacity": {
        "total_cores": 40,
        "total_perf_cores": 32,
        "total_slots": 5,
        "free_slots": 2,
        "in_flight": 3,
    },
    "parked": [
        {
            "id": "wq_01J8",
            "terminal_reason": "storm-parked",
            "gate": "unit-acceptance",
            "park_remedy": "land devtasks/REACHABILITY-EXEMPT.txt on main first, then re-gate",
        }
    ],
}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render_status(PAYLOAD)


def test_pool_and_capacity_headline(rendered: str) -> None:
    assert "POOL  2 machines · 5 workers · 3 in flight · capacity 5" in rendered
    # the operator 2026-08-11: every box's cores and load are visible pool-wide.
    assert "CAPACITY  40 cores (32 perf) · slots 5 (2 free) · pool load1 16.40" in rendered


def test_depth_and_oldest_unclaimed(rendered: str) -> None:
    assert "queued 34" in rendered and "parked 4" in rendered
    assert "OLDEST UNCLAIMED   11m 04s" in rendered
    assert "alert threshold: 15m with idle capacity" in rendered


def test_unclaimable_is_never_silent(rendered: str) -> None:
    assert "UNCLAIMABLE        1" in rendered
    assert "browser" in rendered


def test_per_machine_columns_include_cores_and_load(rendered: str) -> None:
    header = next(line for line in rendered.splitlines() if line.startswith("MACHINE"))
    for column in ("IN FLIGHT", "CAP", "CORES", "LOAD", "DONE/1h", "DONE/6h", "ATT/COMPLETE"):
        assert column in header
    row = next(line for line in rendered.splitlines() if line.startswith("mac-studio"))
    assert "2/3" in row and "24(16P)" in row and "4.1" in row and "1.2" in row
    assert "initech-roi-calculator*" in rendered  # * marks draining
    assert "draining" in rendered


def test_refusal_and_integrity_lines(rendered: str) -> None:
    assert "REFUSALS 24h" in rendered
    assert "unchanged-retry 9" in rendered
    assert "INSTRUMENT SHARE 22%" in rendered and "baseline 71%" in rendered
    assert "DOUBLE EXECUTIONS  0" in rendered
    assert "ALERTS SENT  1 (1 parks)" in rendered
    assert "storm-parked" in rendered and "REACHABILITY-EXEMPT" in rendered


def test_render_survives_a_sparse_payload() -> None:
    """A missing telemetry column must never blind the operator to the whole pool."""
    text = render_status({"machines": [{"machine_id": "new-box"}], "depth": {}})
    assert "new-box" in text
    assert "POOL  1 machines" in text
    assert render_machines([{"machine_id": "new-box", "labels": "[]"}])


def test_json_mode_prints_exactly_the_status_payload(capsys, monkeypatch) -> None:
    import omniagentos.workqueue.cli as cli

    class Q:
        def status(self) -> dict[str, Any]:
            return PAYLOAD

    monkeypatch.setattr(cli, "open_queue", lambda server, db: Q())
    assert main(["--db", "/tmp/ignored.sqlite3", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == PAYLOAD
