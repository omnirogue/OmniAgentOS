"""Alert transport — SPEC-shared-queue §4.5.

This module is TRANSPORT ONLY. The one-alert guard is a compare-and-swap in the
STORE (``wq_refusals.alerted_at`` / ``wq_units.alerted_at``), written inside the
same transaction that decides an alert is owed; the store returns a payload
EXACTLY when that CAS was won, and whoever won it calls :func:`send_alert`. A
second park on the same key never produces a second payload, so it never
produces a second alert. Putting the guard here instead would make it a race
between processes on two machines.

``push_alert`` is IMPORTED, never copied (§4.5): it is not a 20-line function —
it depends on ``pipeline/bridge/notify.py``'s redaction chain, and its
fail-soft contract ("NEVER raises") is the entire reason an alerter can call it.
``pipeline/bridge/**`` is a live path; this module reads it and never modifies
it.

:func:`send_alert` NEVER raises. An alerter that can throw turns "a unit
parked" into "the worker died", which is strictly worse than a missed
notification — the park is already durable in the DB by the time we are called.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["send_alert"]


def _loops_root() -> Path:
    """Where ``push_alert`` appends its transport-failure note (ALERTS.md)."""
    return Path(os.environ.get("WQ_HOME") or Path.home() / "wq")


def _title(payload: dict[str, Any]) -> str:
    unit = payload.get("unit_id") or payload.get("input_key") or "?"
    reason = payload.get("terminal_reason") or payload.get("refusal_class") or "park"
    return f"wq {reason}: {unit}"


def _body(payload: dict[str, Any]) -> str:
    remedy = payload.get("remedy") or payload.get("park_remedy") or "(no remedy recorded)"
    gate = payload.get("gate")
    count = payload.get("count")
    parts = [remedy]
    if gate:
        parts.append(f"gate={gate}")
    if count is not None:
        parts.append(f"refusals={count}")
    return " · ".join(str(p) for p in parts)


def _stderr(payload: dict[str, Any], why: str) -> None:
    """The fallback channel: ONE line, machine-greppable, on stderr.

    A worker's stderr is captured into its launchd/systemd log, so an alert that
    could not be pushed is still recoverable evidence rather than silence
    (favourable absence: an unnoticed park is the worst reading of all).
    """
    try:
        line = json.dumps({"wq_alert": payload, "transport": why}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        line = f'{{"wq_alert": "unserialisable", "transport": "{why}"}}'
    print(line, file=sys.stderr, flush=True)


def send_alert(payload: dict[str, Any]) -> None:
    """Send one park alert. Never raises, whatever happens downstream."""
    if not isinstance(payload, dict):  # defensive: a caller bug must not kill the worker
        _stderr({"raw": repr(payload)}, "payload-not-a-dict")
        return
    try:
        from pipeline.bridge.notify import push_alert
    except ImportError as exc:
        _stderr(payload, f"import-failed:{exc}")
        return
    try:
        push_alert(
            _loops_root(),
            _title(payload),
            _body(payload),
            source="workqueue",
            tags="warning",
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft is the contract
        _stderr(payload, f"push-failed:{type(exc).__name__}:{exc}")
