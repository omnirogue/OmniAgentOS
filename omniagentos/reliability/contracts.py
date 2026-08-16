"""Frozen store contract for the reliability system (W1 only).

Packages W2–W7 import from here and implement the concrete ReliabilityStore against
the Protocol. NEVER edit this file — needed changes are A2A questions to the lead.

All durability and concurrency contracts per §5b (codex review):
  1. CAS transitions via transition_improvement (atomic UPDATE + log append)
  2. Occurrence key idempotency (ON CONFLICT IGNORE)
  3. Lease-based mutual exclusion (not lock) for apply/rollback
  4. SQLITE_BUSY retry policy (exponential backoff + jitter)
  5. Cursor semantics for watch window
  6. Hash-chained append-only reliability_log (no store UPDATE/DELETE methods)
  7. Panel attempt isolation for vote idempotency

§5b.1 – CAS transitions: every status change is atomic (UPDATE WHERE status=? AND version=?,
increment version, append log row) or raise TransitionConflict.

§5b.2 – Apply journal: multi-phase state persisted in reliability_state with rollback.

§5b.3 – Lease + fencing token: mutual exclusion via lease row (owner, token, expires_at)
held ONLY during apply/rollback; overlap check atomic with acquisition.

§5b.4 – SQLITE_BUSY: every write retries whole transaction with bounded exponential backoff.

§5b.5 – Cursor: watch captures high-water mark, scans only up to it, persists events idempotently,
advances cursor ONLY after all rows durably written.

§5b.6 – Hard deadlines: subprocess group kill on deadline, stage times persisted, stale stages
reclaimed with bounded retries.

§5b.7 – Panel attempts: each panel invocation gets panel_attempt_id; complete attempt = 3 family-distinct
votes; <3 families ⇒ status panel_blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Exceptions


class TransitionConflict(Exception):
    """CAS transition failed: expected status/version mismatch on UPDATE."""

    pass


class LeaseConflict(Exception):
    """Could not acquire lease: already held by another owner."""

    pass


# Data models (read-only contracts; stored as JSON in DB)


@dataclass
class ReliabilityEvent:
    """A detected failure or state change (mapped to reliability_events row)."""

    id: str
    failure_class: str
    severity: str  # 'info' | 'warning' | 'critical'
    signature: str
    occurrence_key: str
    source: str
    ref_type: str | None = None
    ref_id: str | None = None
    evidence_json: dict[str, Any] = field(default_factory=dict)
    status: str = "open"
    recovery_json: dict[str, Any] = field(default_factory=dict)
    improvement_id: str | None = None
    audit_id: str | None = None
    detected_at: str = ""
    updated_at: str = ""
    # Last time this event raised an operator alert. NULL = never alerted. The
    # watch alerts once per event rather than once per cycle (migration 053).
    alerted_at: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_json is None:
            self.evidence_json = {}
        if self.recovery_json is None:
            self.recovery_json = {}


@dataclass
class Improvement:
    """A proposed or applied change to fix/optimize the system."""

    id: str
    origin: str
    kind: str
    title: str
    summary: str = ""
    root_cause: str = ""
    proposal_json: dict[str, Any] = field(default_factory=dict)
    risk_level: int = 2
    status: str = "proposed"
    version: int = 0
    stage_started_at: str | None = None
    stage_deadline: str | None = None
    attempt: int = 0
    last_error_json: dict[str, Any] = field(default_factory=dict)
    ranking_score: float = 0.0
    sandbox_json: dict[str, Any] = field(default_factory=dict)
    votes_summary_json: dict[str, Any] = field(default_factory=dict)
    rollback_point_id: str | None = None
    applied_task_id: str | None = None
    applied_sha: str | None = None
    monitor_until: str | None = None
    memory_refs_json: list[str] = field(default_factory=list)
    decided_by: str | None = None
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""
    applied_at: str | None = None
    resolved_at: str | None = None

    def __post_init__(self) -> None:
        if self.proposal_json is None:
            self.proposal_json = {}
        if self.last_error_json is None:
            self.last_error_json = {}
        if self.sandbox_json is None:
            self.sandbox_json = {}
        if self.votes_summary_json is None:
            self.votes_summary_json = {}
        if self.memory_refs_json is None:
            self.memory_refs_json = []


@dataclass
class ImprovementVote:
    """A single judge's verdict on an improvement (mapped to improvement_votes row)."""

    id: str
    improvement_id: str
    panel_attempt_id: str
    judge_agent: str
    model_family: str
    model: str = ""
    verdict: str = "approve"
    scores_json: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    conditions: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.scores_json is None:
            self.scores_json = {}


@dataclass
class ReliabilityAudit:
    """An audit sweep result (mapped to reliability_audits row)."""

    id: str
    kind: str
    status: str = "queued"
    window_start: str = ""
    window_end: str = ""
    stats_json: dict[str, Any] = field(default_factory=dict)
    findings: int = 0
    report_note_path: str | None = None
    started_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if self.stats_json is None:
            self.stats_json = {}


@dataclass
class Scorecard:
    """KPI metrics for an entity over a time window (mapped to scorecards row)."""

    id: str
    subject_type: str  # 'agent' | 'model' | 'harness' | 'department' | 'skill' | 'routine'
    subject_id: str
    window: str  # 'day' | 'week'
    period_start: str
    metrics_json: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""

    def __post_init__(self) -> None:
        if self.metrics_json is None:
            self.metrics_json = {}


@dataclass
class OrgUnit:
    """Organization hierarchy node (mapped to org_units row)."""

    id: str
    name: str
    kind: str  # 'company' | 'department' | 'team'
    parent_id: str | None = None
    charter: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Agent:
    """Agent with org placement (agents table + new W1 columns)."""

    id: str
    name: str
    model: str | None = None
    org_unit_id: str | None = None
    org_role: str = "specialist"
    title: str = ""
    charter: str = ""
    schedule_json: dict[str, Any] = field(default_factory=dict)
    harness: str | None = None
    enabled: int = 1
    vault_note_path: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.schedule_json is None:
            self.schedule_json = {}


@dataclass
class AgentRequest:
    """Request to create a new agent (mapped to agent_requests row).

    from_agent_id stores the actual authenticated/emitting principal in canonical form:
    lane:*, loop:*, job:*, human:*, or system (for missing auth or internal events).
    Non-canonical spellings like agent:* are rejected at the write path (U-E7).
    """

    id: str
    description: str
    #: Display attribution. Derived from ``from_agent_id`` at the write path —
    #: never supplied by a caller, and never defaulted to an operator's name.
    requested_by: str = "system"
    status: str = "pending"
    design_json: dict[str, Any] = field(default_factory=dict)
    improvement_id: str | None = None
    agent_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    from_agent_id: str = "system"
    from_lane: str | None = None
    session_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.design_json is None:
            self.design_json = {}


@dataclass
class AutonomySetting:
    """Scope-level autonomy mode + risk threshold (mapped to autonomy_settings row)."""

    id: str
    scope_type: str  # 'global' | 'department' | 'agent' | 'kind'
    scope_id: str = ""
    mode: str = "approve"
    max_auto_risk: int = 0
    updated_by: str = "system"
    updated_at: str = ""


# Store Protocol (frozen contract)


@runtime_checkable
class ReliabilityStore(Protocol):
    """Data access layer for the reliability system.

    All methods operate on a single SQLite connection with BEGIN IMMEDIATE + retry.
    Append-only: no UPDATE/DELETE methods for reliability_log, improvement_votes, reliability_audits.
    """

    # --- Event lifecycle (watch detector and recovery)

    def insert_reliability_event(
        self,
        failure_class: str,
        severity: str,
        signature: str,
        occurrence_key: str,
        source: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        evidence_json: dict[str, Any] | None = None,
    ) -> str:
        """Insert a detected failure event into ``reliability_events``.

        Named distinctly from the base ``Store.insert_event`` SSE/event-log
        method so dual-purpose concrete stores never route by call shape.

        occurrence_key UNIQUE constraint enforces idempotency: overlapping watch runs
        cannot double-insert the same occurrence (§5b.5).

        Returns event id.
        Raises: sqlite3.IntegrityError if occurrence_key conflict (caller uses ON CONFLICT IGNORE).
        """
        ...

    def get_event(self, event_id: str) -> ReliabilityEvent | None:
        """Fetch an event by id."""
        ...

    def list_events(
        self,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityEvent]:
        """List events, optionally filtered."""
        ...

    def claim_recovery(self, event_id: str) -> bool:
        """CAS: open → recovering (§5b.5).

        Exactly one recovery worker acts. Returns True if caller won; False if already claimed.
        Raises: TransitionConflict if not in 'open' status.
        """
        ...

    def list_events_awaiting_alert(
        self,
        severity: str,
        status: str = "open",
        realert_before: str | None = None,
        limit: int = 200,
    ) -> list[ReliabilityEvent]:
        """Events of *severity*/*status* that still owe the operator an alert.

        Never-alerted events always qualify. An already-alerted event qualifies
        again only when *realert_before* is supplied and its ``alerted_at``
        predates it — that is the opt-in re-alert cadence. Without it an event
        alerts exactly once, which is what stops a permanently-open critical
        event from re-notifying on every watch cycle (migration 053).
        """
        ...

    def mark_event_alerted(self, event_id: str, alerted_at: str | None = None) -> bool:
        """Stamp ``alerted_at`` on an event. Returns True when a row changed."""
        ...

    def update_event_recovery(
        self,
        event_id: str,
        recovery_json: dict[str, Any],
        status: str | None = None,
        actor: str = "recovery",
    ) -> bool:
        """Persist recovery actions onto an event (merge), optionally moving status.

        Recovery that acted must leave a durable record (codex #4): the merged
        ``recovery_json`` survives, and any status move appends a log row.
        Returns False when the event is missing.
        """
        ...

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a base-store run row by id (inherited from ``SqliteStore``).

        Recovery re-enqueues transient run failures and needs the originating
        run; concrete reliability stores share the base store's ``runs`` table.
        """
        ...

    # --- Improvement lifecycle (pipeline)

    def create_improvement(
        self,
        origin: str,
        kind: str,
        title: str,
        summary: str = "",
        root_cause: str = "",
        proposal_json: dict[str, Any] | None = None,
        created_by: str = "system",
    ) -> str:
        """Create a new improvement proposal (proposed status).

        Returns improvement id.
        """
        ...

    def get_improvement(self, imp_id: str) -> Improvement | None:
        """Fetch an improvement by id."""
        ...

    def list_improvements(
        self,
        status: str | None = None,
        origin: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Improvement]:
        """List improvements, optionally filtered."""
        ...

    def transition_improvement(
        self,
        imp_id: str,
        expected_status: str,
        new_status: str,
        actor: str,
        detail_json: dict[str, Any] | None = None,
    ) -> None:
        """CAS status transition: UPDATE WHERE status=? AND version=? in atomic transaction
        that also appends a reliability_log row (§5b.1).

        Call this for EVERY status change (proposed→testing, testing→judging, etc.).
        Atomically increments version and appends immutable log entry.

        Raises: TransitionConflict if expected_status or version mismatch.
        """
        ...

    def update_improvement_fields(
        self,
        imp_id: str,
        **fields: Any,
    ) -> None:
        """Partial update to non-status fields (risk_level, ranking_score, sandbox_json, etc.).

        For field updates that do NOT trigger a status transition. Do NOT use this
        for status changes — use transition_improvement instead.
        """
        ...

    # --- Judge votes (append-only, idempotent)

    def insert_vote(
        self,
        improvement_id: str,
        panel_attempt_id: str,
        judge_agent: str,
        model_family: str,
        verdict: str,
        scores_json: dict[str, Any] | None = None,
        reasoning: str = "",
        conditions: str = "",
        model: str = "",
    ) -> str:
        """Insert a judge vote.

        UNIQUE(improvement_id, panel_attempt_id, model_family) enforces idempotency:
        a retried judge cannot double-vote (§5b.7). ON CONFLICT REPLACE to update.

        Returns vote id.
        """
        ...

    def get_vote(self, vote_id: str) -> ImprovementVote | None:
        """Fetch a vote by id."""
        ...

    def list_votes(
        self,
        improvement_id: str | None = None,
        panel_attempt_id: str | None = None,
        limit: int = 100,
    ) -> list[ImprovementVote]:
        """List votes, optionally filtered by improvement or panel attempt."""
        ...

    # --- Audit lifecycle (append-only, no DELETE)

    def create_audit(
        self,
        kind: str,
        window_start: str,
        window_end: str,
    ) -> str:
        """Create an audit row in 'queued' status.

        Returns audit id.
        """
        ...

    def get_audit(self, audit_id: str) -> ReliabilityAudit | None:
        """Fetch an audit by id."""
        ...

    def list_audits(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityAudit]:
        """List audits, optionally filtered."""
        ...

    def start_audit(self, audit_id: str) -> None:
        """Transition audit from queued → running."""
        ...

    def complete_audit(
        self,
        audit_id: str,
        stats_json: dict[str, Any] | None = None,
        findings: int = 0,
        report_note_path: str | None = None,
    ) -> None:
        """Transition audit from running → completed."""
        ...

    def fail_audit(self, audit_id: str, error: str = "") -> None:
        """Transition audit from running → failed."""
        ...

    # --- Lease (mutual exclusion for apply/rollback, §5b.3)

    def acquire_lease(
        self,
        key: str,
        owner: str,
        duration_seconds: int = 3600,
    ) -> str:
        """Acquire a lease (or renew if already held by same owner).

        Fencing token returned; lease expires after duration_seconds.
        Raises: LeaseConflict if held by different owner.
        """
        ...

    def assert_lease(self, key: str, owner: str, token: str) -> None:
        """Fence a mutation by proving this exact token is still current (§5b.3).

        Raises: LeaseConflict if the lease is missing, corrupt, expired, or held
        with a different owner/token.
        """
        ...

    def renew_lease(self, key: str, owner: str, token: str, duration_seconds: int = 3600) -> None:
        """Renew an existing lease (must hold valid token).

        Raises: LeaseConflict if token stale or held by different owner.
        """
        ...

    def release_lease(self, key: str, owner: str, token: str) -> None:
        """Release a held lease."""
        ...

    def reclaim_stale_lease(self, key: str, threshold_seconds: int = 600) -> bool:
        """Reclaim an expired lease (§5b.3 fencing).

        Returns True if reclaimed; False if lease still valid.
        """
        ...

    # --- Watch cursor (incremental scan boundary, §5b.5)

    def get_watch_cursor(self) -> str | None:
        """Fetch the watch high-water mark (timestamp or row id).

        Returns cursor value (e.g., ISO timestamp of last-scanned event), or
        ``None`` when the watch has never run (or its state is unreadable).
        """
        ...

    def advance_watch_cursor(self, new_cursor: str) -> None:
        """Advance the watch cursor ONLY after all events in the window are durably written."""
        ...

    # --- Scorecards (upsert)

    def upsert_scorecard(
        self,
        subject_type: str,
        subject_id: str,
        window: str,
        period_start: str,
        metrics_json: dict[str, Any] | None = None,
    ) -> str:
        """Insert or replace a scorecard row.

        UNIQUE(subject_type, subject_id, window, period_start) enforces idempotency.
        Returns scorecard id.
        """
        ...

    def get_scorecard(
        self,
        subject_type: str,
        subject_id: str,
        window: str,
        period_start: str,
    ) -> Scorecard | None:
        """Fetch a scorecard by composite key."""
        ...

    def list_scorecards(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        window: str | None = None,
        limit: int = 100,
    ) -> list[Scorecard]:
        """List scorecards, optionally filtered."""
        ...

    # --- Organization (org_units, agents with new columns)

    def create_org_unit(
        self,
        name: str,
        kind: str,
        parent_id: str | None = None,
        charter: str = "",
    ) -> str:
        """Create an organization unit.

        Returns org_unit id.
        """
        ...

    def get_org_unit(self, unit_id: str) -> OrgUnit | None:
        """Fetch an org_unit by id."""
        ...

    def list_org_units(
        self,
        kind: str | None = None,
        parent_id: str | None = None,
        status: str = "active",
    ) -> list[OrgUnit]:
        """List org_units, optionally filtered."""
        ...

    def create_agent(
        self,
        name: str,
        org_unit_id: str | None = None,
        org_role: str = "specialist",
        title: str = "",
        charter: str = "",
        model: str | None = None,
        harness: str | None = None,
        schedule_json: dict[str, Any] | None = None,
        vault_note_path: str | None = None,
    ) -> str:
        """Create an agent (mapped to agents row with new org columns).

        Returns agent id.
        """
        ...

    def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch an agent by id."""
        ...

    def list_agents(
        self,
        org_unit_id: str | None = None,
        org_role: str | None = None,
        enabled: int | None = None,
    ) -> list[Agent]:
        """List agents, optionally filtered."""
        ...

    def update_agent(self, agent_id: str, **fields: Any) -> None:
        """Partial update to an agent (name, model, enabled, etc.)."""
        ...

    # --- Agent requests (lifecycle)

    def create_agent_request(
        self,
        description: str,
        from_agent_id: str = "system",
        from_lane: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create an agent request (pending status).

        Returns agent_request id.
        """
        ...

    def get_agent_request(self, req_id: str) -> AgentRequest | None:
        """Fetch an agent request by id."""
        ...

    def list_agent_requests(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[AgentRequest]:
        """List agent requests, optionally filtered."""
        ...

    def update_agent_request_status(
        self,
        req_id: str,
        status: str,
        design_json: dict[str, Any] | None = None,
        improvement_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Update agent request status and related fields."""
        ...

    # --- Autonomy settings (read-only from store, written via API)

    def get_autonomy_setting(self, scope_type: str, scope_id: str = "") -> AutonomySetting | None:
        """Fetch autonomy setting by scope (most-specific-scope-wins resolution in caller).

        Scopes: global > department > agent > kind.
        """
        ...

    def list_autonomy_settings(self) -> list[AutonomySetting]:
        """List all autonomy settings."""
        ...

    def resolve_autonomy(
        self, agent_id: str | None = None, kind: str | None = None
    ) -> AutonomySetting:
        """Resolve effective autonomy for an agent/kind (most-specific scope wins).

        Returns the active setting or falls back to global default (approve/0).
        """
        ...
