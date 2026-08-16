"""Shared strict allow-list projector for ``approval.recorded`` event payloads.

Any route surface that echoes back an ``approval.recorded`` event payload
(engine's approval receipt, swarm's activity feed, ...) MUST route the raw
payload through :func:`project_receipt_payload` rather than copying it
verbatim. The event payload is caller-supplied data at write time (F001):
a hostile ``approval.recorded`` write can smuggle nested credential-shaped
fields under any key, including allow-listed ones, so this module is the
single place that decides what is safe to return.
"""

from __future__ import annotations

from typing import Any

#: The event ``action`` value this projector applies to.
APPROVAL_ACTION = "approval.recorded"

#: Strict allow-list of receipt fields. Every other key on the payload is
#: dropped, and even an allow-listed key is dropped unless its value is a
#: plain string (a dict or list smuggled under ``reviewer``/``recorded_at``/
#: etc. must never cross the API boundary under an allowed name).
RECEIPT_FIELDS = ("reviewer", "run_id", "head_sha", "recorded_at")


def project_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strict, type-checked allow-list for an approval receipt.

    Every allow-listed field projects ONLY when its value is a plain string.
    A dict or list smuggled under an allow-listed key is dropped rather than
    copied, so nested private fields can never cross the API boundary inside
    an allowed name (F001).
    """
    projected: dict[str, Any] = {}
    for key in RECEIPT_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            projected[key] = value
    return projected
