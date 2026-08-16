"""H-07: durable worker evidence + CAS/lease-guarded re-drive for stranded approvals.

After human approve/apply, the API spawns a detached worker. Without durable
spawn/exit evidence and a re-driver, a failed spawn or ``apply_lease_held`` /
overlap deferral leaves the row stuck in ``approved`` forever.

This module:
  * records spawn attempts, PIDs, log paths, exit codes, and bounded log tails
    in ``reliability_state`` (key ``improvement_worker:<id>``);
  * re-drives stranded ``approved`` rows after a grace period under a single-flight
    redrive lease (exactly-once across concurrent reconcilers);
  * retries lease/overlap deferrals;
  * escalates to ``failed`` + critical notification when attempts are exhausted.

Soft deferrals vs hard failures
-------------------------------
A deferral is NOT a failure. ``overlap_with_monitoring`` means a sibling improvement is
inside a 24–72 h governance observation window; the scheduled ``watch`` loop probes every
600 s, so counting those probes against a 3-attempt budget false-fails legitimate work in
~30 minutes and it can never wait for monitoring to clear. Hard evidence (spawn failure,
worker crash/exit, apply failure, an unrecognised deferral reason) increments
``hard_attempts``, which is the ONLY counter the escalation budget reads. Recognised soft
deferrals instead persist a durable ``soft_deferral`` record carrying a bounded horizon
derived from an authoritative timestamp (the blocking row's ``monitor_until``, or the
``reliability:apply`` lease expiry), and the row stays ``approved`` until either the
overlap clears — in which case it applies exactly once through the existing CAS/lease
safeguards — or the horizon expires, in which case it escalates visibly with a specific
reason. There is no unbounded state: a missing, malformed, or non-advancing horizon is
itself an escalation, never a silent stall.

Residual risk (documented, out of scope here): a worker PID that is alive but HUNG is
still skipped as ``skipped_live`` forever. The re-driver deliberately does not kill or
time out live processes; that needs a separate supervised-timeout design.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.reliability.contracts import (
    LeaseConflict,
    ReliabilityStore,
    TransitionConflict,
)
from omniagentos.reliability.taxonomy import ImprovementStatus

REDRIVE_LEASE_KEY = "reliability:redrive"
WORKER_STATE_PREFIX = "improvement_worker:"
DEFAULT_GRACE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_REDRIVE_LEASE_SECONDS = 120
LOG_SUMMARY_MAX_CHARS = 2000

# Outcomes recorded on the durable worker record.
OUTCOME_SPAWNED = "spawned"
OUTCOME_SPAWN_FAILED = "spawn_failed"
OUTCOME_DEFERRED = "deferred"
OUTCOME_APPLIED = "applied"
OUTCOME_EXITED = "exited"
OUTCOME_FAILED = "failed"
OUTCOME_ESCALATED = "escalated"

# Outcomes that are evidence of a real failure and therefore spend the hard budget.
HARD_OUTCOMES = frozenset({OUTCOME_SPAWN_FAILED, OUTCOME_FAILED, OUTCOME_EXITED})

# --- Soft deferral accounting (H-07) ---------------------------------------

# Deferral reasons emitted by ``ImprovementPipeline.apply`` that mean "not yet",
# not "broken". Anything NOT in this map is treated as a hard failure so an
# unknown reason can never open an unbounded silent wait.
SOFT_MONITORING_OVERLAP = "monitoring_overlap"
SOFT_LEASE_CONTENTION = "lease_contention"
SOFT_DEFERRAL_CLASSES: dict[str, str] = {
    "overlap_with_monitoring": SOFT_MONITORING_OVERLAP,
    "apply_lease_held": SOFT_LEASE_CONTENTION,
}

# The apply lease the pipeline holds while mutating the repo; its durable row in
# ``reliability_state`` carries the authoritative ``expires_at``.
APPLY_LEASE_KEY = "reliability:apply"
LEASE_STATE_PREFIX = "lease:"

# Monitoring overlap: wait until the blocking observation window ends, plus slack for
# the confirm sweep, and never longer than the absolute cap measured from first defer.
OVERLAP_HORIZON_SLACK_SECONDS = 900  # 15 min
OVERLAP_FALLBACK_SECONDS = 900  # unknown/unbounded blocker ⇒ short, re-derived next pass
MAX_OVERLAP_DEFERRAL_SECONDS = 7 * 24 * 3600  # absolute cap: 7 days

# Lease contention is a MINUTES-scale condition, deliberately budgeted separately from
# the HOURS-scale monitoring overlap so the two can never be conflated.
LEASE_CONTENTION_BUDGET_SECONDS = 3600  # 1 h floor from first contention
LEASE_HORIZON_SLACK_SECONDS = 120
MAX_LEASE_CONTENTION_SECONDS = 7200  # absolute cap: 2 h

# Specific, durable escalation reasons — each distinguishable in alerts and audits.
ESCALATION_OVERLAP_HORIZON_EXPIRED = "overlap_horizon_expired"
ESCALATION_LEASE_CONTENTION_EXHAUSTED = "apply_lease_contention_exhausted"
ESCALATION_SOFT_STATE_INVALID = "soft_deferral_state_invalid"

SpawnFn = Callable[[str, str], dict[str, Any]]  # (command, improvement_id) -> meta
ApplyFn = Callable[[str], Any]  # (improvement_id) -> ApplyResult-like
NotifyFn = Callable[..., Any]
PidAliveFn = Callable[[int], bool]
ClockFn = Callable[[], datetime]


def worker_state_key(improvement_id: str) -> str:
    return f"{WORKER_STATE_PREFIX}{improvement_id}"


def _now(clock: ClockFn | None = None) -> datetime:
    return clock() if clock else datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp as UTC-aware, or return None.

    Naive values (legacy rows written without a zone) are read as UTC so horizon
    arithmetic never raises on an aware/naive comparison mid-sweep.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _state_put(store: ReliabilityStore, key: str, value: dict[str, Any]) -> None:
    """Persist a reliability_state row (sqlite or test-double dict)."""
    payload = json.dumps(value)
    now = utc_now_iso()
    conn = getattr(store, "_connection", None)
    if conn is not None:
        lock = getattr(store, "_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            conn.execute(
                "INSERT INTO reliability_state (key, value_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                (key, payload, now),
            )
            conn.commit()
        finally:
            if lock is not None:
                lock.release()
        return
    bag = getattr(store, "reliability_state", None)
    if isinstance(bag, dict):
        bag[key] = dict(value)
        return
    raise RuntimeError("store has no reliability_state backend for worker evidence")


def _state_get(store: ReliabilityStore, key: str) -> dict[str, Any] | None:
    conn = getattr(store, "_connection", None)
    if conn is not None:
        lock = getattr(store, "_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?", (key,)
            ).fetchone()
        finally:
            if lock is not None:
                lock.release()
        if not row:
            return None
        raw = row["value_json"] if hasattr(row, "keys") else row[0]
        return json.loads(raw or "{}")
    bag = getattr(store, "reliability_state", None)
    if isinstance(bag, dict):
        val = bag.get(key)
        return dict(val) if isinstance(val, dict) else None
    return None


def get_worker_state(store: ReliabilityStore, improvement_id: str) -> dict[str, Any]:
    return dict(_state_get(store, worker_state_key(improvement_id)) or {})


def put_worker_state(
    store: ReliabilityStore,
    improvement_id: str,
    **patch: Any,
) -> dict[str, Any]:
    """Merge *patch* into the durable worker record and return the full record."""
    state = get_worker_state(store, improvement_id)
    state.update(patch)
    state["improvement_id"] = improvement_id
    state["updated_at"] = utc_now_iso()
    _state_put(store, worker_state_key(improvement_id), state)
    return state


def summarize_log(log_path: str | Path | None, *, max_chars: int = LOG_SUMMARY_MAX_CHARS) -> str:
    """Return a bounded tail of a worker log file (stdout+stderr)."""
    if not log_path:
        return ""
    path = Path(log_path)
    try:
        if not path.is_file():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) <= max_chars:
        return data
    return data[-max_chars:]


def default_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
    except OSError:
        return False
    return True


# --- Hard/soft attempt accounting (H-07) -----------------------------------

# Per-class ceiling measured from the FIRST deferral, and the escalation reason used
# when the horizon runs out. Both keyed by soft class so the two conditions can never
# borrow each other's budget.
_SOFT_MAX_SECONDS: dict[str, int] = {
    SOFT_MONITORING_OVERLAP: MAX_OVERLAP_DEFERRAL_SECONDS,
    SOFT_LEASE_CONTENTION: MAX_LEASE_CONTENTION_SECONDS,
}
_SOFT_EXPIRY_REASON: dict[str, str] = {
    SOFT_MONITORING_OVERLAP: ESCALATION_OVERLAP_HORIZON_EXPIRED,
    SOFT_LEASE_CONTENTION: ESCALATION_LEASE_CONTENTION_EXHAUSTED,
}


def hard_attempt_count(state: dict[str, Any]) -> int:
    """Failure evidence recorded so far. The ONLY counter the budget spends."""
    try:
        return max(0, int(state.get("hard_attempts") or 0))
    except (TypeError, ValueError):
        return 0


def classify_deferral(reason: str | None) -> str | None:
    """Map a pipeline deferral reason to a soft class, or None if it is not soft.

    Unrecognised reasons return None on purpose: an unknown deferral is accounted as a
    hard failure so it exhausts the budget and surfaces, rather than waiting forever on
    a horizon nobody can derive.
    """
    if not reason:
        return None
    return SOFT_DEFERRAL_CLASSES.get(str(reason).strip())


def _monitoring_overlap_horizon(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    first_at: datetime,
    now: datetime,
) -> tuple[datetime, str]:
    """Deadline for a monitoring overlap, from the blocking row's observation window.

    Authoritative source is the largest ``monitor_until`` among the OTHER rows currently
    in ``monitoring`` — the same set :meth:`ImprovementPipeline.apply` consults — plus
    slack so the confirm sweep gets a full watch cycle to clear the row after the window
    ends. When that is unreadable, absent, or unbounded we fall back to a SHORT horizon
    and re-derive next pass. Every branch is clamped to ``first_at + 7 days``, so even a
    permanently stuck blocker terminates in a visible escalation.
    """
    cap = first_at + timedelta(seconds=MAX_OVERLAP_DEFERRAL_SECONDS)
    fallback = now + timedelta(seconds=OVERLAP_FALLBACK_SECONDS)
    try:
        others = store.list_improvements(status=ImprovementStatus.MONITORING.value, limit=200)
    except Exception:  # noqa: BLE001 — an unreadable list is a short wait, not a crash
        return min(fallback, cap), "monitoring_unreadable"

    blocking = [o for o in others if getattr(o, "id", None) != improvement_id]
    if not blocking:
        # Overlap was reported but the blocker already cleared (or is not visible here):
        # wait one short beat and let the next probe settle it.
        return min(fallback, cap), "no_blocking_row"

    deadlines = [
        parsed
        for o in blocking
        if (parsed := _parse_iso(getattr(o, "monitor_until", None))) is not None
    ]
    if not deadlines:
        return min(fallback, cap), "unbounded_monitor_until"
    horizon = max(deadlines) + timedelta(seconds=OVERLAP_HORIZON_SLACK_SECONDS)
    return min(horizon, cap), "monitor_until"


def _lease_contention_horizon(
    store: ReliabilityStore,
    *,
    first_at: datetime,
    now: datetime,
) -> tuple[datetime, str]:
    """Deadline for apply-lease contention — minutes, never the monitoring time-scale.

    Prefers the authoritative ``expires_at`` on the durable ``reliability:apply`` lease
    row; otherwise a flat contention budget. Both clamped to ``first_at + 2 h``, because
    a lease that never releases is an operational fault, not something to wait days for.
    """
    del now  # horizon is anchored to first contention, not to the current probe
    floor = first_at + timedelta(seconds=LEASE_CONTENTION_BUDGET_SECONDS)
    cap = first_at + timedelta(seconds=MAX_LEASE_CONTENTION_SECONDS)
    try:
        lease = _state_get(store, f"{LEASE_STATE_PREFIX}{APPLY_LEASE_KEY}")
    except Exception:  # noqa: BLE001
        lease = None
    expires = _parse_iso((lease or {}).get("expires_at") if isinstance(lease, dict) else None)
    if expires is None:
        return min(floor, cap), "contention_budget"
    horizon = max(floor, expires + timedelta(seconds=LEASE_HORIZON_SLACK_SECONDS))
    return min(horizon, cap), "apply_lease_expiry"


def build_soft_deferral(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    soft_class: str,
    reason: str,
    previous: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Build the durable soft-deferral record for one deferral observation.

    Per-class ``first_at`` timestamps are preserved in ``first_at_by_class`` across repeats
    and alternating deferral classes so the absolute caps (7-day overlap, 2-hour lease) are
    measured from each class's initial occurrence and cannot be reset by alternating
    deferral reasons. The horizon for the current class is recomputed here, at record
    time, from live authoritative state.
    """
    prior = previous if isinstance(previous, dict) else {}
    first_at_by_class = dict(prior.get("first_at_by_class") or {})

    # Legacy fallback: if prior had class and first_at but no first_at_by_class
    if not first_at_by_class and prior.get("class") and prior.get("first_at"):
        first_at_by_class[str(prior["class"])] = str(prior["first_at"])

    class_first_iso = first_at_by_class.get(soft_class)
    first_at = _parse_iso(class_first_iso) if class_first_iso else None

    if first_at is None or first_at > now:  # clock skew / new class / tampered row
        first_at = now
        first_at_by_class[soft_class] = _iso(now)
    else:
        first_at_by_class[soft_class] = _iso(first_at)

    try:
        count = int(prior.get("count") or 0)
    except (TypeError, ValueError):
        count = 0

    if soft_class == SOFT_MONITORING_OVERLAP:
        horizon, source = _monitoring_overlap_horizon(
            store, improvement_id, first_at=first_at, now=now
        )
    else:
        horizon, source = _lease_contention_horizon(store, first_at=first_at, now=now)

    return {
        "class": soft_class,
        "reason": reason,
        "count": count + 1,
        "first_at": _iso(first_at),
        "last_at": _iso(now),
        "horizon": _iso(horizon),
        "horizon_source": source,
        "first_at_by_class": first_at_by_class,
    }


class SoftDeferralVerdict:
    """Outcome of evaluating a persisted soft-deferral record against the clock."""

    WAITING = "waiting"
    EXPIRED = "expired"
    INVALID = "invalid"

    __slots__ = ("state", "reason", "detail")

    def __init__(self, state: str, reason: str, detail: dict[str, Any]) -> None:
        self.state = state
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SoftDeferralVerdict({self.state!r}, {self.reason!r})"


def evaluate_soft_deferral(
    record: Any,
    *,
    now: datetime,
) -> SoftDeferralVerdict | None:
    """Decide whether a persisted soft deferral may keep waiting.

    Returns None when there is no soft deferral in play. Otherwise:
      * ``WAITING``  — inside a well-formed, bounded horizon; keep the row approved.
      * ``EXPIRED``  — the authoritative horizon passed; escalate with a class-specific reason.
      * ``INVALID``  — the record is malformed, or claims a horizon beyond its class cap;
        escalate rather than honour an unbounded wait.
    """
    if record is None:
        return None
    if not isinstance(record, dict):
        return SoftDeferralVerdict(
            SoftDeferralVerdict.INVALID, ESCALATION_SOFT_STATE_INVALID, {"defect": "not_a_mapping"}
        )

    soft_class = record.get("class")
    max_seconds = _SOFT_MAX_SECONDS.get(str(soft_class))
    if max_seconds is None:
        return SoftDeferralVerdict(
            SoftDeferralVerdict.INVALID,
            ESCALATION_SOFT_STATE_INVALID,
            {"defect": "unknown_class", "class": soft_class},
        )

    first_at = _parse_iso(record.get("first_at"))
    horizon = _parse_iso(record.get("horizon"))
    detail: dict[str, Any] = {
        "class": soft_class,
        "deferral_reason": record.get("reason"),
        "count": record.get("count"),
        "first_at": record.get("first_at"),
        "horizon": record.get("horizon"),
        "horizon_source": record.get("horizon_source"),
    }
    if first_at is None or horizon is None:
        detail["defect"] = "unparseable_timestamps"
        return SoftDeferralVerdict(
            SoftDeferralVerdict.INVALID, ESCALATION_SOFT_STATE_INVALID, detail
        )
    if horizon > first_at + timedelta(seconds=max_seconds):
        # Defence in depth: never honour a horizon wider than the class cap, however
        # it got written. An unbounded wait is a defect, not a policy.
        detail["defect"] = "horizon_exceeds_cap"
        detail["max_seconds"] = max_seconds
        return SoftDeferralVerdict(
            SoftDeferralVerdict.INVALID, ESCALATION_SOFT_STATE_INVALID, detail
        )
    if now >= horizon:
        detail["deadline"] = record.get("horizon")
        return SoftDeferralVerdict(
            SoftDeferralVerdict.EXPIRED,
            _SOFT_EXPIRY_REASON[str(soft_class)],
            detail,
        )

    # Recheck all recorded deferral classes against their respective class caps
    first_at_by_class = record.get("first_at_by_class")
    if isinstance(first_at_by_class, dict):
        for cls, cls_first_iso in first_at_by_class.items():
            cls_max = _SOFT_MAX_SECONDS.get(str(cls))
            if cls_max is None:
                continue
            cls_first = _parse_iso(cls_first_iso)
            if cls_first is not None and now >= cls_first + timedelta(seconds=cls_max):
                exp_reason = _SOFT_EXPIRY_REASON.get(str(cls), ESCALATION_OVERLAP_HORIZON_EXPIRED)
                cls_detail = dict(detail)
                cls_detail["expired_class"] = cls
                cls_detail["class_first_at"] = cls_first_iso
                cls_detail["deadline"] = _iso(cls_first + timedelta(seconds=cls_max))
                return SoftDeferralVerdict(
                    SoftDeferralVerdict.EXPIRED,
                    exp_reason,
                    cls_detail,
                )

    return SoftDeferralVerdict(SoftDeferralVerdict.WAITING, str(record.get("reason") or ""), detail)


def record_spawn_attempt(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    command: str,
    python: str,
    cwd: str,
    log_path: str,
    argv: list[str],
) -> dict[str, Any]:
    """Record the planned spawn (pre-Popen) and count it.

    ``spawn_attempts`` is TOTAL-spawn telemetry, not a failure budget: a re-drive that
    legitimately re-spawns after a soft deferral increments it, so spending the
    escalation budget on it is exactly the H-07 false-fail. Only ``hard_attempts``,
    written when a spawn or a worker actually fails, gates escalation.
    """
    state = get_worker_state(store, improvement_id)
    attempts = int(state.get("spawn_attempts") or 0) + 1
    return put_worker_state(
        store,
        improvement_id,
        command=command,
        python=python,
        cwd=cwd,
        log=str(log_path),
        argv=list(argv),
        spawn_attempts=attempts,
        last_spawn_at=utc_now_iso(),
        outcome=OUTCOME_SPAWNED,
        last_spawn_error=None,
        pid=None,
        exit_code=None,
        exit_recorded_pid=None,
        log_summary="",
    )


def record_spawn_success(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    pid: int,
    log_path: str,
) -> dict[str, Any]:
    store.update_improvement_fields(improvement_id, stage_started_at=utc_now_iso())
    return put_worker_state(
        store,
        improvement_id,
        pid=int(pid),
        log=str(log_path),
        outcome=OUTCOME_SPAWNED,
        last_spawn_error=None,
        exit_code=None,
        running=True,
    )


def record_spawn_failure(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    error: str,
    log_path: str | None = None,
    escalate: bool = False,
) -> dict[str, Any]:
    """Durable spawn-failure evidence. Leaves the row approved for re-drive unless escalated.

    A spawn that never started is HARD evidence, so it spends the escalation budget.
    """
    prior = get_worker_state(store, improvement_id)
    state = put_worker_state(
        store,
        improvement_id,
        outcome=OUTCOME_SPAWN_FAILED,
        last_spawn_error=str(error)[:1000],
        running=False,
        log=str(log_path) if log_path else prior.get("log"),
        exit_code=None,
        pid=None,
        hard_attempts=hard_attempt_count(prior) + 1,
        soft_deferral=None,
    )
    # Mirror a compact signal on the improvement row for operators/API readers.
    store.update_improvement_fields(
        improvement_id,
        last_error_json=json.dumps(
            {
                "kind": "spawn_failed",
                "error": str(error)[:500],
                "spawn_attempts": state.get("spawn_attempts"),
                "hard_attempts": state.get("hard_attempts"),
                "log": state.get("log"),
                "at": utc_now_iso(),
            }
        ),
        stage_started_at=utc_now_iso(),
    )
    if escalate:
        _escalate_to_failed(
            store,
            improvement_id,
            reason="spawn_attempts_exhausted",
            detail={
                "error": str(error)[:500],
                "spawn_attempts": state.get("spawn_attempts"),
                "hard_attempts": state.get("hard_attempts"),
            },
        )
        state = put_worker_state(store, improvement_id, outcome=OUTCOME_ESCALATED)
    return state


def record_worker_exit(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    exit_code: int | None,
    outcome: str,
    reason: str = "",
    applied_sha: str | None = None,
    deferred: bool = False,
    log_path: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
    exit_recorded_pid: int | None = None,
) -> dict[str, Any]:
    """Record CLI/worker completion evidence (exit status + bounded log summary).

    This is also where a deferral is CLASSIFIED, because it is the one point both the
    in-process re-drive and the detached ``apply`` CLI funnel through — so an
    out-of-process worker that defers gets the same durable, bounded soft-deferral
    record as an in-process one, and it survives a restart because it lives in
    ``reliability_state``.

    Accounting:
      * recognised soft deferral → persist/refresh ``soft_deferral`` with a bounded
        horizon; ``hard_attempts`` untouched;
      * unrecognised deferral reason → HARD, so an unknown "not yet" cannot stall forever;
      * ``failed`` / ``exited`` / ``spawn_failed`` → HARD;
      * ``applied`` (and any other terminal success) → clear the soft record.
    """
    state = get_worker_state(store, improvement_id)
    log = log_path or state.get("log")
    summary = summarize_log(log)
    at = _now(None) if now is None else now
    patch: dict[str, Any] = {
        "exit_code": exit_code,
        "outcome": outcome,
        "running": False,
        "finished_at": utc_now_iso(),
        "log_summary": summary,
        "last_reason": reason,
    }
    if exit_recorded_pid is not None:
        patch["exit_recorded_pid"] = exit_recorded_pid
    if log:
        patch["log"] = str(log)
    if applied_sha:
        patch["applied_sha"] = applied_sha
    if error:
        patch["last_error"] = str(error)[:1000]

    is_deferral = bool(deferred) or outcome == OUTCOME_DEFERRED
    soft_class = classify_deferral(reason) if is_deferral else None
    if is_deferral:
        patch["last_deferred_reason"] = reason
    if soft_class is not None:
        patch["soft_deferral"] = build_soft_deferral(
            store,
            improvement_id,
            soft_class=soft_class,
            reason=reason,
            previous=state.get("soft_deferral"),
            now=at,
        )
    else:
        patch["soft_deferral"] = None
        if is_deferral or outcome in HARD_OUTCOMES:
            patch["hard_attempts"] = hard_attempt_count(state) + 1

    state = put_worker_state(store, improvement_id, **patch)
    store.update_improvement_fields(
        improvement_id,
        last_error_json=json.dumps(
            {
                "kind": "worker_exit",
                "exit_code": exit_code,
                "outcome": outcome,
                "reason": reason,
                "hard_attempts": state.get("hard_attempts") or 0,
                "soft_deferral": state.get("soft_deferral"),
                "log_summary": summary[-500:] if summary else "",
                "at": utc_now_iso(),
            }
        ),
    )
    return state


def capture_exit_evidence(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    pid_alive_fn: PidAliveFn | None = None,
) -> dict[str, Any]:
    """If a previously spawned PID is dead, persist exit/log evidence (best-effort)."""
    alive_fn = pid_alive_fn or default_pid_alive
    state = get_worker_state(store, improvement_id)
    pid = state.get("pid")
    if not pid:
        return state
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return state
    if alive_fn(pid_i):
        return put_worker_state(store, improvement_id, running=True)

    # Process gone. Record the exit AT MOST ONCE per spawn generation: a detached worker
    # that died without writing its own exit leaves ``exit_code`` None, so the old
    # "exit_code is not None" guard let every later sweep re-record ``exited`` and, now
    # that ``exited`` is hard evidence, would burn the whole budget in three sweeps
    # without a single new failure. ``exit_recorded_pid`` pins the guard to the PID.
    if state.get("exit_recorded_pid") == pid_i:
        return put_worker_state(store, improvement_id, running=False)
    if state.get("exit_code") is not None and state.get("outcome") not in {
        OUTCOME_SPAWNED,
        None,
        "",
    }:
        # The worker already reported its own outcome (applied/deferred/failed).
        return put_worker_state(store, improvement_id, running=False, exit_recorded_pid=pid_i)
    if state.get("outcome") == OUTCOME_EXITED:
        return put_worker_state(store, improvement_id, running=False, exit_recorded_pid=pid_i)

    return record_worker_exit(
        store,
        improvement_id,
        exit_code=state.get("exit_code"),
        outcome=OUTCOME_EXITED,
        reason="process_not_running",
        log_path=state.get("log"),
        exit_recorded_pid=pid_i,
    )


def _anchor_time(imp: Any, state: dict[str, Any]) -> datetime | None:
    for candidate in (
        state.get("last_spawn_at"),
        getattr(imp, "stage_started_at", None),
        state.get("finished_at"),
        getattr(imp, "updated_at", None),
    ):
        parsed = _parse_iso(candidate if isinstance(candidate, str) else None)
        if parsed is not None:
            return parsed
    return None


def _past_grace(
    imp: Any,
    state: dict[str, Any],
    *,
    grace_seconds: int,
    now: datetime,
) -> bool:
    anchor = _anchor_time(imp, state)
    if anchor is None:
        return True
    return now >= anchor + timedelta(seconds=grace_seconds)


# Alert titles per escalation reason, so an operator can tell a broken apply apart from
# a legitimate wait that ran out of horizon without opening the payload.
_ESCALATION_TITLES: dict[str, str] = {
    ESCALATION_OVERLAP_HORIZON_EXPIRED: "Improvement blocked past its monitoring-overlap horizon",
    ESCALATION_LEASE_CONTENTION_EXHAUSTED: "Improvement blocked by apply lease contention",
    ESCALATION_SOFT_STATE_INVALID: "Improvement deferral state invalid",
}
_DEFAULT_ESCALATION_TITLE = "Improvement apply attempts exhausted"


def escalation_title(reason: str) -> str:
    return _ESCALATION_TITLES.get(reason, _DEFAULT_ESCALATION_TITLE)


def _escalate_to_failed(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
    notify_fn: NotifyFn | None = None,
) -> bool:
    try:
        store.transition_improvement(
            improvement_id,
            ImprovementStatus.APPROVED.value,
            ImprovementStatus.FAILED.value,
            actor="worker_drive:escalate",
            detail_json={"reason": reason, **(detail or {})},
        )
    except TransitionConflict:
        return False
    store.update_improvement_fields(
        improvement_id,
        last_error_json=json.dumps(
            {
                "kind": "redrive_exhausted",
                "reason": reason,
                "detail": detail or {},
                "at": utc_now_iso(),
            }
        ),
        resolved_at=utc_now_iso(),
    )
    if notify_fn is not None:
        try:
            notify_fn(
                kind="escalation",
                title=escalation_title(reason),
                severity="critical",
                imp_id=improvement_id,
                dedupe=True,
                payload={"reason": reason, **(detail or {})},
            )
        except Exception:  # noqa: BLE001 — notify must not break redrive
            pass
    else:
        _default_critical_notify(store, improvement_id, reason=reason, detail=detail or {})
    return True


def _default_critical_notify(
    store: ReliabilityStore,
    improvement_id: str,
    *,
    reason: str,
    detail: dict[str, Any],
) -> None:
    try:
        from omniagentos.notifications.service import record_notification

        conn = getattr(store, "_connection", None)
        record_notification(
            kind="escalation",
            title=escalation_title(reason),
            severity="critical",
            ref_type="improvement",
            ref_id=improvement_id,
            payload={"reason": reason, **detail},
            connection=conn,
            push=False,
            dedupe=True,
        )
    except Exception:  # noqa: BLE001
        pass


def redrive_stranded_approvals(
    store: ReliabilityStore,
    *,
    spawn_fn: SpawnFn | None = None,
    apply_fn: ApplyFn | None = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    owner: str = "redrive",
    clock: ClockFn | None = None,
    notify_fn: NotifyFn | None = None,
    pid_alive_fn: PidAliveFn | None = None,
    lease_seconds: int = DEFAULT_REDRIVE_LEASE_SECONDS,
) -> dict[str, Any]:
    """Idempotent CAS/lease-guarded re-driver for stranded ``approved`` rows (H-07).

    Exactly one concurrent reconciler holds ``reliability:redrive``. For each
    approved improvement past *grace_seconds*:
      * capture exit evidence if a prior PID is gone;
      * skip while a worker PID is still alive;
      * re-invoke *apply_fn* (in-process) or *spawn_fn* (detached) when idle;
      * leave deferred (lease/overlap) rows approved for a later pass;
      * escalate to ``failed`` + critical notification when HARD attempts exceed
        *max_attempts*, or when a soft deferral outlives its bounded horizon.

    *max_attempts* bounds failure evidence only. Recognised soft deferrals
    (``overlap_with_monitoring``, ``apply_lease_held``) are bounded by their own durable
    horizon instead, so a 24–72 h observation window is not mistaken for three broken
    applies.

    Returns a structured summary suitable for CLI/audit logging.
    """
    if spawn_fn is None and apply_fn is None:
        raise ValueError("redrive requires spawn_fn and/or apply_fn")

    summary: dict[str, Any] = {
        "checked": 0,
        "skipped_live": 0,
        "skipped_grace": 0,
        "redriven": 0,
        "deferred": 0,
        "soft_deferred": 0,
        "soft_escalated": 0,
        "applied": 0,
        "escalated": 0,
        "spawn_failed": 0,
        "errors": [],
        "lease_held": False,
    }

    try:
        token = store.acquire_lease(REDRIVE_LEASE_KEY, owner=owner, duration_seconds=lease_seconds)
    except LeaseConflict:
        summary["lease_held"] = True
        return summary

    now = _now(clock)
    try:
        approved = store.list_improvements(status=ImprovementStatus.APPROVED.value, limit=200)
        for imp in approved:
            summary["checked"] += 1
            try:
                _redrive_one(
                    store,
                    imp,
                    summary=summary,
                    spawn_fn=spawn_fn,
                    apply_fn=apply_fn,
                    grace_seconds=grace_seconds,
                    max_attempts=max_attempts,
                    now=now,
                    notify_fn=notify_fn,
                    pid_alive_fn=pid_alive_fn,
                )
            except Exception as exc:  # noqa: BLE001 — one row never aborts the sweep
                summary["errors"].append({"id": imp.id, "error": str(exc)})
    finally:
        try:
            store.release_lease(REDRIVE_LEASE_KEY, owner, token)
        except Exception:  # noqa: BLE001
            pass
    return summary


def _redrive_one(
    store: ReliabilityStore,
    imp: Any,
    *,
    summary: dict[str, Any],
    spawn_fn: SpawnFn | None,
    apply_fn: ApplyFn | None,
    grace_seconds: int,
    max_attempts: int,
    now: datetime,
    notify_fn: NotifyFn | None,
    pid_alive_fn: PidAliveFn | None,
) -> None:
    state = capture_exit_evidence(store, imp.id, pid_alive_fn=pid_alive_fn)
    if state.get("running") and state.get("pid"):
        try:
            if (pid_alive_fn or default_pid_alive)(int(state["pid"])):
                summary["skipped_live"] += 1
                return
        except (TypeError, ValueError):
            pass

    if not _past_grace(imp, state, grace_seconds=grace_seconds, now=now):
        summary["skipped_grace"] += 1
        return

    # Refresh — another worker may have moved the row.
    current = store.get_improvement(imp.id)
    if current is None or current.status != ImprovementStatus.APPROVED.value:
        return

    # Hard budget: real failure evidence only. ``attempt`` counts journaled applies that
    # entered the repo and failed; ``hard_attempts`` counts spawn/worker failures. Soft
    # deferrals never reach either, which is the whole point of H-07.
    attempts = max(int(getattr(current, "attempt", 0) or 0), hard_attempt_count(state))
    if attempts >= max_attempts:
        if _escalate_to_failed(
            store,
            imp.id,
            reason="attempts_exhausted",
            detail={
                "attempts": attempts,
                "hard_attempts": hard_attempt_count(state),
                "spawn_attempts": state.get("spawn_attempts"),
                "last_outcome": state.get("outcome"),
                "last_reason": state.get("last_reason") or state.get("last_deferred_reason"),
                "exit_code": state.get("exit_code"),
                "log_summary": (state.get("log_summary") or "")[-500:],
            },
            notify_fn=notify_fn,
        ):
            put_worker_state(store, imp.id, outcome=OUTCOME_ESCALATED, running=False)
            summary["escalated"] += 1
        return

    # Soft deferral in flight: honour the bounded horizon, or fail VISIBLY when it has
    # run out / the record is unusable. An expired persisted soft horizon performs a
    # final live probe before escalation: if the blocker cleared, apply normally; if
    # the live probe defers again, escalate with truthful class/horizon state.
    verdict = evaluate_soft_deferral(state.get("soft_deferral"), now=now)
    if verdict is not None:
        if verdict.state == SoftDeferralVerdict.INVALID:
            if _escalate_to_failed(
                store,
                imp.id,
                reason=verdict.reason,
                detail={**verdict.detail, "now": _iso(now), "verdict": verdict.state},
                notify_fn=notify_fn,
            ):
                put_worker_state(
                    store, imp.id, outcome=OUTCOME_ESCALATED, running=False, soft_deferral=None
                )
                summary["escalated"] += 1
                summary["soft_escalated"] += 1
            return

        if verdict.state == SoftDeferralVerdict.EXPIRED:
            soft_rec = state.get("soft_deferral")
            last_at_iso = soft_rec.get("last_at") if isinstance(soft_rec, dict) else None
            last_at_dt = _parse_iso(last_at_iso)
            deadline_iso = verdict.detail.get("deadline") or (
                soft_rec.get("horizon") if isinstance(soft_rec, dict) else None
            )
            deadline_dt = _parse_iso(deadline_iso)

            is_fresh = (
                last_at_dt is not None and deadline_dt is not None and last_at_dt >= deadline_dt
            )
            if is_fresh:
                if _escalate_to_failed(
                    store,
                    imp.id,
                    reason=verdict.reason,
                    detail={
                        **verdict.detail,
                        "now": _iso(now),
                        "verdict": verdict.state,
                        "final_probe": "none_fresh_record",
                    },
                    notify_fn=notify_fn,
                ):
                    put_worker_state(
                        store, imp.id, outcome=OUTCOME_ESCALATED, running=False, soft_deferral=None
                    )
                    summary["escalated"] += 1
                    summary["soft_escalated"] += 1
                return

    # Prefer in-process apply (tests / CLI redrive --inline); else re-spawn.
    # NOTE: a waiting soft deferral still probes every cycle. Probing is how a released
    # overlap is detected promptly, it is guarded by the same CAS/lease single-flight,
    # and it no longer costs anything from the budget.
    if apply_fn is not None:
        apply_attempts = int(state.get("apply_attempts") or 0) + 1
        put_worker_state(
            store,
            imp.id,
            apply_attempts=apply_attempts,
            last_redrive_at=_iso(now),
            outcome="redriving",
        )
        try:
            result = apply_fn(imp.id)
        except Exception as exc:  # noqa: BLE001
            record_worker_exit(
                store,
                imp.id,
                exit_code=1,
                outcome=OUTCOME_FAILED,
                reason="apply_failed",
                error=str(exc),
                now=now,
            )
            summary["redriven"] += 1
            return

        applied = bool(getattr(result, "applied", False))
        deferred = bool(getattr(result, "deferred", False))
        reason = str(getattr(result, "reason", "") or "")
        sha = getattr(result, "applied_sha", None)
        if applied:
            record_worker_exit(
                store,
                imp.id,
                exit_code=0,
                outcome=OUTCOME_APPLIED,
                reason=reason or "applied",
                applied_sha=sha,
                now=now,
            )
            summary["applied"] += 1
            summary["redriven"] += 1
            return
        if deferred:
            record_worker_exit(
                store,
                imp.id,
                exit_code=0,
                outcome=OUTCOME_DEFERRED,
                reason=reason or "deferred",
                deferred=True,
                now=now,
            )
            summary["deferred"] += 1
            summary["redriven"] += 1
            if classify_deferral(reason) is not None:
                summary["soft_deferred"] += 1

            # If we probed because of an EXPIRED verdict, re-evaluate the refreshed record.
            if verdict is not None and verdict.state == SoftDeferralVerdict.EXPIRED:
                refreshed_state = get_worker_state(store, imp.id)
                refreshed_verdict = evaluate_soft_deferral(
                    refreshed_state.get("soft_deferral"), now=now
                )
                if (
                    refreshed_verdict is not None
                    and refreshed_verdict.state == SoftDeferralVerdict.EXPIRED
                ):
                    if _escalate_to_failed(
                        store,
                        imp.id,
                        reason=refreshed_verdict.reason,
                        detail={
                            **refreshed_verdict.detail,
                            "now": _iso(now),
                            "verdict": refreshed_verdict.state,
                            "final_probe": "deferred",
                        },
                        notify_fn=notify_fn,
                    ):
                        put_worker_state(
                            store,
                            imp.id,
                            outcome=OUTCOME_ESCALATED,
                            running=False,
                            soft_deferral=None,
                        )
                        summary["escalated"] += 1
                        summary["soft_escalated"] += 1
            return
        record_worker_exit(
            store,
            imp.id,
            exit_code=1,
            outcome=OUTCOME_FAILED,
            reason=reason or "apply_failed",
            error=reason,
            now=now,
        )
        summary["redriven"] += 1
        return

    assert spawn_fn is not None
    command = str(state.get("command") or "apply")
    try:
        meta = spawn_fn(command, imp.id)
    except Exception as exc:  # noqa: BLE001
        record_spawn_failure(store, imp.id, error=str(exc))
        summary["spawn_failed"] += 1
        summary["redriven"] += 1
        # Escalate immediately if this failure exhausted the budget. Counted on
        # hard_attempts, not spawn_attempts: re-spawning after a legitimate soft
        # deferral must not be indistinguishable from a spawn that never started.
        refreshed = get_worker_state(store, imp.id)
        if hard_attempt_count(refreshed) >= max_attempts:
            if _escalate_to_failed(
                store,
                imp.id,
                reason="spawn_attempts_exhausted",
                detail={
                    "error": str(exc)[:500],
                    "hard_attempts": hard_attempt_count(refreshed),
                    "spawn_attempts": refreshed.get("spawn_attempts"),
                },
                notify_fn=notify_fn,
            ):
                put_worker_state(store, imp.id, outcome=OUTCOME_ESCALATED)
                summary["escalated"] += 1
        return

    summary["redriven"] += 1
    put_worker_state(
        store,
        imp.id,
        last_redrive_at=_iso(now),
        pid=meta.get("pid"),
        log=meta.get("log"),
        outcome=OUTCOME_SPAWNED,
        running=True,
    )


# --- Production spawn + scheduled re-drive wiring (H-06/H-07) ---------------

WORKER_MODULE = "omniagentos.reliability"
WORKER_COMMANDS = frozenset({"apply", "rollback"})
REDRIVE_OWNER_PREFIX = "reliability-redrive"


def product_root() -> Path:
    """Repo root that owns ``omniagentos/`` (…/omniagentos/reliability → parents[2])."""
    return Path(__file__).resolve().parents[2]


def worker_python(root: Path) -> str:
    """Prefer the running interpreter so package imports match the parent process (H-06)."""
    if sys.executable:
        return sys.executable
    venv_py = root / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return "python3"


def worker_home(root: Path) -> Path:
    """Runtime home for worker logs — the isolated ``OMNIAGENTOS_HOME`` when set."""
    home = os.environ.get("OMNIAGENTOS_HOME")
    return Path(home) if home else root


def worker_log_path(command: str, improvement_id: str, home: Path) -> Path:
    return home / "var" / "log" / f"reliability-{command}-{improvement_id}.log"


def store_db_path(store: ReliabilityStore) -> str | None:
    """The sqlite file behind *store*, so a re-driven worker writes the same db."""
    conn = getattr(store, "_connection", None)
    if conn is None:
        return None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:  # noqa: BLE001 — an unreadable pragma just means "fall back"
        return None
    for row in rows:
        try:
            name = row["name"] if hasattr(row, "keys") else row[1]
            file = row["file"] if hasattr(row, "keys") else row[2]
        except (KeyError, IndexError, TypeError):
            continue
        if name == "main" and file:
            return str(file)
    return None


def make_worker_spawn_fn(
    store: ReliabilityStore,
    *,
    repo_root: str | Path | None = None,
    db_path: str | None = None,
) -> SpawnFn:
    """Detached worker spawn with H-06 semantics, shared by the CLI and the re-driver.

    Same contract as the approve/apply route: the current interpreter, ``-m
    omniagentos.reliability``, a product-root cwd so the package imports, and a durable
    combined stdout/stderr log. The spawn attempt is recorded BEFORE ``Popen`` so a crash
    mid-spawn still leaves a trail; ``Popen`` failures propagate so the caller records
    durable spawn-failure evidence instead of reporting a success that never happened.
    """
    root = Path(repo_root).resolve() if repo_root else product_root()
    home = worker_home(root)
    db = str(db_path or store_db_path(store) or default_db_path())

    def _spawn(command: str, improvement_id: str) -> dict[str, Any]:
        if command not in WORKER_COMMANDS:
            raise ValueError(f"unsupported worker command: {command}")
        python_exe = worker_python(root)
        env = os.environ.copy()
        env.setdefault("OMNIAGENTOS_HOME", str(home))
        env["OMNIAGENTOS_DB"] = db
        log_path = worker_log_path(command, improvement_id, home)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            python_exe,
            "-m",
            WORKER_MODULE,
            command,
            "--improvement",
            improvement_id,
            "--db",
            db,
            "--repo-root",
            str(root),
        ]
        record_spawn_attempt(
            store,
            improvement_id,
            command=command,
            python=python_exe,
            cwd=str(root),
            log_path=str(log_path),
            argv=argv,
        )
        log_f = None
        try:
            log_f = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                argv,
                cwd=str(root),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            if log_f is not None:
                log_f.close()
        record_spawn_success(store, improvement_id, pid=proc.pid, log_path=str(log_path))
        return {
            "pid": proc.pid,
            "command": command,
            "python": python_exe,
            "module": WORKER_MODULE,
            "cwd": str(root),
            "log": str(log_path),
            "argv": argv,
        }

    return _spawn


def run_redrive_cycle(
    store: ReliabilityStore,
    *,
    repo_root: str | Path | None = None,
    db_path: str | None = None,
    spawn_fn: SpawnFn | None = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    owner: str | None = None,
    notify_fn: NotifyFn | None = None,
) -> dict[str, Any]:
    """One production re-drive pass — the scheduled/recovery hook for H-07.

    Reached from :func:`omniagentos.reliability.recovery.run_recovery_cycle`, which the
    launchd ``watch`` loop and the ``recover`` command both run, so a row stranded by a
    failed spawn, a dead worker, or a restart is re-driven WITHOUT a manual ``redrive``
    command. ``redrive`` stays available for out-of-band operator use.
    """
    return redrive_stranded_approvals(
        store,
        spawn_fn=spawn_fn or make_worker_spawn_fn(store, repo_root=repo_root, db_path=db_path),
        grace_seconds=grace_seconds,
        max_attempts=max_attempts,
        owner=owner or f"{REDRIVE_OWNER_PREFIX}:{os.getpid()}",
        notify_fn=notify_fn,
    )


def wait_briefly_for_pid(pid: int, *, timeout_s: float = 0.05) -> None:
    """Tiny cooperative pause (tests); production redrive is poll-based."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not default_pid_alive(pid):
            return
        time.sleep(0.01)
