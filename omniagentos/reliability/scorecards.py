"""Pure KPI aggregation and scorecard persistence."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityStore


def aggregate_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    successes = sum(
        str(r.get("state", r.get("status", ""))).lower() in {"completed", "success", "succeeded"}
        for r in rows
    )
    failures = sum(
        str(r.get("state", r.get("status", ""))).lower() in {"failed", "error", "failure"}
        for r in rows
    )
    retries = sum(max(0, int(r.get("attempt", r.get("retries", 0)) or 0) - 1) for r in rows)
    latencies = [
        float(r[k]) / (1000 if k == "wall_ms" else 1)
        for r in rows
        for k in ("wall_ms", "latency_s")
        if r.get(k) is not None
    ]
    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    tokens = [
        int(r[k] or 0)
        for r in rows
        for k in ("tokens", "input_tokens", "output_tokens")
        if r.get(k) is not None
    ]
    votes = [str(r.get("verdict", "")) for r in rows if r.get("verdict")]
    return {
        "run_count": len(rows),
        "success_rate": successes / len(rows) if rows else 0.0,
        "failure_count": failures,
        "retry_count": retries,
        "median_latency_s": statistics.median(latencies) if latencies else 0.0,
        "mean_cost_usd": statistics.mean(costs) if costs else 0.0,
        "token_usage": sum(tokens),
        "judge_approval_rate": sum(v in {"approve", "approve_with_conditions"} for v in votes)
        / len(votes)
        if votes
        else 0.0,
    }


def compute_scorecard(
    store: ReliabilityStore,
    subject_type: str,
    subject_id: str,
    window: str,
    period_start: str,
    rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    metrics = aggregate_metrics(rows)
    scorecard_id = store.upsert_scorecard(subject_type, subject_id, window, period_start, metrics)
    return {
        "id": scorecard_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "window": window,
        "period_start": period_start,
        "metrics_json": metrics,
        "computed_at": utc_now_iso(),
    }


aggregate = aggregate_metrics
