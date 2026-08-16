"""Enumerated types for the reliability system (§3).

Single source of truth for failure classes, severity levels, risk categorization,
root-cause categories, improvement/audit/governance state machines, and actor roles.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class FailureClass(StrEnum):
    """Categories of detected failures (detector scans DB only, touches no code)."""

    RUN_FAILED = "run_failed"
    SESSION_ERROR = "session_error"
    TOOL_ERROR = "tool_error"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    FINALIZE_QUARANTINE = "finalize_quarantine"
    RETRY_SPIKE = "retry_spike"
    STALE_RUN = "stale_run"
    COST_SPIKE = "cost_spike"
    LATENCY_REGRESSION = "latency_regression"
    QUALITY_DENY = "quality_deny"
    APPROVAL_EXPIRED = "approval_expired"
    ACCOUNT_DISABLED = "account_disabled"
    QUEUE_STALL = "queue_stall"
    ROUTINE_FAILURE = "routine_failure"
    OUTPUT_INVALID = "output_invalid"
    LOOP_DETECTED = "loop_detected"
    USER_REPORTED = "user_reported"
    DOC_STALE = "doc_stale"
    OTHER = "other"


class Severity(StrEnum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ChangeRisk(IntEnum):
    """Risk levels for improvements (1–4; deterministic classifier only raises, never lowers)."""

    L1 = 1  # prompt wording, retry timing, logging, error messages, formatting, non-governance docs
    L2 = 2  # model selection, fallback order, agent instructions, workflow sequencing, verification, new_agent
    L3 = 3  # everything else including all Tier-S code (detector/analyzer/pipeline/sandbox/recovery/scorecards/memory)
    L4 = 4  # critical: billing, payments, auth, security, permissions, data-deletion, frozen schema, Tier-P paths


class RootCauseCategory(StrEnum):
    """Root cause classification for improvement proposals."""

    TRANSIENT = "transient"
    CONFIG = "config"
    PROMPT = "prompt"
    MODEL = "model"
    CODE = "code"
    API = "api"
    ARCHITECTURAL = "architectural"


class ImprovementOrigin(StrEnum):
    """Where an improvement proposal was generated."""

    REALTIME = "realtime"  # watch-detected event
    AUDIT = "audit"  # batch audit sweep
    DEPARTMENT = "department"  # manager agent scoped review
    CTO = "cto"  # CTO quick pass or deep review
    WEEKLY = "weekly"  # weekly architecture review
    HUMAN = "human"  # the operator-requested via UI/API/CLI
    AGENT_REQUEST = "agent_request"  # from agent-request loop


class ImprovementKind(StrEnum):
    """Type of change proposed."""

    FIX = "fix"
    OPTIMIZATION = "optimization"
    ARCHITECTURE = "architecture"
    NEW_AGENT = "new_agent"
    SKILL = "skill"
    DOCS = "docs"
    CONFIG = "config"


class ImprovementStatus(StrEnum):
    """State machine for an improvement proposal through pipeline."""

    PROPOSED = "proposed"  # initial state
    TESTING = "testing"  # sandbox running
    JUDGING = "judging"  # judges deliberating
    PANEL_BLOCKED = "panel_blocked"  # not enough distinct families seated
    AWAITING_HUMAN = "awaiting_human"  # needs human decision
    APPROVED = "approved"  # human/quorum said yes
    APPLYING = "applying"  # rolling out
    APPLIED = "applied"  # commit landed
    MONITORING = "monitoring"  # observation window
    CONFIRMED = "confirmed"  # KPIs green, improvement held
    REJECTED = "rejected"  # human said no
    ROLLING_BACK = "rolling_back"  # auto-rollback in flight
    ROLLED_BACK = "rolled_back"  # reverted due to regression
    FAILED = "failed"  # crashed during apply/rollback
    SUPERSEDED = "superseded"  # newer improvement replaces this


class AuditKind(StrEnum):
    """Types of reliability audits."""

    WATCH = "watch"  # incremental 10-min scan
    TWICE_DAILY = "twice_daily"  # full sweep 06:30 + 18:30
    DAILY_SUMMARY = "daily_summary"  # consolidated summary
    WEEKLY_ARCHITECTURE = "weekly_architecture"  # deep CTO review
    ON_DEMAND = "on_demand"  # human-triggered


class AuditStatus(StrEnum):
    """Audit lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OrgUnitKind(StrEnum):
    """Organization hierarchy."""

    COMPANY = "company"
    DEPARTMENT = "department"
    TEAM = "team"


class OrgRole(StrEnum):
    """Agent roles in the organization."""

    CTO = "cto"
    VP = "vp"
    MANAGER = "manager"
    SPECIALIST = "specialist"
    JUDGE = "judge"


class AutonomyMode(StrEnum):
    """Autonomy setting for an improvement scope."""

    APPROVE = "approve"  # queue for human decision
    AUTO = "auto"  # auto-apply if conditions met


class VerdictKind(StrEnum):
    """Judge panel verdict on an improvement."""

    APPROVE = "approve"
    REJECT = "reject"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    NEEDS_HUMAN = "needs_human"


class EventStatus(StrEnum):
    """Reliability event lifecycle."""

    OPEN = "open"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    PROPOSED = "proposed"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class AgentRequestStatus(StrEnum):
    """Agent request lifecycle (from design §7)."""

    PENDING = "pending"
    DESIGNING = "designing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CREATED = "created"
    REJECTED = "rejected"
    FAILED = "failed"
