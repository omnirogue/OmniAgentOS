"""Task-conversation steering fan-in for longhaul executors."""

from __future__ import annotations

from typing import Any

from omniagentos.longhaul.prompts import steering_wrap
from omniagentos.longhaul.store import LonghaulStore


class SteeringManager:
    """Persist task guidance and fan it into live or successor sessions."""

    def __init__(self, store: LonghaulStore) -> None:
        self.store = store

    def append_turn(
        self,
        task_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        envelope = dict(meta or {})
        envelope.setdefault("kind", "steering")
        envelope["delivery"] = {"pending": True}
        self.store.append_task_turn(task_id, role, content, envelope)
        # append_task_turn is intentionally the frozen W1 seam and returns None.
        # Read the newest row back to expose the durable id/sequence to callers.
        turns = self.store.task_turns(task_id, limit=1)
        if not turns:
            raise RuntimeError("steering turn was not persisted")
        return turns[0]

    def pending_turns(self, task_id: str) -> list[dict[str, Any]]:
        return self.store.pending_steering(task_id)

    def mark_delivered(
        self,
        turn_id_or_scope_seq: str | tuple[str, int],
        session_id: str,
    ) -> bool:
        return self.store.mark_turn_delivered(turn_id_or_scope_seq, session_id)

    def format_for_injection(self, turns: list[dict[str, Any]]) -> str:
        return steering_wrap(turns)

    def enqueue_for_live_session(self, session_id: str, content: str) -> dict[str, str]:
        """Queue live guidance on the Session Bridge's atomic hook channel."""

        from omniagentos.sessions.dal import SessionsDal
        from omniagentos.sessions.steering_marker import mark_steering_pending

        dal = SessionsDal(self.store._db_path)
        try:
            queued = dal.enqueue_message(session_id, content, created_by="longhaul")
            mark_steering_pending(session_id)
            return queued
        finally:
            dal.close()


__all__ = ["SteeringManager"]
