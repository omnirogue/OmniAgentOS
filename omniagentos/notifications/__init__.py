"""Durable, inspectable notification feed for OmniAgentOS.

Every notification the system raises is PERSISTED (a ``notifications`` row) and
THEN best-effort delivered to the OS / push channel. Because the persisted row
carries ``ref_type`` + ``ref_id`` linking it to the real approval / run /
session / alert it is about, a notification always has an inspectable,
actionable target -- an "approval required" notification can never be a phantom
pointing at an empty queue.

- :class:`NotificationsDal` -- auto-migrated, serialized table access.
- :mod:`omniagentos.notifications.service` -- the single write seam every
  notification source routes through (persist first, then push).
"""

from __future__ import annotations

from omniagentos.notifications.dal import NotificationsDal
from omniagentos.notifications.service import (
    NOTIFICATION_KINDS,
    build_approval_lookup,
    build_board_lookup,
    notify_alert,
    notify_approval_requested,
    notify_session_awaiting,
    notify_task_done,
    record_notification,
    resolve_target,
    serialize_notification,
)

__all__ = [
    "NOTIFICATION_KINDS",
    "NotificationsDal",
    "build_approval_lookup",
    "build_board_lookup",
    "notify_alert",
    "notify_approval_requested",
    "notify_session_awaiting",
    "notify_task_done",
    "record_notification",
    "resolve_target",
    "serialize_notification",
]
