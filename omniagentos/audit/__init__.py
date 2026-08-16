"""Audit event persistence helper + trace-audit re-exports."""

from __future__ import annotations

from typing import Any

from omniagentos.audit.trace import (
    RULES,
    AuditReport,
    CategoryResult,
    Expectations,
    TraceGap,
    TraceSegment,
    Verdict,
    rule_boundaries,
    rule_context,
    rule_cost_latency,
    rule_evidence,
    rule_prompt_injection,
    rule_retries,
    rule_routing,
    rule_timeouts,
    rule_tools,
    rule_verification,
)
from omniagentos.audit.trace import (
    audit as audit_trace,
)
from omniagentos.contracts import Events, Store


def audit(
    store: Store,
    actor: str,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
    trace_id: str = "",
) -> int:
    """Insert one audit event and return its row ID."""
    return store.insert_event(
        type=Events.AUDIT,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        trace_id=trace_id,
    )


__all__ = [
    "audit",
    "audit_trace",
    "RULES",
    "AuditReport",
    "CategoryResult",
    "Expectations",
    "TraceGap",
    "TraceSegment",
    "Verdict",
    "rule_boundaries",
    "rule_context",
    "rule_cost_latency",
    "rule_evidence",
    "rule_prompt_injection",
    "rule_retries",
    "rule_routing",
    "rule_timeouts",
    "rule_tools",
    "rule_verification",
]
