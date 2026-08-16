"""Loop instance seeding on API startup.

This module ensures that critical loop routines (W2 inbox triage, W3 health
monitor, etc.) are created and enabled on API startup via the established
``RoutinesStore.create_routine`` path with full validation.
"""

from __future__ import annotations

from typing import Any

from omniagentos.scheduler.routines import W3_HEALTH_MONITOR_ROUTINE_NAME, w3_health_monitor_routine


def ensure_w3_health_monitor_routine(store: Any) -> dict[str, Any]:
    """Ensure W3 health monitor & self-heal loop is seeded and enabled.

    W3 detects component failures via health-sentinel snapshots and
    auto-repairs allowlisted components via launchctl kickstart. Unknown
    remedies escalate to human approval (T3 tier).

    The loop is idempotent: if it already exists and is enabled, this returns
    the existing routine. If it exists but is disabled, returns it as-is
    (re-enabling is a manual/approval operation). If it doesn't exist, creates
    and returns the new routine.

    Args:
        store: SqliteStore instance

    Returns:
        The W3 routine row (dict), either newly created or existing.
    """
    from omniagentos.scheduler.store import RoutinesStore

    r_store = RoutinesStore(store)
    existing = r_store.list_routines()
    for r in existing:
        if r.get("name") == W3_HEALTH_MONITOR_ROUTINE_NAME:
            return r

    return r_store.create_routine(w3_health_monitor_routine())


__all__ = [
    "ensure_w3_health_monitor_routine",
]
