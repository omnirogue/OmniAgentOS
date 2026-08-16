"""Task-shape router's decision log, modeled on dispatch/log.py.

This module provides the telemetry persistence layer for task-shape routing decisions.
It acts as a best-effort, best-performance logger that swallows its own exceptions,
as planning/routing must never fail on telemetry logging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso

LOG = logging.getLogger(__name__)


def record_decision(
    conn: sqlite3.Connection | None,
    *,
    brief: str,
    route: str,
    topology: str | None = None,
    worker_count: int | None = None,
    rationale: str = "",
    applied: bool = False,
    char: Any | None = None,  # a TaskCharacterization; read defensively
    confidence: float | None = None,
    task_class: Sequence[str] | None = None,
    board_task_id: str | None = None,
    run_id: str | None = None,
    config_version: str | None = None,
    tool_density: float | None = None,
    context_breadth: float | None = None,
    merge_cost: float | None = None,
    shared_state_coupling: float | None = None,
    latency_ms: float | None = None,
    cbm_parallel_candidates: int | None = None,
) -> str | None:
    """Append one ``task_shape_decisions`` row; return its id (``None`` on any failure).

    Best-effort by design: a missing connection, a missing table (migration not yet
    applied), or any write error is swallowed at debug level so that telemetry can
    never block planner flow.
    """
    if conn is None:
        return None

    try:
        row_id = new_id("tsd")
        created_at = utc_now_iso()
        brief_hash = hashlib.sha1((brief or "").encode("utf-8")).hexdigest()

        # Read task characterization features defensively
        feat_d = getattr(char, "D", None) if char is not None else None
        feat_i = getattr(char, "I", None) if char is not None else None
        feat_s = getattr(char, "S", None) if char is not None else None
        feat_u = getattr(char, "U", None) if char is not None else None
        feat_v = getattr(char, "V", None) if char is not None else None
        feat_g = getattr(char, "G", None) if char is not None else None
        feat_c = getattr(char, "C", None) if char is not None else None
        feat_m = getattr(char, "M", None) if char is not None else None
        feat_r = getattr(char, "R", None) if char is not None else None
        feat_k = getattr(char, "K", None) if char is not None else None
        feat_w = getattr(char, "W", None) if char is not None else None
        feat_p = getattr(char, "P", None) if char is not None else None

        # Resolve confidence and task_class with fallback to char
        final_confidence = confidence
        if final_confidence is None and char is not None:
            final_confidence = getattr(char, "confidence", None)

        final_task_class = task_class
        if final_task_class is None and char is not None:
            final_task_class = getattr(char, "task_class", None)

        serialized_task_class = None
        if final_task_class is not None:
            serialized_task_class = json.dumps(list(final_task_class))

        applied_int = 1 if applied else 0

        conn.execute(
            """
            INSERT INTO task_shape_decisions (
                id,
                created_at,
                board_task_id,
                run_id,
                brief_hash,
                config_version,
                feat_d,
                feat_i,
                feat_s,
                feat_u,
                feat_v,
                feat_g,
                feat_c,
                feat_m,
                feat_r,
                feat_k,
                feat_w,
                feat_p,
                confidence,
                task_class,
                tool_density,
                context_breadth,
                merge_cost,
                shared_state_coupling,
                route,
                topology,
                worker_count,
                rationale,
                applied,
                latency_ms,
                cbm_parallel_candidates
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?
            )
            """,
            (
                row_id,
                created_at,
                board_task_id,
                run_id,
                brief_hash,
                config_version,
                feat_d,
                feat_i,
                feat_s,
                feat_u,
                feat_v,
                feat_g,
                feat_c,
                feat_m,
                feat_r,
                feat_k,
                feat_w,
                feat_p,
                final_confidence,
                serialized_task_class,
                tool_density,
                context_breadth,
                merge_cost,
                shared_state_coupling,
                route,
                topology,
                worker_count,
                rationale,
                applied_int,
                latency_ms,
                cbm_parallel_candidates,
            ),
        )
        return row_id
    except Exception:  # noqa: BLE001 -- telemetry must never block planning.
        LOG.debug("allocation: record_decision failed (swallowed)", exc_info=True)
        return None


__all__ = ["record_decision"]
