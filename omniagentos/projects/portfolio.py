"""Portfolio assembly for redesign Phase B — one request, constant query count.

``GET /api/projects/portfolio`` answers the whole attention screen without N+1
per-project queries. State precedence (first match wins):

  blocked → failing → running → idle → healthy
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.contracts import utc_now_iso

LOG = logging.getLogger(__name__)

_STATE_ORDER = ("blocked", "failing", "running", "idle", "healthy")
_STATE_RANK = {name: index for index, name in enumerate(_STATE_ORDER)}
_IDLE_HOURS = 48

# Production SQL helpers — shared with query-plan tests so plans assert real queries.
SQL_PROJECTS_LIST = (
    "SELECT id, name, parent_project_id, budget_usd, created_at, "
    "COALESCE(kind, 'project') AS kind "
    "FROM projects ORDER BY created_at DESC, id DESC"
)

SQL_PENDING_APPROVALS_BY_PROJECT = """
SELECT t.project_id AS project_id, COUNT(*) AS n
FROM approvals a
JOIN runs r ON r.id = a.run_id
JOIN tasks t ON t.id = r.task_id
WHERE a.state = 'pending' AND t.project_id IS NOT NULL
GROUP BY t.project_id
"""

SQL_RUNS_ROLLUP_BY_PROJECT = """
SELECT t.project_id AS project_id,
       SUM(CASE WHEN r.state IN ('failed','cancelled') THEN 1 ELSE 0 END) AS failed_n,
       SUM(CASE WHEN r.state IN ('running','queued','awaiting_approval','paused')
                THEN 1 ELSE 0 END) AS running_n,
       MAX(COALESCE(r.updated_at, r.created_at)) AS last_at,
       SUM(r.cost_usd) AS spent,
       SUM(CASE WHEN r.cost_usd IS NULL THEN 1 ELSE 0 END) AS cost_unknown_n
FROM runs r
JOIN tasks t ON t.id = r.task_id
WHERE t.project_id IS NOT NULL
GROUP BY t.project_id
"""

# H-11 join path (run → task → project). Archived cards must not poison health.
SQL_BLOCKED_BOARD_BY_PROJECT = """
SELECT t.project_id AS project_id, COUNT(*) AS n
FROM board_tasks bt
JOIN runs r ON r.id = bt.run_id
JOIN tasks t ON t.id = r.task_id
WHERE bt.status = 'blocked'
  AND bt.archived_at IS NULL
  AND t.project_id IS NOT NULL
GROUP BY t.project_id
"""

# Collab board listing shapes (L-10 / archive portion).
SQL_BOARD_LIST_ACTIVE = (
    "SELECT * FROM board_tasks WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC"
)
SQL_BOARD_LIST_ACTIVE_BY_STATUS = (
    "SELECT * FROM board_tasks WHERE status = ? AND archived_at IS NULL "
    "ORDER BY created_at DESC, id DESC"
)
SQL_BOARD_LIST_ARCHIVED = (
    "SELECT * FROM board_tasks WHERE archived_at IS NOT NULL ORDER BY created_at DESC, id DESC"
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _worst(a: str, b: str) -> str:
    return a if _STATE_RANK.get(a, 99) <= _STATE_RANK.get(b, 99) else b


def build_portfolio(connection: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the portfolio payload. One query per fact class."""
    projects = [dict(row) for row in connection.execute(SQL_PROJECTS_LIST).fetchall()]
    # Approvals pending by project (run → task → project; sessions have no project_id)
    pending_by_project: dict[str, int] = {}
    try:
        for row in connection.execute(SQL_PENDING_APPROVALS_BY_PROJECT).fetchall():
            pending_by_project[str(row["project_id"])] = int(row["n"] or 0)
    except sqlite3.OperationalError:
        LOG.debug("portfolio: pending approvals query skipped", exc_info=True)

    failed_by_project: dict[str, int] = {}
    running_by_project: dict[str, int] = {}
    last_activity: dict[str, str] = {}
    # float = measured total; None = at least one run has unknown cost (never 0.0).
    # Projects absent from the map have no runs → measured $0.00.
    spent_by_project: dict[str, float | None] = {}
    try:
        for row in connection.execute(SQL_RUNS_ROLLUP_BY_PROJECT).fetchall():
            pid = str(row["project_id"])
            failed_by_project[pid] = int(row["failed_n"] or 0)
            running_by_project[pid] = int(row["running_n"] or 0)
            if row["last_at"]:
                last_activity[pid] = str(row["last_at"])
            # Unknown cost is NULL, never 0.0. SQLite SUM ignores NULLs, so a
            # mixed known+unknown total would under-report as sum-of-known-only
            # without cost_unknown_n. COALESCE(cost, 0) was the prior lie.
            if int(row["cost_unknown_n"] or 0) > 0:
                spent_by_project[pid] = None
            else:
                spent_by_project[pid] = float(row["spent"] or 0.0)
    except sqlite3.OperationalError:
        LOG.debug("portfolio: runs rollup skipped", exc_info=True)

    # Board blocked counts (live cards only; archived never affect health)
    blocked_tasks: dict[str, int] = {}
    try:
        for row in connection.execute(SQL_BLOCKED_BOARD_BY_PROJECT).fetchall():
            blocked_tasks[str(row["project_id"])] = int(row["n"] or 0)
    except sqlite3.OperationalError:
        LOG.debug("portfolio: board blocked skipped", exc_info=True)

    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(hours=_IDLE_HOURS)
    by_id: dict[str, dict[str, Any]] = {}
    for proj in projects:
        pid = str(proj["id"])
        pending = pending_by_project.get(pid, 0)
        failed = failed_by_project.get(pid, 0)
        running = running_by_project.get(pid, 0)
        blocked_n = pending + blocked_tasks.get(pid, 0)
        last_at = last_activity.get(pid) or str(proj.get("created_at") or "")
        last_dt = _parse_ts(last_at)
        if blocked_n >= 1:
            state = "blocked"
        elif failed >= 1 and running == 0:
            state = "failing"
        elif running >= 1:
            state = "running"
        elif last_dt is None or last_dt < idle_cutoff:
            state = "idle"
        else:
            state = "healthy"
        doing = _doing_sentence(
            state=state,
            pending=pending,
            failed=failed,
            running=running,
            last_at=last_at,
        )
        by_id[pid] = {
            "id": pid,
            "name": str(proj.get("name") or ""),
            "parent_id": proj.get("parent_project_id"),
            "kind": str(proj.get("kind") or "project"),
            "path": [],  # filled below
            "depth": 0,
            "state": state,
            "rollup_state": state,
            "doing": doing,
            "blocked_count": blocked_n,
            "failed_count": failed,
            "running_count": running,
            "budget_usd": proj.get("budget_usd"),
            # No rollup row → no runs → measured zero. Present + None → unknown.
            "spent_usd": spent_by_project[pid] if pid in spent_by_project else 0.0,
            "last_activity_at": last_at or None,
        }

    # Build path / depth / rollup
    for pid, node in by_id.items():
        path: list[str] = []
        cursor = pid
        seen: set[str] = set()
        chain: list[str] = []
        while cursor and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            parent = by_id.get(cursor, {}).get("parent_id")
            cursor = str(parent) if parent else ""
        chain.reverse()
        for cid in chain:
            name = by_id.get(cid, {}).get("name") or cid
            path.append(str(name))
        node["path"] = path
        node["depth"] = max(0, len(path) - 1)

    # Rollup worst state up the parent chain
    for _pid, node in by_id.items():
        parent_cursor = node.get("parent_id")
        while parent_cursor and str(parent_cursor) in by_id:
            parent = by_id[str(parent_cursor)]
            parent["rollup_state"] = _worst(str(parent["rollup_state"]), str(node["rollup_state"]))
            parent_cursor = parent.get("parent_id")

    durable = [n for n in by_id.values() if n["kind"] != "scratch"]
    scratch = [n for n in by_id.values() if n["kind"] == "scratch"]

    # Stable: group by state rank, then activity desc within band
    bands: dict[int, list[dict[str, Any]]] = {}
    for item in durable:
        bands.setdefault(_STATE_RANK.get(str(item["state"]), 99), []).append(item)
    ordered: list[dict[str, Any]] = []
    for rank in sorted(bands):
        band = sorted(
            bands[rank],
            key=lambda i: str(i.get("last_activity_at") or ""),
            reverse=True,
        )
        ordered.extend(band)

    return {
        "generated_at": utc_now_iso(),
        "projects": ordered,
        "scratch_count": len(scratch),
        "scratch": sorted(
            scratch,
            key=lambda i: str(i.get("last_activity_at") or ""),
            reverse=True,
        )[:50],
    }


def _doing_sentence(
    *,
    state: str,
    pending: int,
    failed: int,
    running: int,
    last_at: str,
) -> str:
    if state == "blocked" and pending:
        return f"Paused on {pending} approval{'s' if pending != 1 else ''} — needs a human."
    if state == "blocked":
        return "Blocked work waiting on the board — needs a human."
    if state == "failing":
        return f"{failed} failed run{'s' if failed != 1 else ''} since last success."
    if state == "running":
        return f"{running} active run{'s' if running != 1 else ''} or session{'s' if running != 1 else ''}."
    if state == "idle":
        if last_at:
            return f"Nothing queued. Last activity {last_at}."
        return "Nothing queued. No recent activity."
    return "Recent activity, nothing pending or failed."


def sweep_scratch_projects(
    connection: sqlite3.Connection,
    *,
    days: int = 30,
    apply: bool = True,
    _mid_sweep_hook: Any | None = None,
) -> dict[str, Any]:
    """Delete kind=scratch projects with no activity in *days* (Phase A3).

    Apply mode is atomic under ``isolation_level=None`` (autocommit): the whole
    candidate set is wrapped in an explicit ``BEGIN IMMEDIATE`` / commit, and
    any failure rolls the entire sweep back. FK children are detached (not
    cascaded) so dependent tasks/runs survive.

    ``_mid_sweep_hook`` is test-only: called after each project is deleted so
    injected failures can prove rollback restores the pre-sweep state.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = connection.execute(
            """
            SELECT p.id, p.name, p.created_at
            FROM projects p
            WHERE COALESCE(p.kind, 'project') = 'scratch'
              AND p.created_at < ?
              AND NOT EXISTS (
                SELECT 1 FROM tasks t
                JOIN runs r ON r.task_id = t.id
                WHERE t.project_id = p.id
                  AND COALESCE(r.updated_at, r.created_at, r.finished_at) >= ?
              )
            """,
            (cutoff, cutoff),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return {"removed": 0, "error": str(exc), "dry_run": not apply}
    ids = [str(r["id"]) for r in rows]
    if apply and ids:
        # Explicit transaction: isolation_level=None autocommits each statement
        # unless we open one ourselves.
        connection.execute("BEGIN IMMEDIATE")
        try:
            for pid in ids:
                connection.execute(
                    "UPDATE tasks SET project_id = NULL WHERE project_id = ?", (pid,)
                )
                connection.execute(
                    "UPDATE projects SET parent_project_id = NULL WHERE parent_project_id = ?",
                    (pid,),
                )
                connection.execute("DELETE FROM projects WHERE id = ?", (pid,))
                if _mid_sweep_hook is not None:
                    _mid_sweep_hook(connection, pid)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        LOG.info("scratch project sweep removed %d projects", len(ids))
    return {
        "removed": len(ids) if apply else 0,
        "candidates": [{"id": str(r["id"]), "name": r["name"]} for r in rows],
        "cutoff": cutoff,
        "dry_run": not apply,
    }


__all__ = [
    "SQL_BLOCKED_BOARD_BY_PROJECT",
    "SQL_BOARD_LIST_ACTIVE",
    "SQL_BOARD_LIST_ACTIVE_BY_STATUS",
    "SQL_BOARD_LIST_ARCHIVED",
    "SQL_PENDING_APPROVALS_BY_PROJECT",
    "SQL_PROJECTS_LIST",
    "SQL_RUNS_ROLLUP_BY_PROJECT",
    "build_portfolio",
    "sweep_scratch_projects",
]
