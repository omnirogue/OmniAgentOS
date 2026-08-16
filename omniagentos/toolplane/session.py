"""Toolplane attachment to the real sanctioned session tool path.

The standalone ``omniagentos-tool`` CLI is only one end of the sanctioned path.
The other end — the one every Claude session actually traverses — is the
PreToolUse hook (``POST /sessions/hook-eval``), which gates each tool call
before it runs. This module is what that gate calls so that *every* allow /
deny / error outcome on the real path produces a scrubbed durable observation
(M-08, M-17).

Two honest limits are encoded here rather than papered over:

1. Not every session tool has a toolplane equivalent. ``Bash``, ``WebFetch``,
   ``Task`` and friends run natively. Denying them because they are not
   registered capabilities would deny the entire native toolset, so instead
   each observation records ``coverage`` — ``"toolplane"`` when the call maps
   onto a registered capability, ``"bypass"`` when it does not. The bypass
   inventory is then derived from real traffic, not from a guess.

2. Full *execution* interposition (routing the session's tool call through
   ``toolplane.dispatch``) is deferred by the L17 packet until the L08 session
   lifecycle boundary lands. This module observes the real path today and
   supplies the capability mapping that interposition will need.

Observations built here are metadata-only: never tool arguments, command text,
file contents, cwd, or approval reason strings.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from .observe import OBSERVATIONS_SUBDIR, OBSERVE_VERSION, emit_observation, get_sink
from .scrub import scrub_value
from .tools import TOOLS

SESSION_SOURCE = "session-hook"

# Session tool names that map onto a registered toolplane capability.
# Anything absent from this map is recorded as coverage="bypass".
SESSION_TOOL_CAPABILITY: dict[str, str] = {
    "Read": "read_file",
    "Glob": "list_files",
    "Grep": "search_files",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "NotebookEdit": "edit_file",
}

COVERAGE_TOOLPLANE = "toolplane"
COVERAGE_BYPASS = "bypass"

# Gate decision -> observation status.
_DECISION_STATUS = {
    "allow": "allowed",
    "deny": "denied",
    "error": "failed",
}

# Reason-prefix -> short denial code. Only the code is recorded; the raw reason
# string never reaches the observation, so approval ids and paths cannot leak.
_DENIAL_CODES: tuple[tuple[str, str], ...] = (
    ("unauthorized", "unauthorized"),
    ("unknown-session", "unknown_session"),
    ("parked:", "approval_required"),
    ("parked per finance-only policy", "approval_required"),
    # C1 (2026-08-04): the narrow NON-finance park-list (production deploys,
    # remote destructive commands). It is a park like any other, so it maps to the
    # same code -- but it needs its own prefix here, or a real approval-required
    # park would be recorded under the generic "denied" fallback.
    ("parked per non-finance park-list", "approval_required"),
    ("refused per finance-only policy", "orchestrator_refused"),
    ("auto-approved per finance-only policy", "orchestrator_auto_approved"),
    ("orchestrator escalated", "orchestrator_escalated"),
    ("orchestrator auto-approved", "orchestrator_auto_approved"),
    ("api-error", "api_error"),
    ("Bash approval requires", "missing_command"),
    # PKG-INSESSION-FANOUT (2026-07-27) shipped seven deny reasons on the Task gate and none
    # of them had a prefix here, so every one reduced to the generic "denied" -- including the
    # `except Exception` path, which is a CRASHED gate rather than a refusal. Three codes,
    # because three things are operationally different: the flag is dark, the grant said no,
    # or the authority that answers the question failed to run.
    #
    # ORDER IS LOAD-BEARING: this table is scanned first-match-wins, so the specific
    # "task-fanout:task-gate-error" must stay ABOVE the "task-fanout:" catch-all or a crash
    # silently becomes an ordinary denial again. That constraint is asserted, not just
    # written down -- see tests/toolplane/test_denial_code_covers_its_producers.py.
    ("task-fanout-disabled", "fanout_disabled"),
    ("task-fanout:task-gate-error", "fanout_gate_error"),
    # Catch-all for `consume_child_slot`'s closed set of refusals (no_grant, grant_voided,
    # grant_expired, budget_exhausted, attempt_ended). A `why` added later lands here rather
    # than in the global generic bucket, which is the drift this whole entry exists to stop.
    ("task-fanout:", "fanout_denied"),
)


def capability_for_session_tool(tool_name: str) -> str | None:
    """Return the registered toolplane capability for a session tool, if any."""
    capability = SESSION_TOOL_CAPABILITY.get(tool_name)
    return capability if capability in TOOLS else None


def coverage_for_session_tool(tool_name: str) -> str:
    """Classify a session tool as toolplane-covered or a toolplane bypass."""
    return COVERAGE_TOOLPLANE if capability_for_session_tool(tool_name) else COVERAGE_BYPASS


def denial_code(reason: str | None) -> str | None:
    """Reduce a gate reason string to a short, leak-free denial code."""
    if not reason:
        return None
    for prefix, code in _DENIAL_CODES:
        if reason.startswith(prefix):
            return code
    return "denied"


def observe_session_tool_call(
    *,
    tool_name: str,
    session_id: str,
    decision: str,
    duration_ms: int,
    reason: str | None = None,
    eval_kind: str | None = None,
    run_id: str | None = None,
    holder_generation: int | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build a scrubbed observation for one gate decision on the real session path.

    Args:
        tool_name: The session tool being gated (e.g. "Bash", "Write").
        session_id: The session reference the call belongs to.
        decision: Gate outcome — "allow", "deny", or "error".
        duration_ms: Gate evaluation duration in milliseconds.
        reason: Gate reason string; reduced to a code, never recorded verbatim.
        eval_kind: The hook eval kind, when the caller supplies one.
        run_id: Correlating run, when known.
        holder_generation: L08 lifecycle generation, when known.
        correlation_id: Deduplication id; generated per call when omitted.

    Returns:
        A scrubbed, metadata-only observation record.
    """
    status = _DECISION_STATUS.get(decision, "failed")
    error = denial_code(reason) if status not in ("allowed", "success") else None

    observation = {
        "version": OBSERVE_VERSION,
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool_name,
        "run_id": run_id,
        "session_id": session_id,
        "holder_generation": holder_generation,
        # Per-call id: two identical tool calls are distinct events and must not
        # deduplicate each other. Idempotency covers retrying one emit, not calls.
        "correlation_id": correlation_id or uuid.uuid4().hex[:16],
        "status": status,
        "ok": status in ("allowed", "success"),
        "error": error,
        "duration_ms": duration_ms,
        "source": SESSION_SOURCE,
        "decision": decision,
        "coverage": coverage_for_session_tool(tool_name),
        "capability": capability_for_session_tool(tool_name),
        "eval_kind": eval_kind,
    }
    scrubbed = scrub_value(observation)
    return scrubbed if isinstance(scrubbed, dict) else observation


def emit_session_tool_call(**kwargs: Any) -> bool:
    """Build and durably emit one session-path observation.

    Returns:
        True if durably recorded, False if spooled for retry. Never raises: an
        observation failure must not change a gate decision.
    """
    try:
        recorded = emit_observation(observe_session_tool_call(**kwargs))
        try:
            get_sink().retry_pending()
        except Exception:  # noqa: BLE001 - retry is best-effort
            pass
        return recorded
    except Exception:  # noqa: BLE001 - observation must never break the gate
        return False


def bypass_inventory(ledger_dir: str | None = None) -> dict[str, Any]:
    """Derive the toolplane bypass inventory from real recorded session traffic.

    Reads emitted session-hook observations and reports, per tool, how many
    calls were seen and whether they were toolplane-covered or bypassed. This is
    computed from observed calls rather than from a static list, so a tool that
    appears on the real path without a registered capability shows up here.

    Args:
        ledger_dir: Observation ledger root. Defaults to OMNIAGENTOS_LEDGER_DIR.

    Returns:
        {"tools": {name: {...}}, "covered": int, "bypassed": int, "observed": int}
    """
    if ledger_dir is None:
        ledger_dir = os.environ.get("OMNIAGENTOS_LEDGER_DIR", "var/ledger")
    obs_dir = os.path.join(ledger_dir, OBSERVATIONS_SUBDIR)

    tools: dict[str, dict[str, Any]] = {}
    observed = 0
    try:
        names = sorted(os.listdir(obs_dir))
    except OSError:
        names = []

    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(obs_dir, name), encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("source") != SESSION_SOURCE:
            continue

        tool_name = str(record.get("tool") or "")
        if not tool_name:
            continue
        observed += 1
        entry = tools.setdefault(
            tool_name,
            {
                "calls": 0,
                "coverage": record.get("coverage") or coverage_for_session_tool(tool_name),
                "capability": record.get("capability"),
                "statuses": {},
            },
        )
        entry["calls"] += 1
        status = str(record.get("status") or "unknown")
        entry["statuses"][status] = entry["statuses"].get(status, 0) + 1

    covered = sum(1 for e in tools.values() if e["coverage"] == COVERAGE_TOOLPLANE)
    return {
        "tools": tools,
        "covered": covered,
        "bypassed": len(tools) - covered,
        "observed": observed,
    }


__all__ = [
    "COVERAGE_BYPASS",
    "COVERAGE_TOOLPLANE",
    "SESSION_SOURCE",
    "SESSION_TOOL_CAPABILITY",
    "bypass_inventory",
    "capability_for_session_tool",
    "coverage_for_session_tool",
    "denial_code",
    "emit_session_tool_call",
    "observe_session_tool_call",
]
