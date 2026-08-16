"""Consumer layer for agent interactions (L-16).

Runtime callers:
  * API routes in ``omniagentos.api.routes.grok_ops`` (answer / expire / consume)
  * Any owned service that holds a ``SqliteStore``

L10 plain-data handoff (do **not** edit ``swarm/scheduler.py`` from this
satellite): a routine or scheduler tick may call
:func:`expire_due_interactions` with a store-like object and receive a plain
dict ``{"expired": int, "capability": "interactions.expire"}``. L10 owns when
to invoke it; this package owns the pure expire semantics.
"""

from __future__ import annotations

import logging
from typing import Any

from omniagentos.interactions.store import InteractionsStore

logger = logging.getLogger(__name__)


class InteractionConsumer:
    """Consumes interactions: expire → deliver pending → answer threading."""

    def __init__(self, store: InteractionsStore) -> None:
        self.store = store

    def expire_due(self, *, now: str | None = None) -> int:
        """Expire active and delivered-unanswered rows past ``expires_at``."""
        n = self.store.expire_pending(now=now)
        if n:
            logger.info("interactions expired count=%s", n)
        return n

    def consume_pending(
        self,
        work_ref_type: str,
        work_ref_id: str,
        *,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Expire overdue rows, then mark remaining pending as delivered."""
        self.expire_due(now=now)
        pending = self.store.list_pending(work_ref_type=work_ref_type, work_ref_id=work_ref_id)
        delivered: list[dict[str, Any]] = []
        for ixn in pending:
            result = self.store.mark_delivered(ixn["id"])
            if result and result.get("status") == "delivered":
                delivered.append(result)
        return delivered

    def answer(
        self,
        interaction_id: str,
        answer_body: str,
        *,
        author: str | None = None,
    ) -> dict[str, Any] | None:
        """Thread an answer under a question without overwriting the original."""
        return self.store.create_answer(interaction_id, answer_body, author=author)


def expire_due_interactions(store: Any, *, now: str | None = None) -> dict[str, int | str]:
    """Plain-data handoff for L10 routines/scheduler (no scheduler import here).

    ``store`` is a ``SqliteStore`` (or compatible) already opened by the caller.
    """
    from omniagentos.db.store import SqliteStore
    from omniagentos.interactions.store import InteractionsStore as _Store

    if not isinstance(store, SqliteStore):
        # Accept duck-typed stores that expose the same lock/connection surface
        # used by InteractionsStore; still wrap explicitly for type clarity.
        pass
    consumer = InteractionConsumer(_Store(store))  # type: ignore[arg-type]
    return {
        "expired": consumer.expire_due(now=now),
        "capability": "interactions.expire",
    }
