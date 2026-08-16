"""Bounded, optional health-sentinel context for worker prompts."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.runtime_paths import resolve_var_root

LOG = logging.getLogger(__name__)

SENTINEL_INTERVAL_SECONDS = 1800
SNAPSHOT_MAX_AGE_SECONDS = 2 * SENTINEL_INTERVAL_SECONDS
SNAPSHOT_FUTURE_SKEW_SECONDS = 60
HEALTH_SNAPSHOT_PATH = resolve_var_root(leaf=("health-sentinel", "latest.json"))


def _unavailable(
    error: str, *, ts: str | None = None, age_seconds: float | None = None
) -> dict[str, Any]:
    return {
        "available": False,
        "checks": [],
        "ts": ts,
        "age_seconds": age_seconds,
        "error": error,
    }


def read_health_snapshot(
    snapshot_path: Path | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Return only fresh, readable sentinel health data.

    A missing, malformed, stale, or future-stamped snapshot is unavailable rather
    than healthy.  The producer contract names its timestamp field ``ts``.
    """
    path = snapshot_path or HEALTH_SNAPSHOT_PATH
    if not path.is_file():
        return _unavailable("snapshot file not found")
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - health context must be best-effort.
        return _unavailable(f"snapshot read failed: {exc}")
    if not isinstance(data, Mapping):
        return _unavailable("snapshot is not an object")

    raw_ts = data.get("ts")
    if not isinstance(raw_ts, str) or not raw_ts:
        return _unavailable("snapshot carries no readable 'ts'")
    try:
        stamped = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=UTC)
        age_seconds = ((now or datetime.now(UTC)) - stamped).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        return _unavailable(f"snapshot age could not be established: {exc}", ts=raw_ts)

    checks = data.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return _unavailable("snapshot carries no readable checks", ts=raw_ts, age_seconds=age_seconds)
    if age_seconds > SNAPSHOT_MAX_AGE_SECONDS:
        return _unavailable("snapshot is stale", ts=raw_ts, age_seconds=age_seconds)
    if age_seconds < -SNAPSHOT_FUTURE_SKEW_SECONDS:
        return _unavailable("snapshot is too far in the future", ts=raw_ts, age_seconds=age_seconds)
    return {
        "available": True,
        "checks": checks,
        "ts": raw_ts,
        "age_seconds": age_seconds,
    }


def build_health_digest(snapshot: Mapping[str, Any]) -> str:
    """Render a compact alert for degraded checks, or nothing when unavailable."""
    try:
        if not snapshot.get("available"):
            LOG.warning("Health digest omitted: %s", snapshot.get("error", "snapshot unavailable"))
            return ""
        checks = snapshot.get("checks")
        ts = snapshot.get("ts")
        if not isinstance(checks, list) or not isinstance(ts, str) or not ts:
            LOG.warning("Health digest omitted: malformed available snapshot")
            return ""
        degraded = [
            check
            for check in checks
            if isinstance(check, Mapping) and str(check.get("status") or "unknown").lower() != "ok"
        ]
        if not degraded:
            return ""
        lines = ["## System Health Alert"]
        for check in degraded[:4]:
            name = "-".join(str(check.get("name") or "unnamed").split()[:4])[:24]
            status = str(check.get("status") or "unknown").upper()[:12]
            lines.append(f"- {name}: {status}")
        if len(degraded) > 4:
            lines.append(f"- +{len(degraded) - 4} additional degraded checks")
        lines.append(f"Last update: {ts}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - prompt construction must never fail for health context.
        LOG.exception("Health digest omitted: digest construction failed")
        return ""
