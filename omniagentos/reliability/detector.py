"""Failure detection rules for OmniAgentOS V2 (§3, §5b.5).

Pure functions reading DB only (zero mutations). Scans runs, steps, sessions, approvals,
events, claude_accounts, routine_runs for failure signals. Returns FailureCandidate list
per rule. Dedup via signature + occurrence_key (ON CONFLICT IGNORE idempotency).

Cursor semantics: captures high-water mark, scans only rows > cursor, persists events
idempotently, advances cursor ONLY after all rows durably written (§5b.5).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any, cast

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.taxonomy import FailureClass, Severity
from omniagentos.steward.quoting import quote_untrusted


@dataclass(frozen=True, slots=True)
class FailureCandidate:
    """A pure detector rule result; minimal data for dedup and event creation."""

    failure_class: str
    severity: str
    signature: str
    occurrence_key: str
    source: str
    ref_type: str | None = None
    ref_id: str | None = None
    evidence_json: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.evidence_json is None:
            object.__setattr__(self, "evidence_json", {})


def _make_signature(failure_class: str, source: str, normalized_error: str) -> str:
    """Dedup fingerprint: sha1(class|source|error)[:16]."""
    data = f"{failure_class}|{source}|{normalized_error}"
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def _normalized_error(error_text: str | None) -> str:
    """Normalize error text for signature: lowercase, strip whitespace, truncate."""
    if not error_text:
        return "none"
    normalized = error_text.lower().strip()[:100]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


# --- Rule implementations (§3 failure classes)


def rule_run_failed(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """runs.state='failed' → severity warning."""
    candidates = []
    # Filter on the timestamp that marks the FAILURE (finished_at), not created_at —
    # a run created long before the window can still fail inside it. Fall back to
    # updated_at when finished_at was never stamped (§5b.5).
    rows = conn.execute(
        """
        SELECT id, error, task_id, created_at FROM runs
        WHERE state = 'failed' AND COALESCE(finished_at, updated_at) > ?
        ORDER BY COALESCE(finished_at, updated_at)
        """,
        (cursor,),
    ).fetchall()

    for row in rows:
        error_text = row["error"] or "unknown failure"
        sig = _make_signature(
            FailureClass.RUN_FAILED.value,
            "detector_run_failed",
            _normalized_error(error_text),
        )
        occ_key = f"{sig}|{row['id']}|{row['created_at'][:10]}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.RUN_FAILED.value,
                severity=Severity.WARNING.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_run_failed",
                ref_type="run",
                ref_id=row["id"],
                evidence_json={
                    "error": quote_untrusted(error_text, source="run.error", max_chars=500),
                    "task_id": row["task_id"],
                },
            )
        )
    return candidates


def rule_session_error(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """sessions.state='failed' AND sessions.error NOT NULL → severity critical."""
    candidates = []
    # sessions carry no finished_at; updated_at is stamped on the failure transition.
    rows = conn.execute(
        """
        SELECT id, error, created_at FROM sessions
        WHERE state = 'failed' AND error IS NOT NULL AND updated_at > ?
        ORDER BY updated_at
        """,
        (cursor,),
    ).fetchall()

    for row in rows:
        error_text = row["error"] or "unknown session error"
        sig = _make_signature(
            FailureClass.SESSION_ERROR.value,
            "detector_session_error",
            _normalized_error(error_text),
        )
        occ_key = f"{sig}|{row['id']}|{row['created_at'][:10]}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.SESSION_ERROR.value,
                severity=Severity.CRITICAL.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_session_error",
                ref_type="session",
                ref_id=row["id"],
                evidence_json={
                    "error": quote_untrusted(error_text, source="session.error", max_chars=500),
                },
            )
        )
    return candidates


def rule_tool_error(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """steps.error containing tool_name → severity warning."""
    candidates = []
    rows = conn.execute(
        """
        SELECT s.run_id, s.seq, s.error, s.finished_at FROM steps s
        WHERE s.error IS NOT NULL
          AND (s.error LIKE '%tool%' OR s.error LIKE '%MCP%' OR s.error LIKE '%bash%')
          AND s.finished_at > ?
        ORDER BY s.finished_at
        """,
        (cursor,),
    ).fetchall()

    for row in rows:
        error_text = row["error"] or "unknown tool error"
        sig = _make_signature(
            FailureClass.TOOL_ERROR.value,
            "detector_tool_error",
            _normalized_error(error_text),
        )
        occ_key = f"{sig}|{row['run_id']}|{row['seq']}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.TOOL_ERROR.value,
                severity=Severity.WARNING.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_tool_error",
                ref_type="run",
                ref_id=row["run_id"],
                evidence_json={
                    "error": quote_untrusted(error_text, source="step.error", max_chars=500),
                    "step_seq": row["seq"],
                },
            )
        )
    return candidates


def rule_auth_failure(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """runs.error OR sessions.error matching auth pattern → severity critical."""
    candidates = []
    auth_keywords = ["auth", "token", "permission", "oauth", "401", "403", "unauthorized"]

    # Check runs
    runs = conn.execute(
        """
        SELECT id, error, created_at FROM runs
        WHERE error IS NOT NULL AND COALESCE(finished_at, updated_at) > ?
        ORDER BY COALESCE(finished_at, updated_at)
        """,
        (cursor,),
    ).fetchall()

    for row in runs:
        error_text = (row["error"] or "").lower()
        if any(kw in error_text for kw in auth_keywords):
            sig = _make_signature(
                FailureClass.AUTH_FAILURE.value,
                "detector_auth_failure_run",
                _normalized_error(row["error"]),
            )
            occ_key = f"{sig}|{row['id']}|run"
            candidates.append(
                FailureCandidate(
                    failure_class=FailureClass.AUTH_FAILURE.value,
                    severity=Severity.CRITICAL.value,
                    signature=sig,
                    occurrence_key=occ_key,
                    source="detector_auth_failure_run",
                    ref_type="run",
                    ref_id=row["id"],
                    evidence_json={
                        "error": quote_untrusted(row["error"], source="run.error", max_chars=500),
                        "matched_auth_keyword": True,
                    },
                )
            )

    # Check sessions
    sessions = conn.execute(
        """
        SELECT id, error, created_at FROM sessions
        WHERE error IS NOT NULL AND updated_at > ?
        ORDER BY updated_at
        """,
        (cursor,),
    ).fetchall()

    for row in sessions:
        error_text = (row["error"] or "").lower()
        if any(kw in error_text for kw in auth_keywords):
            sig = _make_signature(
                FailureClass.AUTH_FAILURE.value,
                "detector_auth_failure_session",
                _normalized_error(row["error"]),
            )
            occ_key = f"{sig}|{row['id']}|session"
            candidates.append(
                FailureCandidate(
                    failure_class=FailureClass.AUTH_FAILURE.value,
                    severity=Severity.CRITICAL.value,
                    signature=sig,
                    occurrence_key=occ_key,
                    source="detector_auth_failure_session",
                    ref_type="session",
                    ref_id=row["id"],
                    evidence_json={
                        "error": quote_untrusted(
                            row["error"], source="session.error", max_chars=500
                        ),
                        "matched_auth_keyword": True,
                    },
                )
            )
    return candidates


def rule_rate_limit(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """error text matching rate_limit pattern → severity warning."""
    candidates = []
    keywords = ["rate_limit", "429", "quota", "rate exceeded"]

    runs = conn.execute(
        """
        SELECT id, error, created_at FROM runs
        WHERE error IS NOT NULL AND COALESCE(finished_at, updated_at) > ?
        ORDER BY COALESCE(finished_at, updated_at)
        """,
        (cursor,),
    ).fetchall()

    for row in runs:
        error_text = (row["error"] or "").lower()
        if any(kw in error_text for kw in keywords):
            sig = _make_signature(
                FailureClass.RATE_LIMIT.value,
                "detector_rate_limit",
                _normalized_error(row["error"]),
            )
            occ_key = f"{sig}|{row['id']}|rate"
            candidates.append(
                FailureCandidate(
                    failure_class=FailureClass.RATE_LIMIT.value,
                    severity=Severity.WARNING.value,
                    signature=sig,
                    occurrence_key=occ_key,
                    source="detector_rate_limit",
                    ref_type="run",
                    ref_id=row["id"],
                    evidence_json={
                        "error": quote_untrusted(row["error"], source="run.error", max_chars=500),
                    },
                )
            )
    return candidates


def rule_timeout(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """error text matching timeout pattern → severity warning."""
    candidates = []
    keywords = ["timeout", "deadline", "took too long", "timed out"]

    runs = conn.execute(
        """
        SELECT id, error, created_at FROM runs
        WHERE error IS NOT NULL AND COALESCE(finished_at, updated_at) > ?
        ORDER BY COALESCE(finished_at, updated_at)
        """,
        (cursor,),
    ).fetchall()

    for row in runs:
        error_text = (row["error"] or "").lower()
        if any(kw in error_text for kw in keywords):
            sig = _make_signature(
                FailureClass.TIMEOUT.value,
                "detector_timeout",
                _normalized_error(row["error"]),
            )
            occ_key = f"{sig}|{row['id']}|timeout"
            candidates.append(
                FailureCandidate(
                    failure_class=FailureClass.TIMEOUT.value,
                    severity=Severity.WARNING.value,
                    signature=sig,
                    occurrence_key=occ_key,
                    source="detector_timeout",
                    ref_type="run",
                    ref_id=row["id"],
                    evidence_json={
                        "error": quote_untrusted(row["error"], source="run.error", max_chars=500),
                    },
                )
            )
    return candidates


def rule_approval_expired(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """approvals.state='pending' AND expires_at < now() → severity info."""
    candidates = []
    now = utc_now_iso()
    rows = conn.execute(
        """
        SELECT id, created_at FROM approvals
        WHERE state = 'pending' AND expires_at IS NOT NULL AND expires_at < ?
        ORDER BY created_at
        """,
        (now,),
    ).fetchall()

    for row in rows:
        sig = _make_signature(
            FailureClass.APPROVAL_EXPIRED.value,
            "detector_approval_expired",
            "expired",
        )
        occ_key = f"{sig}|{row['id']}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.APPROVAL_EXPIRED.value,
                severity=Severity.INFO.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_approval_expired",
                ref_type="approval",
                ref_id=row["id"],
                evidence_json={"expired_at": now},
            )
        )
    return candidates


def rule_account_disabled(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """claude_accounts.enabled=0 → severity critical (one per account)."""
    candidates = []
    rows = conn.execute(
        """
        SELECT id, label, updated_at FROM claude_accounts
        WHERE enabled = 0 AND updated_at > ?
        ORDER BY updated_at
        """,
        (cursor,),
    ).fetchall()

    for row in rows:
        sig = _make_signature(
            FailureClass.ACCOUNT_DISABLED.value,
            "detector_account_disabled",
            row["id"],
        )
        occ_key = f"{sig}|{row['id']}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.ACCOUNT_DISABLED.value,
                severity=Severity.CRITICAL.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_account_disabled",
                ref_type="account",
                ref_id=row["id"],
                evidence_json={"label": row["label"]},
            )
        )
    return candidates


def rule_queue_stall(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """runs in queued state > 5 min no worker claim → severity warning."""
    candidates = []
    cutoff = None
    # Calculate 5 minutes ago as a simple ISO offset
    import datetime

    cutoff_dt = datetime.datetime.fromisoformat(cursor.replace("Z", "+00:00")) - datetime.timedelta(
        minutes=5
    )
    cutoff = cutoff_dt.isoformat().replace("+00:00", "Z")

    rows = conn.execute(
        """
        SELECT id, task_id, queued_at FROM runs
        WHERE state = 'queued' AND worker_id IS NULL AND queued_at < ?
        ORDER BY queued_at
        """,
        (cutoff,),
    ).fetchall()

    for row in rows:
        sig = _make_signature(
            FailureClass.QUEUE_STALL.value,
            "detector_queue_stall",
            row["id"],
        )
        occ_key = f"{sig}|{row['id']}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.QUEUE_STALL.value,
                severity=Severity.WARNING.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_queue_stall",
                ref_type="run",
                ref_id=row["id"],
                evidence_json={"queued_at": row["queued_at"]},
            )
        )
    return candidates


def rule_routine_failure(conn: sqlite3.Connection, cursor: str) -> list[FailureCandidate]:
    """routine_runs.gate_passed=0 AND accepted=0 in window → severity warning."""
    candidates = []
    rows = conn.execute(
        """
        SELECT rr.id, rr.routine_id, rr.created_at FROM routine_runs rr
        WHERE rr.gate_passed = 0 AND rr.accepted = 0 AND rr.created_at > ?
        ORDER BY rr.created_at
        """,
        (cursor,),
    ).fetchall()

    for row in rows:
        sig = _make_signature(
            FailureClass.ROUTINE_FAILURE.value,
            "detector_routine_failure",
            row["routine_id"],
        )
        occ_key = f"{sig}|{row['routine_id']}|{row['created_at'][:10]}"
        candidates.append(
            FailureCandidate(
                failure_class=FailureClass.ROUTINE_FAILURE.value,
                severity=Severity.WARNING.value,
                signature=sig,
                occurrence_key=occ_key,
                source="detector_routine_failure",
                ref_type="routine",
                ref_id=row["routine_id"],
                evidence_json={"routine_run_id": row["id"]},
            )
        )
    return candidates


# Registry of all detector rules
_DETECTOR_RULES = [
    rule_run_failed,
    rule_session_error,
    rule_tool_error,
    rule_auth_failure,
    rule_rate_limit,
    rule_timeout,
    rule_approval_expired,
    rule_account_disabled,
    rule_queue_stall,
    rule_routine_failure,
]


def detect_failures(
    store: ReliabilityStore,
    window_start: str,
    window_end: str,
    *,
    errors: list[dict[str, str]] | None = None,
) -> list[FailureCandidate]:
    """Run all detector rules over the time window.

    Uses store's underlying connection to scan DB read-only. Returns candidates
    for event insertion (caller handles ON CONFLICT IGNORE idempotency).

    Args:
        store: ReliabilityStore with DB connection
        window_start: cursor (typically watch high-water mark from last run)
        window_end: end of window (typically now); unused for cursor-based scan
        errors: optional sink list; a failing rule appends
            ``{"rule": name, "error": str}`` here so the caller can HOLD the watch
            cursor rather than silently skip whatever that rule would have found
            (§5b.5 — nothing silently skipped). One failing rule never blocks the
            others.

    Returns:
        List of FailureCandidate for store.insert_reliability_event()
    """
    all_candidates = []

    # Use store's connection directly (read-only). The Protocol does not expose
    # the private connection; concrete stores (SqliteReliabilityStore) do.
    conn: sqlite3.Connection = cast(Any, store)._connection

    for rule in _DETECTOR_RULES:
        try:
            candidates = rule(conn, window_start)
            all_candidates.extend(candidates)
        except Exception as exc:  # noqa: BLE001 - one bad rule never blocks the rest
            if errors is not None:
                errors.append({"rule": getattr(rule, "__name__", "rule"), "error": str(exc)})

    return all_candidates
