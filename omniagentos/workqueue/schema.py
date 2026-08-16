"""Workqueue value types — the frozen vocabulary of the shared pool.

Everything here mirrors omniagentos/workqueue/migrations/001_workqueue.sql exactly.
If a value is not representable in that migration's CHECK constraints, it does not
belong in these enums. Do not import anything from omniagentos.db here — the queue
DB is a separate file with a separate migrator (SPEC-shared-queue §1.2).
"""

from __future__ import annotations

import enum
import secrets
import time
from dataclasses import dataclass, field

# ``enum.StrEnum`` rather than ``(str, Enum)``: identical membership/comparison
# semantics for every use in this package (each member's ``.value`` is what
# reaches SQLite, and the frozensets below are compared against plain strings),
# but ``str(MEMBER)`` renders the VALUE instead of ``'ClassName.MEMBER'`` — the
# footgun that a mixin str-enum keeps loaded. Repo-wide grep confirms nothing
# interpolates these members directly; ruff UP042 flagged the old form.


class UnitState(enum.StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    REVIEW = "review"
    DONE = "done"
    PARKED = "parked"
    CANCELLED = "cancelled"


class RiskClass(enum.StrEnum):
    MECHANICAL = "mechanical"
    STANDARD = "standard"
    SENSITIVE = "sensitive"  # never auto-lands; always stops at state='review'


class Outcome(enum.StrEnum):
    PASS = "pass"
    CANDIDATE_DEFECT = "candidate-defect"
    INSTRUMENT_ERROR = "instrument-error"
    ENVIRONMENT = "environment"
    CONTENTION_FLAKE = "contention-flake"
    UNCHANGED_RETRY = "unchanged-retry"
    STORM_PARKED = "storm-parked"
    LEASE_LOST = "lease-lost"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"


#: Outcomes that must NOT consume the candidate's attempt budget (§3.1 attempt
#: accounting): record_result reverses the claim-time increment for these, and the
#: first three additionally increment instrument_retries.
NON_CONSUMING_OUTCOMES = frozenset(
    {
        Outcome.INSTRUMENT_ERROR,
        Outcome.ENVIRONMENT,
        Outcome.CONTENTION_FLAKE,
        Outcome.UNCHANGED_RETRY,
        Outcome.LEASE_LOST,
        Outcome.ABANDONED,
    }
)
# NOTE: TIMEOUT deliberately consumes the attempt — SPEC §3.1 lists exactly six
# reversing outcomes and timeout is not one of them; the unit's own timeout_s
# bounds the work, so exceeding it is evidence about the candidate, not the box.

#: The subset that also increments instrument_retries.
INSTRUMENT_RETRY_OUTCOMES = frozenset(
    {Outcome.INSTRUMENT_ERROR, Outcome.ENVIRONMENT, Outcome.CONTENTION_FLAKE}
)


class TerminalReason(enum.StrEnum):
    ACCEPTED = "accepted"
    ATTEMPTS_EXHAUSTED = "attempts-exhausted"
    STORM_PARKED = "storm-parked"
    TERMINAL_INSTRUMENT = "terminal-instrument"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    UNCLAIMABLE = "unclaimable-no-capable-machine"


#: Lease: 120 s TTL renewed every 30 s — four missed beats before reclaim (§3.3).
#: The lease bounds the SILENCE, the unit's timeout_s bounds the WORK.
LEASE_TTL_S = 120
HEARTBEAT_INTERVAL_S = 30

#: Refusal-storm cap per (input_key, gate) — §4.5.
REFUSAL_STORM_CAP = 5

#: Instrument-retry backoff schedule, seconds — §4.5.
INSTRUMENT_BACKOFF_S = (60, 300, 900)
INSTRUMENT_RETRY_CAP = 3


def utc_now_iso() -> str:
    """UTC ISO-8601 with Z suffix. Local copy — omniagentos.contracts is Tier P.

    time.gmtime never consults TZ, so this is immune to the time.mktime local-TZ
    corruption class recorded in project memory (2026-08-11).
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_unit_id() -> str:
    return "wq_" + _ulid()


def new_boot_nonce() -> str:
    """8-hex nonce generated once per worker process — a pid is meaningless across
    machines and reusable within one; the nonce makes worker_id globally unique
    without needing to prove anything about a remote process."""
    return secrets.token_hex(4)


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """Crockford-base32 ULID: 48-bit ms timestamp + 80 random bits, lexically sortable."""
    ts = int(time.time() * 1000)
    out = []
    for shift in range(45, -1, -5):
        out.append(_ULID_ALPHABET[(ts >> shift) & 0x1F])
    rand = secrets.randbits(80)
    for shift in range(75, -1, -5):
        out.append(_ULID_ALPHABET[(rand >> shift) & 0x1F])
    return "".join(out)


@dataclass
class WorkUnit:
    id: str
    idempotency_key: str
    created_at: str
    updated_at: str
    repo_url: str
    repo_slug: str
    base_sha: str
    base_ref: str
    branch: str
    owned_paths: list[str]
    agent_profile: str
    acceptance_cmd: str
    risk_class: str
    brief_inline: str | None = None
    brief_path: str | None = None
    acceptance_gate: str | None = None
    #: Person (or agent id) who offloaded this unit — 'owner' / 'bob' / 'alice',
    #: defaulted from env WQ_USER at enqueue. Empty means unattributed; it is
    #: never inferred from the claiming machine, which is a fact about WHERE the
    #: unit ran, not about who asked for it (ROUTING-DECISIONS §2).
    submitted_by: str = ""
    timeout_s: int = 3600
    labels: list[str] = field(default_factory=list)
    state: str = UnitState.QUEUED.value
    priority: int = 2
    attempt: int = 0
    max_attempts: int = 3
    instrument_retries: int = 0
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    lease_generation: int = 0
    terminal_reason: str | None = None
    result_branch: str | None = None
    result_sha: str | None = None
    finished_at: str | None = None
    cancel_requested: int = 0
    park_remedy: str | None = None
    alerted_at: str | None = None


@dataclass
class Attempt:
    unit_id: str
    attempt: int
    lease_generation: int
    machine_id: str
    worker_id: str
    started_at: str
    finished_at: str | None = None
    input_key: str | None = None
    outcome: str | None = None
    exit_code: int | None = None
    gate_exit_code: int | None = None
    retryable: int | None = None
    remedy: str | None = None
    head_sha: str | None = None
    log_path: str | None = None


@dataclass
class Machine:
    machine_id: str
    hostname: str
    os: str = "darwin"
    ssh_target: str | None = None
    labels: list[str] = field(default_factory=list)
    max_concurrent: int = 1
    drain: int = 0
    enrolled_at: str = ""
    last_seen_at: str | None = None
    agent_version: str | None = None
    notes: str | None = None
    ncpu: int | None = None
    perf_cores: int | None = None
    mem_gb: float | None = None
    last_load1: float | None = None
    last_load5: float | None = None
    last_mem_free_gb: float | None = None
    ceiling_fraction: float = 0.75
    telemetry_at: str | None = None


class LeaseLost(Exception):
    """Raised when a fenced write matches zero rows: you were adopted, stop.

    The worker holding this lease must kill its child process tree immediately,
    record outcome='lease-lost', and write no result.
    """
