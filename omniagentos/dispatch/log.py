"""Best-effort telemetry for FAST DISPATCH decisions.

Every :func:`omniagentos.dispatch.gate.decide` call at intake appends one row to
``dispatch_decisions`` (migration 054) -- whether or not the gate changed routing
(``applied``). This is the future fine-tune corpus for the classifier.

:func:`record_decision` is best-effort by contract: it swallows every error (debug
log only). Telemetry must NEVER block or break a dispatch -- exactly the posture of
the reliability/notification writers elsewhere in the tree.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import TYPE_CHECKING

from omniagentos.contracts import new_id, utc_now_iso

if TYPE_CHECKING:
    from omniagentos.dispatch.gate import GateDecision

LOG = logging.getLogger(__name__)

_BRIEF_HEAD_CHARS = 160


def record_decision(
    conn: sqlite3.Connection | None,
    decision: GateDecision,
    *,
    brief: str,
    applied: bool = False,
    dispatch_kind: str | None = None,
    ref_id: str | None = None,
) -> str | None:
    """Append one ``dispatch_decisions`` row; return its id (``None`` on any failure).

    ``applied`` is ``True`` only when the gate ACTUALLY changed routing (the caller
    knows this, not the gate). Best-effort: a missing connection, a missing table
    (migration not yet applied), or any write error is swallowed at debug level.
    """
    if conn is None:
        return None
    try:
        brief_text = brief or ""
        row_id = new_id("dgd")
        conn.execute(
            """
            INSERT INTO dispatch_decisions (
                id, created_at, brief_hash, brief_head, decision, gate,
                risk_flagged, confidence, latency_ms, applied, dispatch_kind, ref_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                utc_now_iso(),
                hashlib.sha1(brief_text.encode("utf-8")).hexdigest(),
                brief_text[:_BRIEF_HEAD_CHARS],
                decision.decision,
                decision.gate,
                1 if decision.risk_flagged else 0,
                float(decision.confidence),
                float(decision.latency_ms),
                1 if applied else 0,
                dispatch_kind,
                ref_id,
            ),
        )
        return row_id
    except Exception:  # noqa: BLE001 -- telemetry must never block dispatch.
        LOG.debug("dispatch: record_decision failed (swallowed)", exc_info=True)
        return None


__all__ = ["record_decision"]
