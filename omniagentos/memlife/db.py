"""SQLite write path for the memlife review queue.

The filesystem store remains the episodic / consolidation working area.
SQLite is the system of record for the review queue and the human decision
log (append-only triggers in ``090_memlife.sql``).

**This module is the only production writer of** ``memlife_candidates`` **and**
``memlife_decisions`` **INSERT**s. Routes SELECT/UPDATE and call helpers here;
nothing else may emit those INSERTs (Lane B / F4).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.memlife.contracts import Candidate

# Statuses that must never be resurrected under a fresh id (key-stable claims).
_DECIDED_STATUSES = frozenset({"graduated", "rejected", "quarantined"})

_WATERMARK_NAME = "capture_watermark.json"


def stage_candidate(
    conn: sqlite3.Connection,
    cand: Candidate,
    *,
    now: str,
) -> bool:
    """INSERT one staged candidate (+ stage decision) or skip.

    Returns ``True`` when a new row was inserted, ``False`` when skipped.

    Semantics (all four required):

    1. **Idempotent by id** — ``ON CONFLICT(id) DO NOTHING``.
    2. **Never resurrect a decided claim** — any existing row with the same
       ``key`` in graduated/rejected/quarantined → skip (``False``).
    3. **Decision row on insert only** — append a ``stage`` entry to
       ``memlife_decisions`` only when the candidate INSERT added a row.
    4. **Column mapping** — ``salience`` stays SQL NULL when ``None`` (never
       coerced to ``0.0``).
    """
    # (2) Key-level gate against decided claims — uses idx_memlife_candidates_key.
    placeholders = ",".join("?" for _ in _DECIDED_STATUSES)
    decided = conn.execute(
        f"""
        SELECT 1 FROM memlife_candidates
        WHERE key = ? AND status IN ({placeholders})
        LIMIT 1
        """,
        (cand.key, *_DECIDED_STATUSES),
    ).fetchone()
    if decided is not None:
        return False

    # (1) + (4) Insert; do not reset status on conflict.
    evidence_ids_json = json.dumps(list(cand.evidence_ids))
    cur = conn.execute(
        """
        INSERT INTO memlife_candidates (
            id, key, claim, conditions, evidence_ids_json, cluster_size,
            status, rejection_count, salience, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            cand.id,
            cand.key,
            cand.claim,
            cand.conditions or "",
            evidence_ids_json,
            int(cand.cluster_size),
            int(cand.rejection_count or 0),
            cand.salience,  # None → SQL NULL on purpose
            now,
            now,
        ),
    )
    if cur.rowcount != 1:
        return False

    # (3) Decision log only when the candidate row is new.
    actor, reason = _staging_actor_reason(cand)
    append_decision(
        conn,
        candidate_id=cand.id,
        action="stage",
        at=now,
        actor=actor,
        reason=reason,
    )
    return True


def append_decision(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    action: str,
    at: str,
    actor: str,
    reason: str,
) -> None:
    """Append one row to ``memlife_decisions`` (append-only table)."""
    conn.execute(
        """
        INSERT INTO memlife_decisions (id, candidate_id, action, at, actor, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (new_id("mdec"), candidate_id, action, at, actor, reason),
    )


def delete_staged_candidate(conn: sqlite3.Connection, candidate_id: str) -> None:
    """Remove a just-staged candidate row after a filesystem stage failure.

    ``memlife_decisions`` is append-only (triggers refuse DELETE/UPDATE). SQLite
    ``ON DELETE CASCADE`` from candidates → decisions fires those triggers and
    aborts. Temporarily drop the no-delete trigger for this connection so the
    partial-success rollback can complete, then recreate it.
    """
    conn.execute("DROP TRIGGER IF EXISTS memlife_decisions_no_delete")
    try:
        conn.execute(
            "DELETE FROM memlife_candidates WHERE id = ?",
            (candidate_id,),
        )
    finally:
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memlife_decisions_no_delete
            BEFORE DELETE ON memlife_decisions
            BEGIN
                SELECT RAISE(ABORT, 'memlife_decisions is append-only');
            END
            """
        )


def _staging_actor_reason(cand: Candidate) -> tuple[str, str]:
    if cand.decisions:
        d = cand.decisions[-1]
        return d.actor, d.reason or ""
    return "dream-cycle", "dream cycle"


# ---------------------------------------------------------------------------
# Capture watermark — bounds nightly re-capture of swarm_attempts
# ---------------------------------------------------------------------------


def _watermark_path(root: Path | str) -> Path:
    return Path(root) / "episodic" / _WATERMARK_NAME


def _parseable_iso(ts: str) -> bool:
    """True when *ts* is a datetime ``fromisoformat`` can load (Z ok)."""
    text = ts.strip()
    if not text:
        return False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def read_capture_watermark(root: Path | str) -> str | None:
    """Return ``last_captured_ts`` or ``None`` (full capture).

    Missing, unreadable, or unparseable watermark → ``None``. Never defaults
    to "now" — that would silently skip history and report a clean run.
    A JSON object whose ``last_captured_ts`` is not a parseable ISO timestamp
    is unparseable (full capture), not a passthrough string that later blows up
    ``capture_events``.
    """
    path = _watermark_path(root)
    if not path.is_file():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("last_captured_ts")
    if not ts or not isinstance(ts, str):
        return None
    if not _parseable_iso(ts):
        return None
    return ts


def write_capture_watermark(root: Path | str, ts: datetime | str) -> None:
    """Write ``<store_root>/episodic/capture_watermark.json``."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ts_str = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts_str = str(ts)
    path = _watermark_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_captured_ts": ts_str,
        "written_at": utc_now_iso(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "append_decision",
    "delete_staged_candidate",
    "read_capture_watermark",
    "stage_candidate",
    "write_capture_watermark",
]
