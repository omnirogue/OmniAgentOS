"""Production writers for North Star measurement carriers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PHASES: tuple[str, ...] = (
    "accepted",
    "planned",
    "first_useful_work",
    "lanes_complete",
    "integrated",
    "gated",
    "reviewed",
    "completed",
)


@dataclass(frozen=True)
class AllocationMeasurement:
    """Persisted axes used to decide one C-31 allocation frontier point."""

    config_id: str
    formation_selections: int
    swarm_attempts: int
    session_cost_usd: float
    completion_seconds: float
    quality_score: float

    def __post_init__(self) -> None:
        costs = (
            self.formation_selections,
            self.swarm_attempts,
            self.session_cost_usd,
            self.completion_seconds,
        )
        if not self.config_id or min(costs) < 0:
            raise ValueError("configuration id is required and costs cannot be negative")


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value}") from exc


def _inserted_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("collector insert did not produce a row id")
    return cursor.lastrowid


def record_rediscovery_event(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    context_key: str,
    first_seen_at: str,
    rediscovered_at: str,
    source_digest: str,
    trace: Mapping[str, Any],
) -> int:
    """Persist a context rediscovery with its original source and causal trace."""
    if not attempt_id or not context_key:
        raise ValueError("attempt_id and context_key are required")
    if _timestamp(rediscovered_at) < _timestamp(first_seen_at):
        raise ValueError("rediscovered_at cannot precede first_seen_at")
    if len(source_digest) != 64 or any(char not in "0123456789abcdef" for char in source_digest):
        raise ValueError("source_digest must be a lowercase sha256 digest")
    trace_json = json.dumps(dict(trace), sort_keys=True, separators=(",", ":"))
    cursor = connection.execute(
        "INSERT INTO rediscovery_event_traces "
        "(attempt_id, context_key, first_seen_at, rediscovered_at, source_digest, trace_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            context_key,
            first_seen_at,
            rediscovered_at,
            source_digest,
            trace_json,
        ),
    )
    return _inserted_id(cursor)


def record_provisioning_latency(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    resource_kind: str,
    requested_at: str,
    ready_at: str,
    outcome: str,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Persist measured request-to-ready latency for a provisioned resource."""
    if not attempt_id or not resource_kind:
        raise ValueError("attempt_id and resource_kind are required")
    requested = _timestamp(requested_at)
    ready = _timestamp(ready_at)
    if ready < requested:
        raise ValueError("ready_at cannot precede requested_at")
    if outcome not in {"ready", "failed", "timeout"}:
        raise ValueError("outcome must be ready, failed, or timeout")
    latency_ms = round((ready - requested).total_seconds() * 1000)
    detail_json = json.dumps(dict(detail or {}), sort_keys=True, separators=(",", ":"))
    cursor = connection.execute(
        "INSERT INTO provisioning_latency_events "
        "(attempt_id, resource_kind, requested_at, ready_at, latency_ms, outcome, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            resource_kind,
            requested_at,
            ready_at,
            latency_ms,
            outcome,
            detail_json,
        ),
    )
    return _inserted_id(cursor)


def record_phase_baseline(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    baseline_key: str,
    anchors: Mapping[str, float],
) -> int:
    """Persist a complete C-17 phase baseline from monotonic event anchors."""
    if not run_id or not baseline_key:
        raise ValueError("run_id and baseline_key are required")
    missing = [phase for phase in PHASES if phase not in anchors]
    if missing:
        raise ValueError(f"missing phase anchors: {', '.join(missing)}")
    values = [float(anchors[phase]) for phase in PHASES]
    if any(later < earlier for earlier, later in zip(values[:-1], values[1:], strict=True)):
        raise ValueError("phase anchors must be monotonic")
    phase_seconds = {
        f"{start}_to_{end}": end_at - start_at
        for start, end, start_at, end_at in zip(
            PHASES[:-1], PHASES[1:], values[:-1], values[1:], strict=True
        )
    }
    cursor = connection.execute(
        "INSERT INTO northstar_phase_baselines "
        "(run_id, baseline_key, total_seconds, phase_seconds_json, anchors_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run_id,
            baseline_key,
            values[-1] - values[0],
            json.dumps(phase_seconds, sort_keys=True, separators=(",", ":")),
            json.dumps(
                {phase: float(anchors[phase]) for phase in PHASES},
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    return _inserted_id(cursor)


def _allocation_dominates(left: AllocationMeasurement, right: AllocationMeasurement) -> bool:
    no_worse = (
        left.formation_selections <= right.formation_selections
        and left.swarm_attempts <= right.swarm_attempts
        and left.session_cost_usd <= right.session_cost_usd
        and left.completion_seconds <= right.completion_seconds
        and left.quality_score >= right.quality_score
    )
    strictly_better = (
        left.formation_selections < right.formation_selections
        or left.swarm_attempts < right.swarm_attempts
        or left.session_cost_usd < right.session_cost_usd
        or left.completion_seconds < right.completion_seconds
        or left.quality_score > right.quality_score
    )
    return no_worse and strictly_better


def record_allocation_frontier_report(
    connection: sqlite3.Connection,
    *,
    report_key: str,
    configurations: Sequence[AllocationMeasurement],
    created_at: str,
) -> int:
    """Persist the complete allocation frontier inputs and dominance decision."""
    if not report_key or not configurations:
        raise ValueError("report_key and at least one configuration are required")
    _timestamp(created_at)
    ids = [configuration.config_id for configuration in configurations]
    if len(ids) != len(set(ids)):
        raise ValueError("configuration ids must be unique")
    connection.execute("SAVEPOINT northstar_allocation_frontier")
    try:
        cursor = connection.execute(
            "INSERT INTO allocation_frontier_reports (report_key, created_at) VALUES (?, ?)",
            (report_key, created_at),
        )
        report_id = _inserted_id(cursor)
        for candidate in configurations:
            dominated = any(
                other.config_id != candidate.config_id and _allocation_dominates(other, candidate)
                for other in configurations
            )
            connection.execute(
                "INSERT INTO allocation_frontier_points "
                "(report_id, config_id, formation_selections, swarm_attempts, session_cost_usd, "
                "completion_seconds, quality_score, dominated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    candidate.config_id,
                    candidate.formation_selections,
                    candidate.swarm_attempts,
                    candidate.session_cost_usd,
                    candidate.completion_seconds,
                    candidate.quality_score,
                    int(dominated),
                ),
            )
        connection.execute("RELEASE SAVEPOINT northstar_allocation_frontier")
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT northstar_allocation_frontier")
        connection.execute("RELEASE SAVEPOINT northstar_allocation_frontier")
        raise
    return report_id


def record_tool_set_digest(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    tool_names: Sequence[str],
) -> str:
    """Write a stable digest of the exact tool-name set exposed to an attempt."""
    if not attempt_id:
        raise ValueError("attempt_id is required")
    normalized = sorted({name.strip() for name in tool_names if name.strip()})
    canonical = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    cursor = connection.execute(
        "UPDATE swarm_attempts SET tool_set_digest = ? WHERE id = ?",
        (digest, attempt_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"unknown attempt: {attempt_id}")
    return digest
