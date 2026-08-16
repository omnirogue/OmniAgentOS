"""Persisted lifecycle state for background orchestrations."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from omniagentos.contracts import utc_now_iso

if TYPE_CHECKING:
    from omniagentos.orchestrator.contracts import ResumeState

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
LOG = logging.getLogger(__name__)


class OrchestrationsDal:
    """A dedicated SQLite connection for orchestration lifecycle writes."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        pid_alive: Callable[[int], bool] | None = None,
    ) -> None:
        self._lock = RLock()
        self._pid_alive = pid_alive or _pid_alive
        path = str(Path(db_path).expanduser()) if str(db_path) != ":memory:" else ":memory:"
        self._connection = sqlite3.connect(
            path, isolation_level=None, timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def create(
        self,
        orch_id: str,
        *,
        board_task_id: str,
        working_dir: str,
        goal: str = "",
        params_json: str = "{}",
    ) -> None:
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT INTO orchestrations "
                    "(id, board_task_id, working_dir, goal, params_json, status, heartbeat_at, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "board_task_id = excluded.board_task_id, "
                    "working_dir = CASE WHEN excluded.working_dir <> '' "
                    "THEN excluded.working_dir ELSE orchestrations.working_dir END, "
                    "goal = CASE WHEN excluded.goal <> '' "
                    "THEN excluded.goal ELSE orchestrations.goal END, "
                    "params_json = CASE WHEN excluded.params_json <> '{}' "
                    "THEN excluded.params_json ELSE orchestrations.params_json END",
                    (orch_id, board_task_id, working_dir, goal, params_json, now, now, now),
                )
        except sqlite3.OperationalError:
            LOG.debug("orchestration lifecycle create unavailable", exc_info=True)

    def set_status(
        self,
        orch_id: str,
        status: str,
        *,
        stage: str | None = None,
        error: str | None = None,
        conductor_pid: int | None = None,
        conductor_claimed_at: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        assignments = ["status = ?", "heartbeat_at = ?", "updated_at = ?"]
        values: list[Any] = [status, now, now]
        if stage is not None:
            assignments.append("stage = ?")
            values.append(stage)
        if error is not None or status == "completed":
            assignments.append("error = ?")
            values.append(error)
        if status != "queued":
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in _TERMINAL:
            assignments.append("finished_at = ?")
            values.append(now)
            assignments.append("conductor_pid = NULL")
            assignments.append("conductor_claimed_at = NULL")
        values.append(orch_id)
        where_clause = "WHERE id = ?"
        if status in _TERMINAL and conductor_pid is not None and conductor_claimed_at is not None:
            where_clause += " AND conductor_pid = ? AND conductor_claimed_at = ?"
            values.extend((conductor_pid, conductor_claimed_at))
        try:
            with self._lock:
                cursor = self._connection.execute(
                    f"UPDATE orchestrations SET {', '.join(assignments)} {where_clause}",
                    values,
                )
                return cursor.rowcount > 0
        except sqlite3.OperationalError:
            LOG.debug("orchestration lifecycle status update unavailable", exc_info=True)
            return True

    def heartbeat(
        self,
        orch_id: str,
        *,
        conductor_pid: int | None = None,
        conductor_claimed_at: str | None = None,
    ) -> bool:
        now = utc_now_iso()
        values: list[Any] = [now, now, orch_id]
        where_clause = "WHERE id = ?"
        if conductor_pid is not None and conductor_claimed_at is not None:
            where_clause += " AND conductor_pid = ? AND conductor_claimed_at = ?"
            values.extend((conductor_pid, conductor_claimed_at))
        try:
            with self._lock:
                cursor = self._connection.execute(
                    f"UPDATE orchestrations SET heartbeat_at = ?, updated_at = ? {where_clause}",
                    values,
                )
                return cursor.rowcount > 0
        except sqlite3.OperationalError:
            LOG.debug("orchestration lifecycle heartbeat unavailable", exc_info=True)
            return True

    def conductor_started(self, orch_id: str, *, pid: int) -> str | None:
        """Record the initial conductor without counting it as a resume."""
        now = utc_now_iso()
        claim_stamp = _claim_stamp()
        try:
            with self._lock:
                cursor = self._connection.execute(
                    "UPDATE orchestrations SET conductor_pid = ?, conductor_claimed_at = ?, "
                    "heartbeat_at = ?, updated_at = ? WHERE id = ? "
                    "AND status NOT IN ('completed', 'failed', 'cancelled')",
                    (pid, claim_stamp, now, now, orch_id),
                )
                return claim_stamp if cursor.rowcount > 0 else None
        except sqlite3.OperationalError:
            LOG.debug("orchestration conductor start unavailable", exc_info=True)
            return None

    def record_plan(self, run_id: str, plan_json: str, step_titles: list[str]) -> None:
        """Persist a plan and seed its ordered checkpoint rows."""
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._connection.execute(
                        "UPDATE orchestrations SET plan_json = ?, updated_at = ? WHERE id = ?",
                        (plan_json, now, run_id),
                    )
                    self._connection.executemany(
                        "INSERT INTO orchestration_steps "
                        "(run_id, seq, title, status, updated_at) "
                        "VALUES (?, ?, ?, 'pending', ?) "
                        "ON CONFLICT(run_id, seq) DO UPDATE SET title = excluded.title",
                        [(run_id, seq, title, now) for seq, title in enumerate(step_titles)],
                    )
                except sqlite3.Error:
                    self._connection.execute("ROLLBACK")
                    raise
                self._connection.execute("COMMIT")
        except sqlite3.Error:
            LOG.debug("orchestration plan checkpoint unavailable", exc_info=True)

    def step_started(self, run_id: str, seq: int, attempts: int) -> None:
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT INTO orchestration_steps "
                    "(run_id, seq, title, status, attempts, session_id, updated_at) "
                    "VALUES (?, ?, '', 'running', ?, NULL, ?) "
                    "ON CONFLICT(run_id, seq) DO UPDATE SET "
                    "status = 'running', attempts = excluded.attempts, "
                    "session_id = NULL, updated_at = excluded.updated_at",
                    (run_id, seq, attempts, now),
                )
        except sqlite3.Error:
            LOG.debug("orchestration step-start checkpoint unavailable", exc_info=True)

    def step_session(self, run_id: str, seq: int, session_id: str) -> None:
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute(
                    "UPDATE orchestration_steps SET session_id = ?, updated_at = ? "
                    "WHERE run_id = ? AND seq = ?",
                    (session_id, now, run_id, seq),
                )
        except sqlite3.Error:
            LOG.debug("orchestration step-session checkpoint unavailable", exc_info=True)

    def step_finished(
        self,
        run_id: str,
        seq: int,
        status: str,
        attempts: int,
        output_tail: str,
    ) -> None:
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute(
                    "UPDATE orchestration_steps SET status = ?, attempts = ?, "
                    "output_tail = ?, updated_at = ? WHERE run_id = ? AND seq = ?",
                    (status, attempts, output_tail, now, run_id, seq),
                )
        except sqlite3.Error:
            LOG.debug("orchestration step-finish checkpoint unavailable", exc_info=True)

    def load_resume_state(self, run_id: str) -> ResumeState | None:
        from omniagentos.orchestrator.contracts import ResumeState, ResumeStep

        try:
            with self._lock:
                orchestration = self._connection.execute(
                    "SELECT plan_json FROM orchestrations WHERE id = ?", (run_id,)
                ).fetchone()
                if orchestration is None or orchestration["plan_json"] is None:
                    return None
                rows = self._connection.execute(
                    "SELECT seq, title, status, session_id, attempts, output_tail "
                    "FROM orchestration_steps WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
        except sqlite3.Error:
            LOG.debug("orchestration resume-state read unavailable", exc_info=True)
            return None
        return ResumeState(
            plan_json=str(orchestration["plan_json"]),
            steps=[
                ResumeStep(
                    seq=int(row["seq"]),
                    title=str(row["title"]),
                    status=str(row["status"]),
                    session_id=None if row["session_id"] is None else str(row["session_id"]),
                    attempts=int(row["attempts"]),
                    output_tail=str(row["output_tail"]),
                )
                for row in rows
            ],
        )

    def reset_retry_steps(self, run_id: str) -> bool:
        """Reset exhausted step outcomes for an explicit operator retry."""
        now = utc_now_iso()
        try:
            with self._lock:
                self._connection.execute(
                    "UPDATE orchestration_steps SET status = 'pending', attempts = 0, "
                    "session_id = NULL, updated_at = ? WHERE run_id = ? "
                    "AND status IN ('failed', 'denied')",
                    (now, run_id),
                )
        except sqlite3.Error:
            LOG.debug("orchestration retry-step reset unavailable", exc_info=True)
            return False
        return True

    def claim_conductor(
        self,
        run_id: str,
        *,
        pid: int,
        stale_minutes: int,
        allow_failed_retry: bool,
        max_resumes: int = 10,
        max_retries: int = 2,
    ) -> dict[str, Any] | None:
        """Optimistically claim one resumable row; only one racing caller can win."""
        now = utc_now_iso()
        claim_stamp = _claim_stamp()
        cutoff = datetime.now(UTC) - timedelta(minutes=max(2, stale_minutes))
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT * FROM orchestrations WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    return None
                current = dict(row)
                status = str(current["status"])
                heartbeat = _as_utc(current.get("heartbeat_at"))
                stale = heartbeat is None or heartbeat < cutoff
                conductor_pid = current.get("conductor_pid")
                conductor_alive = conductor_pid is not None and self._pid_alive(int(conductor_pid))
                resume_count = int(current.get("resume_count") or 0)
                retry_count = int(current.get("retry_count") or 0)
                if status == "failed":
                    eligible = (
                        (allow_failed_retry or retry_count < max_retries)
                        and not conductor_alive
                        and resume_count < max_resumes
                    )
                else:
                    eligible = (
                        status not in _TERMINAL
                        and resume_count < max_resumes
                        and (conductor_pid is None or not conductor_alive or stale)
                    )
                if not eligible:
                    return None
                claimed = self._connection.execute(
                    "UPDATE orchestrations SET conductor_pid = ?, conductor_claimed_at = ?, "
                    "heartbeat_at = ?, updated_at = ?, status = 'running', "
                    "started_at = COALESCE(started_at, ?), finished_at = NULL, error = NULL, "
                    "resume_count = resume_count + 1, "
                    "retry_count = retry_count + CASE WHEN status = 'failed' THEN 1 ELSE 0 END "
                    "WHERE id = ? AND updated_at = ? AND status = ? "
                    "AND resume_count = ? AND retry_count = ? RETURNING *",
                    (
                        pid,
                        claim_stamp,
                        now,
                        now,
                        now,
                        run_id,
                        current["updated_at"],
                        status,
                        resume_count,
                        retry_count,
                    ),
                ).fetchone()
        except sqlite3.Error:
            LOG.debug("orchestration conductor claim unavailable", exc_info=True)
            return None
        return None if claimed is None else dict(claimed)

    def conductor_live(self, run_id: str, *, stale_minutes: int) -> bool:
        """Return whether a fresh heartbeat is held by a live process."""
        cutoff = datetime.now(UTC) - timedelta(minutes=max(0, stale_minutes))
        row = self.get(run_id)
        if row is None or row.get("conductor_pid") is None:
            return False
        heartbeat = _as_utc(row.get("heartbeat_at"))
        return (
            heartbeat is not None
            and heartbeat >= cutoff
            and self._pid_alive(int(row["conductor_pid"]))
        )

    def find_resumable(
        self,
        *,
        stale_minutes: int,
        include_failed_retry: bool,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        stale_cutoff = moment - timedelta(minutes=max(2, stale_minutes))
        try:
            backoff_minutes = max(
                0,
                int(os.environ.get("OMNIAGENTOS_ORCH_RETRY_BACKOFF_MINUTES", "5")),
            )
        except ValueError:
            LOG.warning("invalid OMNIAGENTOS_ORCH_RETRY_BACKOFF_MINUTES; using default 5")
            backoff_minutes = 5
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT * FROM orchestrations WHERE plan_json IS NOT NULL "
                    "AND (status NOT IN ('completed', 'failed', 'cancelled') "
                    "OR (? AND status = 'failed' AND retry_count < 2 "
                    "AND EXISTS (SELECT 1 FROM orchestration_steps "
                    "WHERE run_id = orchestrations.id "
                    "AND status IN ('running', 'pending')))) "
                    "ORDER BY updated_at, id",
                    (include_failed_retry,),
                ).fetchall()
        except sqlite3.Error:
            LOG.debug("orchestration resumable scan unavailable", exc_info=True)
            return []
        resumable: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            status = str(row["status"])
            if status == "failed":
                updated_at = _as_utc(row.get("updated_at"))
                retry_count = int(row.get("retry_count") or 0)
                backoff = timedelta(minutes=backoff_minutes * (retry_count + 1))
                if updated_at is not None and updated_at <= moment - backoff:
                    resumable.append(row)
                continue
            heartbeat = _as_utc(row.get("heartbeat_at"))
            conductor_pid = row.get("conductor_pid")
            dead = conductor_pid is not None and not self._pid_alive(int(conductor_pid))
            if dead or heartbeat is None or heartbeat <= stale_cutoff:
                resumable.append(row)
        return resumable

    def get(self, orch_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT * FROM orchestrations WHERE id = ?", (orch_id,)
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else dict(row)

    def get_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        unique = list(dict.fromkeys(ids))
        if not unique:
            return {}
        # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
        placeholders = ",".join("?" for _ in unique)
        try:
            with self._lock:
                rows = self._connection.execute(
                    f"SELECT * FROM orchestrations WHERE id IN ({placeholders})", unique
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {str(row["id"]): dict(row) for row in rows}

    def mark_stale_failed(self, *, stale_minutes: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(minutes=max(0, stale_minutes))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now = utc_now_iso()
        try:
            with self._lock:
                stale = self._connection.execute(
                    "SELECT id FROM orchestrations "
                    "WHERE status NOT IN ('completed', 'failed', 'cancelled') "
                    "AND plan_json IS NULL AND heartbeat_at < ? LIMIT 1",
                    (cutoff,),
                ).fetchone()
                if stale is None:
                    return []
                rows = self._connection.execute(
                    "UPDATE orchestrations SET status = 'failed', "
                    "error = 'stale heartbeat — orchestrator process died', "
                    "finished_at = ?, updated_at = ? "
                    "WHERE status NOT IN ('completed', 'failed', 'cancelled') "
                    "AND plan_json IS NULL AND heartbeat_at < ? RETURNING *",
                    (now, now, cutoff),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _claim_stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = ["OrchestrationsDal"]
