"""HTTP surface for the durable notification feed.

- ``GET /api/notifications`` -- newest-first list, optionally unread-only, each
  row enriched with a resolved ``target`` (so an approval-kind notification
  carries its ``approval_id`` and current approval state; a resolved/expired
  approval reports its outcome instead of a dead link).
- ``GET /api/notifications/count`` -- unread badge count for the topbar bell;
  ``?actionable=1`` additionally reports the unread count restricted to the
  kinds that need a human decision (``ACTIONABLE_KINDS``).
- ``POST /api/notifications/{id}/read`` -- mark one read.
- ``POST /api/notifications/read-all`` -- mark every unread row read, optionally
  restricted to ``kinds`` (clear the noise, keep the approvals).

Actual approval decisions still go through the existing decide route
(``POST /api/approvals/{id}/decision``); this module only reads/marks the feed.

AUTH: the mutating routes here carry no route-level check by design -- the
app-level ``require_session_token`` dependency in ``api.main`` gates EVERY
POST/PUT/PATCH/DELETE outside a tiny public allowlist that this router is not
in. ``tests/notifications/test_api.py`` pins that for ``read-all`` explicitly so
the bulk mutation can never quietly become public.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from omniagentos.api.routes.control import fail
from omniagentos.contracts import default_db_path
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.notifications.service import (
    ACTIONABLE_KINDS,
    NOTIFICATION_KINDS,
    build_approval_lookup,
    build_board_lookup,
    serialize_notification,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class ReadAllRequest(BaseModel):
    """Optional body for the bulk mark-read sweep.

    ``kinds=None`` (or no body at all) clears every unread row. An explicit
    empty list clears nothing — see :meth:`NotificationsDal.mark_all_read`.
    """

    kinds: list[str] | None = None


_NOTIFICATIONS_DAL: Any = None
_NOTIFICATIONS_DAL_LOCK = threading.Lock()


def get_notifications_dal() -> Any:
    """Return the process-lifetime NotificationsDal (one connection, serialized).

    Mirrors ``sessions.get_sessions_dal``: a single long-lived DAL shared across
    requests rather than a per-request connection.
    """
    global _NOTIFICATIONS_DAL
    if _NOTIFICATIONS_DAL is None:
        with _NOTIFICATIONS_DAL_LOCK:
            if _NOTIFICATIONS_DAL is None:
                _NOTIFICATIONS_DAL = NotificationsDal(default_db_path())
    return _NOTIFICATIONS_DAL


def _open_sessions_reader() -> Any:
    """Open the sessions reader for approval point lookups.

    No fail-open here: :func:`build_approval_lookup` turns open failures into a
    raising lookup so :func:`resolve_target` reports ``state='unavailable'``
    rather than measured absence.
    """
    from omniagentos.api.routes.control import _open_sessions_reader as open_reader

    return open_reader()


def _open_collab_store() -> Any:
    """Open the collab store for board-task point lookups.

    No fail-open here: :func:`build_board_lookup` turns open failures into a
    raising lookup so missing and unavailable stay distinct.
    """
    from omniagentos.api.routes.collab import get_collab_store

    return get_collab_store()


@router.get("")
def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    dal = get_notifications_dal()
    rows = dal.list(unread_only=unread, limit=limit)
    # Build lookups once per request via the package builders (not fail-open
    # private factories that swallow open errors into lambda: None).
    approval_lookup = build_approval_lookup(_open_sessions_reader)
    board_lookup = build_board_lookup(_open_collab_store)
    return [
        serialize_notification(
            row,
            approval_lookup=approval_lookup,
            board_lookup=board_lookup,
        )
        for row in rows
    ]


@router.get("/count")
def notification_count(actionable: bool = Query(default=False)) -> dict[str, int]:
    """Unread badge counts.

    ``unread`` is unchanged (every unread row) so existing clients keep working.
    ``?actionable=1`` adds ``actionable``: the unread rows whose kind needs a
    human decision (approval / escalation / blocked) -- the number that should
    drive an attention badge, since the rest of the feed is a log, not a queue.
    """
    dal = get_notifications_dal()
    counts = {"unread": dal.unread_count()}
    if actionable:
        counts["actionable"] = dal.unread_count(kinds=sorted(ACTIONABLE_KINDS))
    return counts


@router.post("/read-all")
def mark_all_notifications_read(body: ReadAllRequest | None = None) -> dict[str, int]:
    """Mark every unread notification read; return how many rows changed.

    Optional ``kinds`` restricts the sweep. Unknown kinds are rejected (400)
    rather than silently matching nothing, so a typo cannot look like "there
    was nothing to clear".
    """
    kinds = body.kinds if body is not None else None
    if kinds is not None:
        unknown = sorted({str(kind) for kind in kinds} - set(NOTIFICATION_KINDS))
        if unknown:
            fail(400, "invalid_kind", "unknown notification kind", {"kinds": unknown})
    updated = get_notifications_dal().mark_all_read(kinds=kinds)
    return {"updated": updated}


@router.post("/{notification_id}/read")
def mark_notification_read(notification_id: str) -> dict[str, Any]:
    dal = get_notifications_dal()
    if dal.get(notification_id) is None:
        fail(404, "not_found", "notification not found", {"id": notification_id})
    updated = dal.mark_read(notification_id)
    if updated is None:
        fail(404, "not_found", "notification not found", {"id": notification_id})
    return serialize_notification(
        updated,
        approval_lookup=build_approval_lookup(_open_sessions_reader),
        board_lookup=build_board_lookup(_open_collab_store),
    )


__all__ = ["ReadAllRequest", "get_notifications_dal", "router"]
