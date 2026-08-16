"""Swarm run summary + Throughput Score (WP7).

One function per plan-doc bullet:

* :func:`compute_metrics` — pure mechanical arithmetic over the run's own
  rows (``swarm_runs``, ``board_tasks``/``swarm_deps``, ``swarm_attempts``,
  ``swarm.event``). Speedup and estimate-error are measured against the
  ``est_manual_minutes``/``est_agent_minutes`` FROZEN in ``swarm_runs.plan_json``
  at plan time — never against ``board_tasks.swarm_json`` (which can drift
  after a split/re-estimation) and never recomputed. This is the
  anti-gaming rule from the plan doc (Grok): if the summary let the planner's
  live estimates count, the planner could inflate ``est_manual`` after the
  fact and the score would game itself.
* :func:`est_completion` — remaining critical-path minutes for the C2 live
  overview tile; frozen ``est_agent_minutes`` only, no attempts/events needed.
* :func:`write_summary` — the terminal-status hook: mechanical vault note
  (always) + ONE optional Kimi narrative section (degrades to
  mechanical-only on ANY LLM failure) + best-effort knowledge ingest
  (discipline ``"swarm"``) + ``summary_note_path`` + a ``run_completed``/
  ``run_failed`` event carrying the score and note path. Every I/O step here
  is best-effort: ``write_summary`` itself never raises, because its only
  caller (the scheduler coordinator, at every terminal run status) must never
  be blocked or crashed by a summary failure.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, default_vault_dir, utc_now_iso
from omniagentos.swarm.contracts import (
    ACTION_PROVIDER_SWITCHED,
    ACTION_RATE_LIMIT_STALL,
    ACTION_RESIZE,
    ACTION_RUN_COMPLETED,
    ACTION_RUN_FAILED,
    SwarmEmitter,
)
from omniagentos.swarm.costgreen import effort_stats_as_dict, summarize_run
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT

LOG = logging.getLogger(__name__)

# --- Throughput Score formula constants (plan doc "WP7 — swarm/summary.py",
# frozen at plan time) ------------------------------------------------------
SPEEDUP_WEIGHT = 40.0
UTILIZATION_WEIGHT = 25.0
FIRST_ATTEMPT_WEIGHT = 25.0
STALL_WEIGHT = 10.0
ESTIMATE_ERROR_PENALTY_SLOPE = 30.0  # penalty = min(CAP, SLOPE * estimate_error)
ESTIMATE_ERROR_PENALTY_CAP = 15.0
SCORE_MIN = 0.0
SCORE_MAX = 100.0

# Terminal board-task statuses: a task here contributes nothing further to
# either the "done/blocked" bookkeeping or est_completion's remaining-work walk.
_NON_REMAINING_TASK_STATUSES = frozenset({"done", "blocked", "cancelled"})


# ---------------------------------------------------------------------------
# small parsing helpers (defensive: every field here is untrusted DB text)
# ---------------------------------------------------------------------------


def _parse_json(value: Any, *, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
    return default if parsed is None else parsed


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _frozen_tasks(run: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The FROZEN plan tasks, keyed by their plan-time id (``task_key``).

    Read ONLY from ``swarm_runs.plan_json`` — never from ``board_tasks.swarm_json``,
    which is live and can be mutated post-plan (splits, re-estimation). This is
    what makes the frozen-estimate rule hold: recomputing/mutating a task's
    live ``swarm_json`` estimates can never change a metric derived from here.
    """
    plan = _parse_json(run.get("plan_json"), default={})
    tasks = plan.get("tasks") if isinstance(plan, dict) else None
    if not isinstance(tasks, list):
        return {}
    return {str(task["id"]): task for task in tasks if isinstance(task, dict) and task.get("id")}


def _dependents_map(frozen_by_key: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    dependents: dict[str, list[str]] = {}
    for key, task in frozen_by_key.items():
        for dep in task.get("depends_on") or []:
            dependents.setdefault(str(dep), []).append(key)
    return dependents


def _member_tasks(dal: SwarmDal, run: Mapping[str, Any]) -> list[dict[str, Any]]:
    root_id = str(run.get("board_task_id") or "")
    return [task for task in dal.tasks_for_run(str(run["id"])) if str(task["id"]) != root_id]


def _task_key_of(task: Mapping[str, Any]) -> str | None:
    swarm_json = _parse_json(task.get("swarm_json"), default={})
    key = swarm_json.get("task_key") if isinstance(swarm_json, dict) else None
    return str(key) if key else None


# ---------------------------------------------------------------------------
# critical path (over the FROZEN plan DAG — bottleneck reporting)
# ---------------------------------------------------------------------------


def _critical_path_keys(frozen_by_key: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Longest chain (by frozen ``est_agent_minutes``) through the plan DAG."""
    if not frozen_by_key:
        return []
    dependents = _dependents_map(frozen_by_key)
    memo: dict[str, int] = {}

    def critical(key: str, trail: frozenset[str] = frozenset()) -> int:
        if key in memo:
            return memo[key]
        if key in trail or key not in frozen_by_key:
            return 0
        est = max(0, int(frozen_by_key[key].get("est_agent_minutes") or 0))
        downstream = max(
            (critical(child, trail | {key}) for child in dependents.get(key, [])),
            default=0,
        )
        memo[key] = est + downstream
        return memo[key]

    for key in frozen_by_key:
        critical(key)

    start = max(frozen_by_key, key=lambda k: (memo.get(k, 0), k))
    chain = [start]
    current = start
    seen = {start}
    while True:
        children = [c for c in dependents.get(current, []) if c not in seen]
        if not children:
            break
        nxt = max(children, key=lambda k: (memo.get(k, 0), k))
        chain.append(nxt)
        seen.add(nxt)
        current = nxt
    return chain


# ---------------------------------------------------------------------------
# 1 + 2. compute_metrics — mechanical metrics + Throughput Score
# ---------------------------------------------------------------------------


def _mean_target_n(
    resize_events: Sequence[Mapping[str, Any]],
    *,
    initial_target_n: int,
    started_at: datetime | None,
    finished_at: datetime,
) -> float:
    """Time-weighted mean ``target_n`` over the run's wall clock.

    Falls back to ``initial_target_n`` (the run row's ``target_concurrency``,
    itself defaulted from the frozen plan's ``target_n``) when there is no
    resize history or no ``started_at`` to anchor the timeline against.
    """
    initial_target_n = max(1, initial_target_n)
    if not resize_events or started_at is None:
        return float(initial_target_n)

    points: list[tuple[datetime, int]] = []
    for event in resize_events:
        ts = _parse_iso(event.get("ts"))
        payload = event.get("payload") or {}
        to_value = payload.get("to")
        if ts is None or to_value is None:
            continue
        try:
            points.append((ts, max(1, int(to_value))))
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda p: p[0])
    if not points:
        return float(initial_target_n)

    total_seconds = max(0.0, (finished_at - started_at).total_seconds())
    if total_seconds <= 0:
        return float(points[-1][1])

    weighted = 0.0
    cursor = started_at
    current_n = initial_target_n
    for ts, new_n in points:
        clamped = max(started_at, min(ts, finished_at))
        if clamped > cursor:
            weighted += current_n * (clamped - cursor).total_seconds()
            cursor = clamped
        current_n = new_n
    if finished_at > cursor:
        weighted += current_n * (finished_at - cursor).total_seconds()
    return weighted / total_seconds


def _throughput_score(
    *,
    speedup: float | None,
    parallelism_ratio: float,
    utilization: float | None,
    first_attempt_rate: float | None,
    rate_limit_stall_fraction: float | None,
    estimate_error_penalty: float | None,
) -> float:
    """``40*min(1, speedup/parallelism_ratio) + 25*utilization +
    25*first_attempt_rate + 10*(1 - rate_limit_stall_frac) - penalty``,
    clamped to [0, 100].

    An undefined observation contributes no points.  This is intentionally
    different from coercing the metric itself to zero: ``None`` remains
    visible in telemetry, while the score cannot turn missing evidence into
    either a success bonus or a fabricated failure.
    """
    ratio = parallelism_ratio if parallelism_ratio > 0 else 1.0
    speedup_term = SPEEDUP_WEIGHT * min(1.0, speedup / ratio) if speedup is not None else 0.0
    utilization_term = UTILIZATION_WEIGHT * utilization if utilization is not None else 0.0
    first_attempt_term = (
        FIRST_ATTEMPT_WEIGHT * first_attempt_rate if first_attempt_rate is not None else 0.0
    )
    stall_term = (
        STALL_WEIGHT * (1.0 - rate_limit_stall_fraction)
        if rate_limit_stall_fraction is not None
        else 0.0
    )
    score = (
        speedup_term
        + utilization_term
        + first_attempt_term
        + stall_term
        - (estimate_error_penalty if estimate_error_penalty is not None else 0.0)
    )
    return max(SCORE_MIN, min(SCORE_MAX, score))


def _ratio_or_none(
    numerator: float,
    denominator: float,
    *,
    observed: bool = True,
) -> float | None:
    """Return a measured ratio; an absent population/denominator is unknown."""
    if not observed or denominator <= 0:
        return None
    return numerator / denominator


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _optional_nonneg_float(value: Any) -> float | None:
    """Parse a numeric cost. ``None``/unparseable stay unknown — never coerced to 0.0.

    Explicit ``0`` / ``0.0`` is a real report ("cost nothing"). Bool is rejected
    because ``True``/``False`` are ``int`` subclasses. Non-finite and negative
    values are not valid usage and must not publish as measured spend.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _run_cost_usd(dal: SwarmDal, run_id: str, run: Mapping[str, Any]) -> float | None:
    """Three-valued total run cost for summary metrics and the vault Cost line.

    Only a parseable ``SOURCE_CLI_REPORT`` cost certifies a measured row.
    Tokens-only, ``SOURCE_NONE``, unparseable CLI cost, absent/NULL source
    (LEFT JOIN with no session), legacy ``estimator``, or any unrecognized
    ``usage_source`` means usage was not fully measured. Those cases stay
    unknown even when the run column still holds the legacy floor of ``0.0``
    — rendering that as ``Cost: $0.0000`` (or a partial measured floor as a
    complete total) is the defect class.
    """
    incomplete = False
    measured_total = 0.0
    measured_any = False
    reader = getattr(dal, "attempts_with_usage", None)
    rows = reader(run_id) if callable(reader) else dal.attempts_for_run(run_id)
    for row in rows:
        source = row.get("usage_source")
        if source == SOURCE_CLI_REPORT:
            parsed = _optional_nonneg_float(row.get("cost_usd"))
            if parsed is None:
                # CLI marker without a parseable cost cannot certify free/complete.
                incomplete = True
                continue
            measured_total += parsed
            measured_any = True
            continue
        # Every other evidence shape is incomplete for cost:
        # SOURCE_TOKENS_ONLY, SOURCE_NONE, absent/NULL (no session row),
        # legacy "estimator", or any unrecognized tag. Ignoring these rows
        # free-looked the run floor / partial sum as a complete total.
        incomplete = True
    if incomplete:
        return None
    if measured_any:
        return measured_total
    # No attempt rows at all: trust the run cache without ``or 0`` coercion.
    return _optional_nonneg_float(run.get("cost_usd"))


def compute_metrics(
    run_id: str,
    dal: SwarmDal,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure mechanical metrics for a run, persisted to ``swarm_runs.metrics_json``.

    Never touches an LLM and never recomputes an estimate — every
    speedup/estimate-error number is measured against the plan-time frozen
    ``est_manual_minutes``/``est_agent_minutes`` in ``swarm_runs.plan_json``.
    Returns ``{}`` (no-op, no write) when the run does not exist.
    """
    run = dal.get_run(run_id)
    if run is None:
        return {}

    frozen_by_key = _frozen_tasks(run)
    plan = _parse_json(run.get("plan_json"), default={})

    member_tasks = _member_tasks(dal, run)
    task_key_of_board_id = {str(t["id"]): _task_key_of(t) for t in member_tasks}
    status_by_board_id = {str(t["id"]): str(t.get("status") or "") for t in member_tasks}
    tasks_done = sum(1 for status in status_by_board_id.values() if status == "done")
    tasks_blocked = sum(1 for status in status_by_board_id.values() if status == "blocked")
    cancelled_real = sum(
        1
        for task in member_tasks
        if str(task.get("status") or "") == "cancelled"
        and not (_parse_json(task.get("swarm_json"), default={}) or {}).get("split")
    )
    completed_task_keys = {
        task_key_of_board_id.get(board_task_id) or board_task_id
        for board_task_id, status in status_by_board_id.items()
        if status == "done"
    }

    attempts = dal.attempts_for_run(run_id)
    attempts_by_board_id: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_board_id.setdefault(str(attempt["board_task_id"]), []).append(attempt)

    actual_minutes_by_key: dict[str, float] = {}
    retries_by_key: dict[str, int] = {}
    efforts_by_key: dict[str, list[str | None]] = {}
    tasks_with_attempts = 0
    first_attempt_completed = 0

    for board_task_id, task_attempts in attempts_by_board_id.items():
        key = task_key_of_board_id.get(board_task_id) or board_task_id
        ordered = sorted(task_attempts, key=lambda a: int(a.get("seq") or 0))
        total_seconds = 0.0
        for attempt in ordered:
            started = _parse_iso(attempt.get("started_at"))
            ended = _parse_iso(attempt.get("ended_at"))
            if started is not None and ended is not None and ended >= started:
                total_seconds += (ended - started).total_seconds()
        actual_minutes_by_key[key] = total_seconds / 60.0
        retries_by_key[key] = max(0, len(ordered) - 1)
        # Router-decided reasoning effort per attempt (048), in seq order.
        # None = pre-048 row or a provider running at session default.
        efforts_by_key[key] = [str(a["effort"]) if a.get("effort") else None for a in ordered]
        if ordered:
            tasks_with_attempts += 1
            if str(ordered[0].get("end_reason") or "") == "completed":
                first_attempt_completed += 1

    first_attempt_rate = _ratio_or_none(
        first_attempt_completed,
        tasks_with_attempts,
        observed=tasks_with_attempts > 0,
    )

    # --- wall time --------------------------------------------------------
    started_at = _parse_iso(run.get("started_at"))
    finished_at = _parse_iso(run.get("finished_at")) or (now or datetime.now(UTC))
    wall_seconds = max(0.0, (finished_at - started_at).total_seconds()) if started_at else 0.0
    wall_minutes = wall_seconds / 60.0

    # --- speedup (completed FROZEN est_manual_minutes vs actual wall) --------
    total_est_manual_minutes = sum(
        max(0, int(task.get("est_manual_minutes") or 0)) for task in frozen_by_key.values()
    )
    completed_est_manual_minutes = sum(
        max(0, int(task.get("est_manual_minutes") or 0))
        for key, task in frozen_by_key.items()
        if key in completed_task_keys
    )
    speedup = _ratio_or_none(
        completed_est_manual_minutes,
        wall_minutes,
        observed=tasks_done > 0,
    )

    parallelism_ratio = (
        float(plan.get("parallelism_ratio") or 0.0) if isinstance(plan, dict) else 0.0
    )
    if parallelism_ratio <= 0:
        parallelism_ratio = 1.0

    # Utilization measures how much of the plan's inherent parallelism was
    # actually realized: busy task-seconds / (wall * plan parallelism ratio).
    # Requested target_n remains telemetry, but is deliberately not the
    # denominator: lowering requested concurrency must never improve this rate.
    events = dal.events_for_run(
        run_id, actions=(ACTION_RESIZE, ACTION_RATE_LIMIT_STALL, ACTION_PROVIDER_SWITCHED)
    )
    resize_events = [e for e in events if e.get("action") == ACTION_RESIZE]
    initial_target_n = int(run.get("target_concurrency") or plan.get("target_n") or 1)
    mean_target_n = _mean_target_n(
        resize_events,
        initial_target_n=initial_target_n,
        started_at=started_at,
        finished_at=finished_at,
    )
    busy_slot_seconds = sum(actual_minutes_by_key.values()) * 60.0
    utilization = _ratio_or_none(
        busy_slot_seconds,
        wall_seconds * parallelism_ratio,
        observed=tasks_with_attempts > 0,
    )
    if utilization is not None:
        utilization = min(1.0, utilization)

    # --- rate-limit stall fraction + cooldown-wait seconds ------------------
    stall_events = [e for e in events if e.get("action") == ACTION_RATE_LIMIT_STALL]
    cooldown_wait_seconds = sum(
        float((e.get("payload") or {}).get("seconds") or 0.0) for e in stall_events
    )
    rate_limit_stall_fraction = _ratio_or_none(
        cooldown_wait_seconds,
        wall_seconds,
        observed=tasks_with_attempts > 0,
    )
    if rate_limit_stall_fraction is not None:
        rate_limit_stall_fraction = min(1.0, rate_limit_stall_fraction)

    provider_switches = sum(1 for e in events if e.get("action") == ACTION_PROVIDER_SWITCHED)

    # --- estimate error: median(|actual - est_agent| / est_agent) ----------
    errors: list[float] = []
    for key, task in frozen_by_key.items():
        est_agent = max(0, int(task.get("est_agent_minutes") or 0))
        actual = actual_minutes_by_key.get(key)
        if est_agent <= 0 or actual is None:
            continue
        errors.append(abs(actual - est_agent) / est_agent)
    estimate_error = statistics.median(errors) if errors else None
    estimate_error_penalty = (
        min(ESTIMATE_ERROR_PENALTY_CAP, ESTIMATE_ERROR_PENALTY_SLOPE * estimate_error)
        if estimate_error is not None
        else None
    )

    # --- cost + overshoot ---------------------------------------------------
    # Three-valued: tokens-only / NULL cost is unknown, never coerced to $0.00.
    # (``float(run.get("cost_usd") or 0.0)`` was the free-look counterfeit.)
    cost_usd = _run_cost_usd(dal, run_id, run)
    budget = run.get("budget_usd_max")
    budget_f = float(budget) if budget is not None else None
    if cost_usd is None:
        # Unknown spend cannot claim zero overshoot against a cap.
        cost_overshoot_usd: float | None = None
    elif budget_f is not None:
        cost_overshoot_usd = max(0.0, cost_usd - budget_f)
    else:
        cost_overshoot_usd = 0.0

    # --- cost-to-green by starting effort (retries/escalations included) -----
    # Production consumer of swarm.costgreen: surfaces per-green rates and the
    # measured/partial/sparse confidence band on every finished run's
    # metrics_json so sparse comparisons no longer look identical to measured.
    cost_to_green_by_effort = [effort_stats_as_dict(stats) for stats in summarize_run(dal, run_id)]

    # --- bottlenecks ---------------------------------------------------------
    critical_path_keys = _critical_path_keys(frozen_by_key)
    most_retried_key: str | None = None
    most_retried_count = 0
    if retries_by_key:
        candidate_key, candidate_count = max(retries_by_key.items(), key=lambda kv: (kv[1], kv[0]))
        if candidate_count > 0:
            most_retried_key, most_retried_count = candidate_key, candidate_count

    # --- task/run bookkeeping (partial flag) ---------------------------------
    run_status = str(run.get("status") or "")
    # Explicit None check: bare ``cost_usd >= budget`` TypeErrors; treating
    # unknown as not-hit is conservative for the partial flag only — the Cost
    # line still surfaces unknown, not a free $0.00.
    budget_hit = budget_f is not None and cost_usd is not None and cost_usd >= budget_f
    partial = bool(tasks_blocked or cancelled_real or budget_hit) or run_status == "failed"

    score = _throughput_score(
        speedup=speedup,
        parallelism_ratio=parallelism_ratio,
        utilization=utilization,
        first_attempt_rate=first_attempt_rate,
        rate_limit_stall_fraction=rate_limit_stall_fraction,
        estimate_error_penalty=estimate_error_penalty,
    )

    metrics: dict[str, Any] = {
        "run_id": run_id,
        "status": run_status,
        "partial": partial,
        "wall_seconds": round(wall_seconds, 3),
        "task_durations_minutes": {
            k: round(v, 3) for k, v in sorted(actual_minutes_by_key.items())
        },
        "task_efforts": {k: v for k, v in sorted(efforts_by_key.items())},
        "est_manual_minutes_total": total_est_manual_minutes,
        "speedup": _round_or_none(speedup, 4),
        "parallelism_ratio": round(parallelism_ratio, 4),
        "utilization": _round_or_none(utilization, 4),
        "mean_target_n": round(mean_target_n, 4),
        "first_attempt_rate": _round_or_none(first_attempt_rate, 4),
        "rate_limit_stall_fraction": _round_or_none(rate_limit_stall_fraction, 4),
        "estimate_error": _round_or_none(estimate_error, 4),
        "estimate_error_penalty": _round_or_none(estimate_error_penalty, 4),
        "cost_usd": _round_or_none(cost_usd, 6),
        "budget_usd_max": budget_f,
        "cost_overshoot_usd": _round_or_none(cost_overshoot_usd, 6),
        "cost_to_green_by_effort": cost_to_green_by_effort,
        "bottlenecks": {
            "critical_path_task_keys": critical_path_keys,
            "most_retried_task_key": most_retried_key,
            "most_retried_count": most_retried_count,
            "cooldown_wait_seconds": round(cooldown_wait_seconds, 3),
            "provider_switches": provider_switches,
        },
        "tasks_done": tasks_done,
        "tasks_blocked": tasks_blocked,
        "tasks_total": len(member_tasks),
        "score": round(score, 2),
    }
    dal.set_metrics(run_id, metrics)
    return metrics


# ---------------------------------------------------------------------------
# est_completion — remaining critical-path minutes (C2 live overview tile)
# ---------------------------------------------------------------------------


def est_completion(run_id: str, dal: SwarmDal | None = None) -> float:
    """Remaining critical-path minutes: FROZEN ``est_agent_minutes`` summed
    along the longest chain of NOT-YET-DONE tasks (open/claimed/in_progress —
    blocked/cancelled tasks contribute nothing further, they will not run).

    Uses only the frozen plan + current board status — no attempts/events
    needed, so this is cheap enough for a live overview tile to poll.
    """
    from omniagentos.contracts import default_db_path

    owns_dal = dal is None
    dal = dal or SwarmDal(default_db_path())
    try:
        run = dal.get_run(run_id)
        if run is None:
            return 0.0
        frozen_by_key = _frozen_tasks(run)
        if not frozen_by_key:
            return 0.0

        member_tasks = _member_tasks(dal, run)
        status_by_key: dict[str, str] = {}
        for task in member_tasks:
            key = _task_key_of(task)
            if key:
                status_by_key[key] = str(task.get("status") or "")

        pending = {
            key
            for key in frozen_by_key
            if status_by_key.get(key, "open") not in _NON_REMAINING_TASK_STATUSES
        }
        if not pending:
            return 0.0

        dependents = _dependents_map(frozen_by_key)
        memo: dict[str, float] = {}

        def remaining(key: str, trail: frozenset[str] = frozenset()) -> float:
            if key in memo:
                return memo[key]
            if key in trail or key not in pending:
                return 0.0
            est = max(0, int(frozen_by_key.get(key, {}).get("est_agent_minutes") or 0))
            downstream = max(
                (
                    remaining(child, trail | {key})
                    for child in dependents.get(key, [])
                    if child in pending
                ),
                default=0.0,
            )
            memo[key] = est + downstream
            return memo[key]

        return max((remaining(key) for key in pending), default=0.0)
    finally:
        if owns_dal:
            dal.close()


# ---------------------------------------------------------------------------
# 3. write_summary — vault note + narrative + knowledge ingest + event
# ---------------------------------------------------------------------------

FableRunner = Callable[..., dict[str, Any] | None]
KnowledgeIngestFn = Callable[[str, str], None]


def write_summary(
    run_id: str,
    *,
    dal: SwarmDal,
    emitter: SwarmEmitter | None = None,
    vault_dir: str | None = None,
    ledger_dir: str | None = None,
    fable_runner: FableRunner | None = None,
    knowledge_ingest: KnowledgeIngestFn | None = None,
) -> dict[str, Any]:
    """The WP7 terminal-status hook.

    Computes metrics + score, writes ``vault/swarm/<run_id>.md`` (mechanical
    sections always, ONE optional configured Kimi narrative section that
    degrades silently to mechanical-only on any LLM failure), appends the
    run's ledger manifest (one JSONL line in ``ledger/runs-<month>.jsonl``
    carrying the per-attempt provider/model/tier/effort/end_reason chain —
    the durable effort record outside the DB), best-effort knowledge ingest
    (discipline ``"swarm"``), persists ``summary_note_path``, and emits a
    ``run_completed``/``run_failed`` event carrying the score and note path.

    Never raises — this is called from the scheduler coordinator at every
    terminal run status and must never block or crash it.
    """
    try:
        return _write_summary_inner(
            run_id,
            dal=dal,
            emitter=emitter,
            vault_dir=vault_dir,
            ledger_dir=ledger_dir,
            fable_runner=fable_runner,
            knowledge_ingest=knowledge_ingest,
        )
    except Exception:  # noqa: BLE001 -- WP7 hook is best-effort by contract.
        LOG.warning("swarm write_summary failed for run %s", run_id, exc_info=True)
        return {}


def _write_summary_inner(
    run_id: str,
    *,
    dal: SwarmDal,
    emitter: SwarmEmitter | None,
    vault_dir: str | None,
    ledger_dir: str | None,
    fable_runner: FableRunner | None,
    knowledge_ingest: KnowledgeIngestFn | None,
) -> dict[str, Any]:
    run = dal.get_run(run_id)
    if run is None:
        return {}

    metrics = compute_metrics(run_id, dal)
    if not metrics:
        return {}

    # M-16/M-18/M-38 production closers on the real run-terminal path (scheduler
    # already calls write_summary; L16 must not edit scheduler.py — L10 owns it).
    # 1) Settle open TaskContract + CBM stamps from plain swarm_json data.
    # 2) Run fan-in over multi-attempt tasks so the pipeline is exercised default-path.
    try:
        status_early = str(run.get("status") or "")
        accepted_run = status_early == "completed"
        db_path = getattr(dal, "db_path", None)
        from omniagentos.swarm.settle_handoff import settle_run_tasks

        settle_results = settle_run_tasks(
            db_path=db_path,
            dal=dal,
            run_id=run_id,
            accepted=accepted_run,
            production_outcome="healthy" if accepted_run else "failed",
        )
        if settle_results:
            metrics["settle_handoff"] = settle_results
    except Exception:  # noqa: BLE001 — summary must never raise
        LOG.warning("swarm settle_handoff on summary failed for %s", run_id, exc_info=True)

    try:
        from omniagentos.swarm.summary_fanin import fanin_multi_attempt_tasks

        fanin_results = fanin_multi_attempt_tasks(dal=dal, run=run, metrics=metrics)
        if fanin_results:
            metrics["fanin_pipeline"] = fanin_results
    except Exception:  # noqa: BLE001
        LOG.warning("swarm fanin on summary failed for %s", run_id, exc_info=True)

    vault_root = vault_dir or default_vault_dir()
    note_path: str | None = None
    try:
        note_path = _write_vault_note(run_id, run, metrics, vault_root, fable_runner)
    except Exception:  # noqa: BLE001 -- the note is best-effort; metrics/event still land.
        LOG.warning("swarm vault note write failed for run %s", run_id, exc_info=True)

    manifest_path: str | None = None
    try:
        manifest_path = _append_ledger_manifest(run_id, run, metrics, dal, ledger_dir)
    except Exception:  # noqa: BLE001 -- the ledger line is best-effort like the note.
        LOG.warning("swarm ledger manifest write failed for run %s", run_id, exc_info=True)

    metrics["summary_note_path"] = note_path
    metrics["ledger_manifest_path"] = manifest_path
    try:
        dal.set_metrics(run_id, metrics)
        if note_path is not None:
            dal.set_summary_note_path(run_id, note_path)
    except Exception:  # noqa: BLE001
        LOG.warning("swarm metrics persist failed for run %s", run_id, exc_info=True)

    _safe_knowledge_ingest(run_id, run, metrics, ingest_fn=knowledge_ingest)

    status = str(run.get("status") or "")
    action = ACTION_RUN_COMPLETED if status == "completed" else ACTION_RUN_FAILED
    if emitter is not None:
        try:
            emitter.emit(
                run_id,
                action,
                {
                    "score": metrics.get("score"),
                    "summary_note_path": note_path,
                    "status": status,
                    "partial": metrics.get("partial"),
                },
            )
        except Exception:  # noqa: BLE001 -- emission is best-effort observability.
            LOG.debug("swarm summary emit failed for run %s", run_id, exc_info=True)

    return metrics


# --- ledger manifest (durable per-attempt effort record, 048) -----------------


def _append_ledger_manifest(
    run_id: str,
    run: Mapping[str, Any],
    metrics: Mapping[str, Any],
    dal: SwarmDal,
    ledger_dir: str | None,
) -> str:
    """Append the swarm run's terminal manifest to the append-only ledger.

    One ``RunManifest`` JSONL line (``omniagentos.ledger.append_manifest``,
    idempotent per run id) whose ``extra.attempts`` carries the full attempt
    chain — task_key, seq, provider, model, tier, router-decided ``effort``
    (048), and end_reason — so the decided effort levels are durably recorded
    outside the database, alongside every other lane's run manifests.
    """
    from omniagentos.contracts import (
        HarnessProfile,
        HarnessType,
        RunManifest,
        RunState,
        default_ledger_dir,
    )
    from omniagentos.ledger import append_manifest

    task_key_of_board_id = {str(t["id"]): _task_key_of(t) for t in _member_tasks(dal, run)}
    attempts = [
        {
            "task_key": task_key_of_board_id.get(str(a.get("board_task_id")))
            or str(a.get("board_task_id") or ""),
            "seq": int(a.get("seq") or 0),
            "provider": str(a.get("provider") or ""),
            "model": str(a.get("model") or ""),
            "tier": a.get("tier"),
            "effort": a.get("effort"),
            "end_reason": a.get("end_reason"),
        }
        for a in dal.attempts_for_run(run_id)
    ]
    status = str(run.get("status") or "")
    state = {
        "completed": RunState.COMPLETED,
        "cancelled": RunState.CANCELLED,
    }.get(status, RunState.FAILED)
    manifest = RunManifest(
        run_id=run_id,
        task_id=str(run.get("board_task_id") or run_id),
        discipline="swarm",
        harness=HarnessProfile(harness=HarnessType.SWARM),
        state=state,
        started_at=str(run.get("started_at") or "") or None,
        finished_at=str(run.get("finished_at") or "") or None,
        extra={
            "goal": str(run.get("goal") or ""),
            "score": metrics.get("score"),
            "partial": metrics.get("partial"),
            "cost_usd": metrics.get("cost_usd"),
            "attempts": attempts,
        },
    )
    return append_manifest(ledger_dir or default_ledger_dir(), manifest)


# --- vault note --------------------------------------------------------------


def _write_vault_note(
    run_id: str,
    run: Mapping[str, Any],
    metrics: Mapping[str, Any],
    vault_root: str,
    fable_runner: FableRunner | None,
) -> str:
    from omniagentos.vault import render_frontmatter, write_note
    from omniagentos.vault.paths import swarm_relpath

    body_lines = _mechanical_sections(run_id, run, metrics)
    narrative = _narrative_section(run_id, run, metrics, fable_runner)
    if narrative:
        body_lines += ["", "## Improvement opportunities (Kimi)", "", narrative]

    fm = VaultFrontmatter(
        id=run_id,
        type=NoteType.RUN,
        discipline="swarm",
        created=utc_now_iso(),
        source_run=run_id,
        status="active",
    )
    content = render_frontmatter(fm) + "\n" + "\n".join(body_lines) + "\n"
    return write_note(vault_root, swarm_relpath(run_id), content)


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"


def _fmt_per_green(value: Any, *, money: bool = False) -> str:
    """Format a three-valued per-green rate. ``None`` is unknown, never $0.00."""
    if value is None:
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if money:
        return f"${number:.4f}"
    return f"{number:.0f}"


def _cost_to_green_section(metrics: Mapping[str, Any]) -> list[str]:
    """Render cost-to-green buckets (confidence included) for the vault note."""
    buckets = metrics.get("cost_to_green_by_effort") or []
    if not buckets:
        return []
    lines = [
        "## Cost-to-green by starting effort",
        "",
    ]
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        effort = bucket.get("effort")
        label = effort if effort is not None else "(unset)"
        # Explicit None checks — bare truthiness would treat unknown as free.
        cost_pg = _fmt_per_green(bucket.get("cost_per_green"), money=True)
        tokens_pg = _fmt_per_green(bucket.get("tokens_per_green"))
        wall_pg = _fmt_per_green(bucket.get("wall_ms_per_green"))
        lines.append(
            f"- **{label}**: cost/green {cost_pg}, tokens/green {tokens_pg}, "
            f"wall_ms/green {wall_pg}, "
            f"green {bucket.get('green', 0)}/{bucket.get('tasks', 0)}, "
            f"confidence={bucket.get('confidence') or 'empty'}"
        )
    lines.append("")
    return lines


def _mechanical_sections(
    run_id: str, run: Mapping[str, Any], metrics: Mapping[str, Any]
) -> list[str]:
    goal = str(run.get("goal") or "").strip() or "(no goal recorded)"
    status = str(run.get("status") or "unknown")
    bottlenecks = metrics.get("bottlenecks") or {}
    # Three-valued overall cost: None → "unknown", never "$0.0000".
    # Measured free (0.0) still renders as $0.0000 via _fmt_per_green.
    cost_display = _fmt_per_green(metrics.get("cost_usd"), money=True)
    overshoot = metrics.get("cost_overshoot_usd")
    overshoot_suffix = ""
    if overshoot is not None:
        try:
            overshoot_f = float(overshoot)
        except (TypeError, ValueError):
            overshoot_f = 0.0
        if overshoot_f:
            overshoot_suffix = f" (overshoot ${overshoot_f:.4f})"

    lines = [
        f"# Swarm run {run_id}",
        "",
        f"Goal: {goal}",
        "",
        "## Result",
        "",
        f"- Status: **{status}**" + (" (partial)" if metrics.get("partial") else ""),
        f"- Throughput Score: **{metrics.get('score')}** / 100",
        f"- Wall time: {float(metrics.get('wall_seconds') or 0):.0f}s",
        f"- Speedup vs manual estimate: {float(metrics.get('speedup') or 0):.2f}x "
        f"(parallelism ratio {float(metrics.get('parallelism_ratio') or 0):.2f})",
        f"- Utilization: {_fmt_pct(metrics.get('utilization'))} "
        f"(mean concurrency {float(metrics.get('mean_target_n') or 0):.2f})",
        f"- First-attempt success rate: {_fmt_pct(metrics.get('first_attempt_rate'))}",
        f"- Rate-limit stall fraction: {_fmt_pct(metrics.get('rate_limit_stall_fraction'))}",
        f"- Estimate error (median): {_fmt_pct(metrics.get('estimate_error'))} "
        f"(score penalty: -{float(metrics.get('estimate_error_penalty') or 0):.1f})",
        f"- Cost: {cost_display}{overshoot_suffix}",
        f"- Tasks: {metrics.get('tasks_done', 0)} done / "
        f"{metrics.get('tasks_blocked', 0)} blocked / {metrics.get('tasks_total', 0)} total",
        "",
        *_cost_to_green_section(metrics),
        "## Bottlenecks",
        "",
        f"- Critical path: "
        f"{' -> '.join(bottlenecks.get('critical_path_task_keys') or []) or '(none)'}",
        f"- Most retried task: {bottlenecks.get('most_retried_task_key') or '(none)'} "
        f"({bottlenecks.get('most_retried_count', 0)} retries)",
        f"- Cooldown-wait: {float(bottlenecks.get('cooldown_wait_seconds') or 0):.0f}s",
        f"- Provider switches: {bottlenecks.get('provider_switches', 0)}",
    ]

    plan = _parse_json(run.get("plan_json"), default={})
    assumptions = plan.get("assumptions") if isinstance(plan, dict) else None
    if assumptions:
        lines += ["", "## Planner assumptions", ""]
        lines += [f"- {a}" for a in assumptions]

    return lines


def _narrative_section(
    run_id: str,
    run: Mapping[str, Any],
    metrics: Mapping[str, Any],
    fable_runner: FableRunner | None,
) -> str | None:
    """ONE bounded Kimi pass for improvement opportunities.

    Degrades to ``None`` (mechanical-only note) on ANY failure: an
    unavailable CLI, a non-dict/empty response, or an exception raised by an
    legacy-named injected ``fable_runner`` in tests.
    """
    runner = fable_runner
    if runner is None:
        from omniagentos.improvement_chain import run_kimi_json

        runner = run_kimi_json

    plan = _parse_json(run.get("plan_json"), default={})
    assumptions = plan.get("assumptions") if isinstance(plan, dict) else None
    bottlenecks = metrics.get("bottlenecks") or {}
    prompt = "\n".join(
        [
            "You are reviewing a completed OmniAgentOS swarm run for improvement",
            "opportunities. Be specific and concise (3-6 bullet points).",
            "",
            f"Goal: {run.get('goal') or ''}",
            f"Status: {run.get('status')}",
            f"Throughput Score: {metrics.get('score')}/100",
            f"Speedup: {metrics.get('speedup')}x vs manual "
            f"(parallelism ratio {metrics.get('parallelism_ratio')})",
            f"Utilization: {metrics.get('utilization')}",
            f"First-attempt rate: {metrics.get('first_attempt_rate')}",
            f"Estimate error (median): {metrics.get('estimate_error')}",
            f"Bottlenecks: {json.dumps(bottlenecks)}",
            f"Planner assumptions recorded at plan time: {json.dumps(list(assumptions or []))}",
            "",
            "Write 3-6 markdown bullet points on: what should change next run, and",
            "which planner assumptions (if any) proved wrong.",
            'Return JSON: {"narrative": "...markdown bullet list..."}',
        ]
    )
    schema = {
        "type": "object",
        "required": ["narrative"],
        "properties": {"narrative": {"type": "string"}},
    }
    try:
        raw = runner(prompt, schema, effort="medium", max_turns=3, wall_ms=180_000)
    except Exception:  # noqa: BLE001 -- degrade to mechanical-only on ANY LLM failure.
        LOG.warning("swarm summary narrative failed for run %s", run_id, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    narrative = raw.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return None
    return narrative.strip()


# --- knowledge ingest (best-effort seam, mirrors knowledge.runner_hook) -------


def _safe_knowledge_ingest(
    run_id: str,
    run: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    ingest_fn: KnowledgeIngestFn | None,
) -> None:
    content = (
        f"Swarm run {run_id} ({run.get('status')}): score {metrics.get('score')}/100, "
        f"speedup {metrics.get('speedup')}x, utilization {metrics.get('utilization')}, "
        f"first-attempt rate {metrics.get('first_attempt_rate')}, estimate error "
        f"{metrics.get('estimate_error')}. Goal: {run.get('goal') or ''}"
    )
    if ingest_fn is not None:
        try:
            ingest_fn(run_id, content)
        except Exception:  # noqa: BLE001 -- best-effort, even for an injected fake.
            LOG.debug("swarm knowledge ingest (injected) failed for run %s", run_id, exc_info=True)
        return

    # Merge-order-safe: an un-migrated/absent knowledge package must never
    # fail a run's terminal transition (mirrors knowledge.runner_hook).
    try:
        from omniagentos.knowledge import ingest as knowledge_ingest_module
        from omniagentos.knowledge.contracts import EpisodeSource
        from omniagentos.knowledge.recall import _get_store
    except Exception:  # noqa: BLE001
        return
    try:
        knowledge_ingest_module.ingest_episode(
            _get_store(),
            source=EpisodeSource.RUN.value,
            content=content,
            source_ref=run_id,
            discipline="swarm",
        )
    except Exception:  # noqa: BLE001
        LOG.debug("swarm knowledge ingest failed for run %s", run_id, exc_info=True)


__all__ = [
    "ESTIMATE_ERROR_PENALTY_CAP",
    "ESTIMATE_ERROR_PENALTY_SLOPE",
    "FIRST_ATTEMPT_WEIGHT",
    "SPEEDUP_WEIGHT",
    "STALL_WEIGHT",
    "UTILIZATION_WEIGHT",
    "compute_metrics",
    "est_completion",
    "write_summary",
]
