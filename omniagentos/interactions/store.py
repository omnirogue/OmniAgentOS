"""SQLite DAL for ``agent_interactions`` (migration 058)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from omniagentos.db.store import SqliteStore


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return f"ixn_{uuid.uuid4().hex}"


_Z_SUFFIX = re.compile(r"([+-]\d{2}:\d{2}|Z)$")


def normalize_timestamp(value: str | None) -> str | None:
    """Normalize timestamps to UTC ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepts already-normalized Zulu, space-separated, and offset forms.
    Returns ``None`` for empty input; raises ``ValueError`` for garbage.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    text = raw.replace(" ", "T")
    if text.endswith("Z"):
        candidate = text[:-1] + "+00:00"
    elif _Z_SUFFIX.search(text):
        candidate = text
    else:
        # Naive timestamps are treated as UTC.
        candidate = text + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class InteractionsStore:
    """Nudge/question/answer channel for running work units."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def create(
        self,
        *,
        work_ref_type: str,
        work_ref_id: str,
        direction: str,
        kind: str,
        body: str,
        session_id: str | None = None,
        blocking_policy: str = "none",
        author: str | None = None,
        parent_id: str | None = None,
        expires_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if direction not in {"user_to_agent", "agent_to_user"}:
            raise ValueError(f"invalid direction: {direction!r}")
        if kind not in {"nudge", "question", "answer"}:
            raise ValueError(f"invalid kind: {kind!r}")
        if blocking_policy not in {"none", "checkpoint", "wait"}:
            raise ValueError(f"invalid blocking_policy: {blocking_policy!r}")
        now = _now()
        expires_norm = normalize_timestamp(expires_at) if expires_at is not None else None
        row = {
            "id": _new_id(),
            "created_at": now,
            "updated_at": now,
            "work_ref_type": work_ref_type,
            "work_ref_id": work_ref_id,
            "session_id": session_id,
            "direction": direction,
            "kind": kind,
            "blocking_policy": blocking_policy,
            "status": "active",
            "parent_id": parent_id,
            "author": author,
            "body": body,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            "delivered_at": None,
            "answered_at": None,
            "expires_at": expires_norm,
        }
        with self._store._lock:
            self._store._connection.execute(
                """
                INSERT INTO agent_interactions (
                  id, created_at, updated_at, work_ref_type, work_ref_id, session_id,
                  direction, kind, blocking_policy, status, parent_id, author, body,
                  metadata_json, delivered_at, answered_at, expires_at
                ) VALUES (
                  :id, :created_at, :updated_at, :work_ref_type, :work_ref_id, :session_id,
                  :direction, :kind, :blocking_policy, :status, :parent_id, :author, :body,
                  :metadata_json, :delivered_at, :answered_at, :expires_at
                )
                """,
                row,
            )
            self._store._connection.commit()
        return row

    def mark_delivered(self, interaction_id: str) -> dict[str, Any] | None:
        """Delivery-once: first delivery wins; subsequent calls are no-ops."""
        now = _now()
        with self._store._lock:
            cur = self._store._connection.execute(
                """
                UPDATE agent_interactions
                SET status = 'delivered', delivered_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND delivered_at IS NULL
                """,
                (now, now, interaction_id),
            )
            self._store._connection.commit()
            if cur.rowcount == 0:
                return self.get(interaction_id)
            return self.get(interaction_id)

    def create_answer(
        self,
        parent_id: str,
        answer_body: str,
        *,
        author: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Thread an answer under a question/nudge without overwriting the original body.

        Marks the parent ``answered`` and inserts a child row with
        ``kind='answer'`` and ``parent_id`` set. Returns
        ``{"parent": ..., "answer": ...}`` or ``None`` if the parent is missing
        or not answerable.
        """
        parent = self.get(parent_id)
        if parent is None:
            return None
        if parent.get("kind") not in {"question", "nudge"}:
            raise ValueError(
                f"cannot answer interaction kind={parent.get('kind')!r}; "
                "only question/nudge may be answered"
            )
        if parent.get("status") in {"answered", "expired", "canceled"}:
            raise ValueError(
                f"interaction {parent_id} is {parent.get('status')!r} and cannot be answered"
            )

        # Opposite direction of the parent (agent_to_user question → user_to_agent answer).
        parent_dir = str(parent.get("direction") or "agent_to_user")
        answer_dir = "user_to_agent" if parent_dir == "agent_to_user" else "agent_to_user"
        answer = self.create(
            work_ref_type=str(parent["work_ref_type"]),
            work_ref_id=str(parent["work_ref_id"]),
            direction=answer_dir,
            kind="answer",
            body=answer_body,
            session_id=parent.get("session_id"),
            blocking_policy="none",
            author=author,
            parent_id=parent_id,
            metadata=metadata,
        )
        now = _now()
        with self._store._lock:
            cur = self._store._connection.execute(
                """
                UPDATE agent_interactions
                SET status = 'answered', answered_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('active', 'delivered')
                """,
                (now, now, parent_id),
            )
            self._store._connection.commit()
            if cur.rowcount == 0:
                # Race: parent transitioned; leave the answer row but report current state.
                return {"parent": self.get(parent_id), "answer": answer}
        return {"parent": self.get(parent_id), "answer": answer}

    def mark_answered(self, interaction_id: str, answer_body: str) -> dict[str, Any] | None:
        """Compatibility wrapper: threads an answer rather than overwriting the question."""
        result = self.create_answer(interaction_id, answer_body)
        if result is None:
            return None
        return result.get("parent")

    def expire_pending(self, *, now: str | None = None) -> int:
        """Expire active *and* delivered-unanswered rows past ``expires_at``.

        Answered/canceled rows are left alone. Timestamps are compared as
        normalized UTC Zulu strings (ISO-8601 lexicographic order).
        """
        clock = normalize_timestamp(now) if now else _now()
        assert clock is not None
        with self._store._lock:
            cur = self._store._connection.execute(
                """
                UPDATE agent_interactions
                SET status = 'expired', updated_at = ?
                WHERE status IN ('active', 'delivered')
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
                """,
                (clock, clock),
            )
            self._store._connection.commit()
            return int(cur.rowcount or 0)

    def get(self, interaction_id: str) -> dict[str, Any] | None:
        with self._store._lock:
            row = self._store._connection.execute(
                "SELECT * FROM agent_interactions WHERE id = ?",
                (interaction_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_pending(
        self,
        *,
        work_ref_type: str | None = None,
        work_ref_id: str | None = None,
        session_id: str | None = None,
        blocking_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if work_ref_type:
            clauses.append("work_ref_type = ?")
            params.append(work_ref_type)
        if work_ref_id:
            clauses.append("work_ref_id = ?")
            params.append(work_ref_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if blocking_only:
            clauses.append("blocking_policy IN ('checkpoint', 'wait')")
        sql = (
            "SELECT * FROM agent_interactions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at ASC"
        )
        with self._store._lock:
            rows = self._store._connection.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
