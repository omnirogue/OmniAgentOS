"""`send_alert` NEVER raises, and never invents a second alert.

An alerter that can throw turns "a unit parked" into "the worker died", which is
strictly worse than a missed notification: by the time the alerter runs, the park
is already durable in the DB. Every failure mode therefore degrades to one
greppable stderr line.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from omniagentos.workqueue import alert as alert_module

PAYLOAD = {
    "unit_id": "wq_01J8",
    "terminal_reason": "storm-parked",
    "remedy": "land the exemption on main first, then re-gate",
    "gate": "unit-acceptance",
    "count": 5,
}


def test_missing_transport_falls_back_to_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A notify module with no push_alert: `from ... import push_alert` raises
    # ImportError, which is exactly the shape of "this box has no loop engine".
    monkeypatch.setitem(sys.modules, "pipeline.bridge.notify", type(sys)("notify_stub"))
    alert_module.send_alert(PAYLOAD)
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1, "the fallback must be exactly one line"
    parsed = json.loads(err[0])
    assert parsed["wq_alert"]["unit_id"] == "wq_01J8"
    assert "import-failed" in parsed["transport"]


def test_a_raising_transport_is_swallowed(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    module = type(sys)("pipeline.bridge.notify")

    def boom(*args: Any, **kw: Any) -> None:
        raise RuntimeError("ntfy is down")

    module.push_alert = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pipeline.bridge.notify", module)
    alert_module.send_alert(PAYLOAD)  # must not raise
    assert "push-failed:RuntimeError" in capsys.readouterr().err


def test_the_payload_reaches_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    module = type(sys)("pipeline.bridge.notify")
    module.push_alert = lambda *args, **kw: seen.append((args, kw))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pipeline.bridge.notify", module)
    alert_module.send_alert(PAYLOAD)
    (args, kw) = seen[0]
    assert "storm-parked" in args[1] and "wq_01J8" in args[1]
    assert "land the exemption" in args[2]
    assert kw["source"] == "workqueue"


def test_a_bad_payload_cannot_kill_the_worker(capsys) -> None:
    alert_module.send_alert("not a dict")  # type: ignore[arg-type]
    assert "payload-not-a-dict" in capsys.readouterr().err
