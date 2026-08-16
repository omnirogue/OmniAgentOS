"""Read-only, UTC-day dashboard summary for operators."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from omniagentos.contracts import default_db_path

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/today")
def today_dashboard() -> dict[str, Any]:
    """Return the actual swarm activity and unread escalations for today (UTC)."""
    today = datetime.now(UTC).date().isoformat()
    connection = sqlite3.connect(default_db_path())
    connection.row_factory = sqlite3.Row
    try:
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS started_today,
                COALESCE(SUM(CASE WHEN end_reason = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed_today
            FROM swarm_attempts
            WHERE DATE(started_at) = ?
            """,
            (today,),
        ).fetchone()

        completion_by_provider = connection.execute(
            """
            SELECT
                provider,
                COUNT(*) AS started,
                SUM(CASE WHEN end_reason = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM swarm_attempts
            WHERE DATE(started_at) = ?
            GROUP BY provider
            ORDER BY provider ASC
            """,
            (today,),
        ).fetchall()

        end_reasons = connection.execute(
            """
            SELECT
                COALESCE(end_reason, 'running') AS end_reason,
                detail,
                COUNT(*) AS count
            FROM swarm_attempts
            WHERE DATE(started_at) = ?
            GROUP BY COALESCE(end_reason, 'running'), detail
            ORDER BY count DESC, end_reason ASC, detail ASC
            LIMIT 3
            """,
            (today,),
        ).fetchall()

        escalations = connection.execute(
            """
            SELECT id, title, body, created_at
            FROM notifications
            WHERE kind = 'escalation' AND read_at IS NULL
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        # Two DIFFERENT numbers that must never be conflated. The unread
        # escalation feed above is a notification BACKLOG (LIMIT 20, and rows
        # can outlive the session they refer to); counting it and calling the
        # result "sessions waiting for approval" overstates the live figure by
        # roughly 10x. What actually needs a human right now is the count of
        # sessions parked in state='awaiting_approval'.
        sessions_awaiting_approval = int(
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE state = 'awaiting_approval'"
            ).fetchone()[0]
        )
    finally:
        connection.close()

    return {
        "started_today": int(counts["started_today"]),
        "completed_today": int(counts["completed_today"]),
        "completion_by_provider": [
            {
                "provider": str(row["provider"]),
                "started": int(row["started"]),
                "completed": int(row["completed"]),
                "completion_rate": int(row["completed"]) / int(row["started"]) * 100,
            }
            for row in completion_by_provider
        ],
        "end_reasons": [{"end_reason": row["end_reason"], "detail": row["detail"], "count": row["count"]} for row in end_reasons],
        "escalations": [{"id": row["id"], "title": row["title"], "body": row["body"], "created_at": row["created_at"]} for row in escalations],
        "sessions_awaiting_approval": sessions_awaiting_approval,
    }


__all__ = ["router", "today_dashboard"]
