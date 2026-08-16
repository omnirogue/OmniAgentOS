"""Built-in routine jobs — the missing half of the routines engine.

The seeding half (``ensure_*_routine``) writes a row whose
``task_template.input.module`` names a real module. Until this package,
``routines_tick._fire`` only ever enqueued a mock task from that template.
A due built-in routine must **execute** the module it declares.

Lane A registered memlife only. ``improve.dispatcher`` and ``lab.jobs`` were
left unregistered on purpose ("spend money; residual R1"), which is one reason
the self-improvement loop went dark. Operator approval 2026-08-12 (the operator) revives
them under a $50/day hard cap and a free/cheap-model policy: the reflection
proposer now runs a FREE OpenRouter model (configs/loop_models.yaml) and all
OpenRouter spend is bounded at $50/UTC-day by the ``openrouter`` provider cap in
configs/spend-caps.yaml. Both jobs are therefore registered below. They are
independently spend-gated: ``improve.dispatcher`` is off until a human approves
improve-mode and carries its own daily ledger; ``lab.jobs`` only spends when a
job is actually queued.

HONESTY NOTE (F4, 2026-08-12 review): this revives reflection telemetry +
RECONCILIATION of already-queued work — it does NOT yet source NEW improvement
work. The dispatcher has no candidate loader wired, so it reconciles and
ingests but originates nothing; building a work source is a separate scope
decision for the operator. Do not describe this lane as "advancing new
improvement work".

LOOPS Phase 1 adds ``omniagentos.loops``, which launches one loop tick in its
own venv (see ``omniagentos/scheduler/loop_jobs.py``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from omniagentos.memlife.contracts import CycleStatus, EpisodicEvent
from omniagentos.memlife.dream import run_dream_cycle
from omniagentos.memlife.paths import memlife_store_root
from omniagentos.memlife.store import MemlifeStore
from omniagentos.scheduler.routines import (
    OUTCOME_ADVERSE,
    OUTCOME_CLASSES,
    OUTCOME_FAVOURABLE,
    OUTCOME_NEUTRAL,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuiltinResult:
    """What one built-in tick produced — in THREE values, never two.

    ``accepted`` alone cannot express the difference between "the tick did its
    job", "the tick behaved but there was nothing to do / it is waiting on a
    human", and "the tick could not proceed". Collapsing the middle case into
    either boolean is a defect in both directions: collapsing it into True made
    a loop that parked forever report 100% acceptance, and collapsing it into
    False auto-paused four of this repo's routines on 2026-07-31.

    So a built-in declares an :data:`~omniagentos.scheduler.routines.OUTCOME_NEUTRAL`
    outcome explicitly, together with a machine-readable ``reason`` that says
    WHICH non-result it was. ``routines_tick._fire`` turns that into a
    NULL/NULL run row, which every acceptance denominator in the system already
    excludes.

    Back-compatible by construction: ``BuiltinResult(accepted=..., notes=...)``
    still means favourable/adverse, so no existing built-in changes shape.
    """

    accepted: bool
    notes: str  # goes verbatim into routine_runs.notes
    #: Declared outcome class. ``None`` derives it from ``accepted``.
    outcome: str | None = None
    #: Machine-readable ``routine_runs.stop_reason``. Empty derives a default.
    reason: str = ""
    #: The executor's OWN status string, verbatim ("completed", "parked",
    #: "no_input", …). Migration 104 stores it beside — never instead of — the
    #: gate's verdict, so "the loop said it worked and the gate said otherwise"
    #: is a query rather than an archaeology exercise.
    self_report: str = ""

    def __post_init__(self) -> None:
        if self.outcome is not None and self.outcome not in OUTCOME_CLASSES:
            raise ValueError(f"unknown BuiltinResult outcome: {self.outcome!r}")
        if self.outcome == OUTCOME_NEUTRAL and self.accepted:
            # The whole point of the third state is that it is NOT acceptance.
            # A caller that sets both has re-created the collapse in one object.
            raise ValueError("a neutral outcome can never also be accepted")

    @property
    def outcome_class(self) -> str:
        """Resolved three-valued class. Never ``None``: a built-in has run."""
        if self.outcome is not None:
            return self.outcome
        return OUTCOME_FAVOURABLE if self.accepted else OUTCOME_ADVERSE

    @property
    def self_reported_status(self) -> str:
        """What this tick CLAIMED about itself — never empty.

        A built-in that names no status still reports its class, because the
        column's PRESENCE is what tells ``settle_pending`` that this run row was
        executed by the scheduler itself and has no ``runs`` row to join. An
        empty value would produce a row that is neither dispatched nor
        settleable, i.e. a run nobody ever judges — the exact shape this lane
        removes.
        """
        return self.self_report or self.outcome_class

    @property
    def stop_reason(self) -> str:
        """Resolved machine-readable stop reason for the run row."""
        if self.reason:
            return self.reason
        if self.outcome_class == OUTCOME_FAVOURABLE:
            return ""
        if self.outcome_class == OUTCOME_NEUTRAL:
            # Unreachable via the loop path (which always names its cause) but
            # a generic neutral must still be queryable as a non-result.
            return "loop_idle_no_work"
        return "builtin_failed"


# (store) -> result. Template-aware jobs (see _TEMPLATE_AWARE_JOBS) receive
# their routine's task_template as an already-bound keyword, so the shape the
# tick invokes stays exactly ``job(store)``.
BuiltinJob = Callable[..., BuiltinResult]


def _db_path_from_store(store: Any) -> str:
    """Resolve the DB path the tick is already bound to.

    Exact idiom at ``routines_tick.py`` board-sweep block: PRAGMA database_list
    so capture and scheduling never silently split across databases (F2).
    """
    row = store._connection.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    # sqlite3.Row and tuple both support index access.
    return str(row[2] if not hasattr(row, "keys") else row[2])


def _read_watermark(root: Path) -> datetime | str | None:
    """Lane B watermark helper when present; else full capture (since=None)."""
    try:
        from omniagentos.memlife.db import read_capture_watermark
    except ImportError:
        return None
    try:
        return read_capture_watermark(root)
    except Exception as exc:  # noqa: BLE001 — watermark fault must not kill the cycle
        logger.warning("read_capture_watermark failed: %s", exc)
        return None


def _max_archived_event_ts(root: Path) -> datetime | None:
    """Newest event timestamp in the post-cycle archive, if any."""
    archive = root / "episodic" / "archive.jsonl"
    if not archive.is_file():
        return None
    latest: datetime | None = None
    try:
        raw = archive.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = EpisodicEvent.model_validate_json(stripped)
        except Exception:  # noqa: BLE001
            continue
        ts = event.ts if event.ts.tzinfo is not None else event.ts.replace(tzinfo=UTC)
        if latest is None or ts > latest:
            latest = ts
    return latest


def _write_watermark(root: Path, ts: datetime | str) -> None:
    try:
        from omniagentos.memlife.db import write_capture_watermark
    except ImportError:
        return
    try:
        write_capture_watermark(root, ts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_capture_watermark failed: %s", exc)


def run_memlife_dream_cycle(store: Any) -> BuiltinResult:
    """Execute one memlife dream cycle for the due ``memlife-dream-cycle`` routine.

    Never raises — a memlife fault must not stop other routines from firing
    (same posture as the board-sweep guard in ``routines_tick``).
    """
    try:
        db_path = _db_path_from_store(store)
        root = memlife_store_root()
        MemlifeStore(root).ensure_layout()
        watermark = _read_watermark(root)
        # db_conn from the tick's store (under lock) so capture + candidate
        # writes share one database — B2 / F2. Without this, the dream cycle
        # stages filesystem-only and GET /api/memlife/queue stays at 0.
        lock = getattr(store, "_lock", None)
        conn = getattr(store, "_connection", None)
        if lock is not None and conn is not None:
            with lock:
                report = run_dream_cycle(
                    root,
                    db_path=db_path or None,
                    since=watermark,
                    db_conn=conn,
                )
        else:
            report = run_dream_cycle(
                root,
                db_path=db_path or None,
                since=watermark,
                db_conn=conn,
            )
        if report.status is CycleStatus.COMPLETED:
            max_ts = _max_archived_event_ts(root)
            if max_ts is not None:
                _write_watermark(root, max_ts)
        notes = (
            f"dream cycle {report.status.value}: staged={report.staged} "
            f"input={report.input_bytes}B kept={report.kept_bytes}B "
            f"archived={report.archived_bytes}B quarantined={report.quarantined_bytes}B "
            f"conserved={report.bytes_conserved} errors={len(report.errors)}"
        )
        if report.status is CycleStatus.NO_INPUT:
            # A quiet night is the same shape of non-result as an idle loop
            # tick: nothing to dream about is neither success nor failure.
            # It used to be scored ACCEPTED, for the right reason (three quiet
            # nights must not trip the floor) by the wrong mechanism — which
            # also meant a dream cycle that never saw input again looked
            # perfectly healthy at 100%.
            return BuiltinResult(
                accepted=False,
                notes=notes,
                outcome=OUTCOME_NEUTRAL,
                reason="loop_idle_no_work",
                self_report=report.status.value,
            )
        return BuiltinResult(
            accepted=report.status is CycleStatus.COMPLETED,
            notes=notes,
            self_report=report.status.value,
        )
    except Exception as exc:  # noqa: BLE001 — outer guard; never raise out of a builtin
        return BuiltinResult(accepted=False, notes=f"dream cycle raised: {exc}")


def run_improve_dispatcher_tick(store: Any) -> BuiltinResult:
    """Execute one improvement-lane dispatcher tick for the due routine.

    SI-loop revival (operator approval 2026-08-12). This tick RECONCILES the
    improvement lane's in-flight sagas and INGESTS any worker results — i.e. it
    revives reflection/reconciliation telemetry and advances work that is
    ALREADY queued. It does NOT source NEW improvement work: no candidate loader
    is wired (``tests=()``), so with an empty queue it reconciles and dispatches
    nothing (F4, 2026-08-12 review — building a work source is a pending scope
    decision for the operator, not part of this lane). Only when improve-mode is
    ``on`` (a human approval, off by default) can it dispatch, under the lane's
    own daily budget ledger.

    Never raises: a dispatcher fault must not stop other routines from firing
    (same posture as :func:`run_memlife_dream_cycle`).
    """
    try:
        from omniagentos.improve.dispatcher import GitCli, tick
        from omniagentos.runtime_paths import TOKEN_VAR_ENV_KEYS, resolve_var_root

        conn = store._connection
        repo_root = Path(__file__).resolve().parents[2]
        inbox = resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("improve", "inbox"))
        # tests=() by default: this tick reconciles + ingests + dispatches what
        # is already queued; it never invents new work. ledger=None so tick opens
        # its own durable day-cap ledger (see dispatcher._default_budget_ledger).
        result = tick(conn, refs=GitCli(repo_root), inbox=inbox)
        raw_mode = result.get("mode")
        mode = str(raw_mode) if raw_mode is not None else ""
        dispatched = result.get("dispatched") or []
        budget_blocked = result.get("budget_blocked") or []
        budget_overshot = bool(result.get("budget_overshot"))
        budget_unenforceable = bool(result.get("budget_unenforceable"))
        notes = (
            f"improve dispatcher tick: mode={mode or 'MISSING'!r} "
            f"reconciled={len(result.get('reconciled') or [])} "
            f"ingested={len(result.get('ingested') or [])} "
            f"dispatched={len(dispatched)} budget_blocked={len(budget_blocked)} "
            f"budget_overshot={budget_overshot} "
            f"budget_unenforceable={budget_unenforceable} "
            f"blocked_by={result.get('blocked_by')!r}"
        )
        # F3 (2026-08-12 Class-A review): a spend-gate failure is UNHEALTHY, never
        # a healthy tick. If the dispatcher could not enforce its own budget (the
        # cap was unreadable) or overshot it, record adverse so the scheduler's
        # acceptance floor SEES the budget-control failure — and keep the flags
        # in the receipt notes as evidence.
        if budget_unenforceable or budget_overshot:
            return BuiltinResult(
                accepted=False,
                notes=notes + " — spend gate FAILED (budget uncontrolled)",
                self_report=mode or "unknown",
            )
        if mode == "off":
            # Improve-mode is off until a human approves it; a tick that runs
            # while off did its job but produced no result — neither success
            # nor failure. Score it neutral so quiet ticks never trip the floor.
            return BuiltinResult(
                accepted=False,
                notes=notes,
                outcome=OUTCOME_NEUTRAL,
                reason="improve_mode_off",
                self_report=mode,
            )
        if mode in {"on", "shadow"}:
            return BuiltinResult(accepted=True, notes=notes, self_report=mode)
        # F3: unknown/missing mode is a malformed result — fail closed, never
        # let it read as a favourable run.
        return BuiltinResult(
            accepted=False,
            notes=notes + f" — unrecognized dispatcher mode {mode or 'MISSING'!r}",
            self_report=mode or "unknown",
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a builtin
        return BuiltinResult(accepted=False, notes=f"improve dispatcher tick raised: {exc}")


def run_lab_jobs_drain(store: Any) -> BuiltinResult:
    """Claim and run one queued lab job under an owner lease.

    SI-loop revival (operator approval 2026-08-12). Without a drain the
    ``/api/lab/run`` route enqueues durably and nothing ever advances the queue.
    Reuses the canonical lab store + protected evaluator wiring (lab.api.deps).

    Never raises: a lab fault must not stop other routines from firing.
    """
    try:
        from omniagentos.lab import jobs as lab_jobs
        from omniagentos.lab.api.deps import get_lab_store, get_protected_evaluator

        lab_store = get_lab_store()
        evaluator = get_protected_evaluator()
        stages = lab_jobs.campaign_stages(lab_store, evaluator)
        job = lab_jobs.run_once(lab_store, stages, owner="lab-jobs-drain")
        if job is None:
            # An empty queue is a non-result, exactly like a quiet dream cycle.
            return BuiltinResult(
                accepted=False,
                notes="lab jobs drain: no queued job to claim",
                outcome=OUTCOME_NEUTRAL,
                reason="loop_idle_no_work",
                self_report="no_input",
            )
        state = str(getattr(job, "state", "unknown"))
        return BuiltinResult(
            accepted=state == lab_jobs.SUCCEEDED,
            notes=f"lab jobs drain: job {getattr(job, 'job_id', '?')} -> {state}",
            self_report=state,
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a builtin
        return BuiltinResult(accepted=False, notes=f"lab jobs drain raised: {exc}")


# selfimprove-curator (system_jobs.py catalog entry) is DECLARED but was never
# registered as a runnable builtin, so a completed mission could never become a
# skill through the routines engine. Off by default: flipping the flag is a
# deliberate operator decision, not a side effect of this registration landing.
CURATOR_ENABLED_ENV = "OMNIAGENTOS_CURATOR_ENABLED"  # "1" -> curator tick runs


def _curator_enabled() -> bool:
    return os.getenv(CURATOR_ENABLED_ENV, "0") == "1"


def run_selfimprove_curator_tick(store: Any) -> BuiltinResult:
    """Execute one selfimprove-curator pass for the due routine.

    Guarded OFF by default (`OMNIAGENTOS_CURATOR_ENABLED=1` to enable) so
    nothing changes in production until an operator deliberately turns it on.
    When disabled the tick is a declared non-result, exactly like the other
    quiet-night builtins in this module — never a favourable or adverse run.

    Never raises: a curator fault must not stop other routines from firing
    (same posture as :func:`run_memlife_dream_cycle`).
    """
    if not _curator_enabled():
        return BuiltinResult(
            accepted=False,
            notes=f"selfimprove curator tick: disabled ({CURATOR_ENABLED_ENV} is not set)",
            outcome=OUTCOME_NEUTRAL,
            reason="curator_disabled",
            self_report="disabled",
        )
    try:
        from omniagentos.selfimprove.curator import curate_sessions

        result = curate_sessions()
        notes = (
            f"selfimprove curator tick: scanned={result.scanned} "
            f"captured={len(result.captured)} already_captured={len(result.already_captured)} "
            f"unverified={len(result.unverified)} proposals={len(result.proposals)} "
            f"pending_proposals={len(result.pending_proposals)} errors={len(result.errors)}"
        )
        return BuiltinResult(
            accepted=not result.errors,
            notes=notes,
            self_report="completed",
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a builtin
        return BuiltinResult(accepted=False, notes=f"selfimprove curator tick raised: {exc}")


def _run_loop_job(store: Any, *, task_template: dict[str, Any]) -> BuiltinResult:
    """Launch one LangGraph loop tick.

    Imported lazily because ``loop_jobs`` imports :class:`BuiltinResult` from
    here; a module-level import in both directions would be circular.
    """
    from omniagentos.scheduler.loop_jobs import run_loop_job

    return run_loop_job(store, task_template=task_template)


# Registry of task_template.input.module → job.
BUILTIN_JOBS: dict[str, BuiltinJob] = {
    "omniagentos.memlife.dream": run_memlife_dream_cycle,
    # Value pinned to loop_jobs.LOOP_MODULE by tests/scheduler/test_loop_jobs.py.
    "omniagentos.loops": _run_loop_job,
    # SI-loop revival (operator approval 2026-08-12, $50/day cap + free-model
    # policy). Previously left unregistered "on purpose (spend money; residual
    # R1)"; now spend-gated (improve-mode off by default; lab drains only queued
    # work) under the openrouter $50/UTC-day cap in configs/spend-caps.yaml.
    "omniagentos.improve.dispatcher": run_improve_dispatcher_tick,
    "omniagentos.lab.jobs": run_lab_jobs_drain,
    # Declared in scheduler/system_jobs.py's selfimprove-curator catalog entry;
    # registered here so a due routine can actually execute it. Off by default
    # (see CURATOR_ENABLED_ENV / run_selfimprove_curator_tick).
    "omniagentos.selfimprove.curator": run_selfimprove_curator_tick,
}

# Jobs whose behaviour depends on the ROW that fired them (a loop row names its
# template, instance and params). They receive ``task_template`` as a bound
# keyword; every other job keeps the frozen ``(store) -> BuiltinResult`` shape,
# so ``routines_tick._fire`` still calls ``builtin(store)`` unchanged.
_TEMPLATE_AWARE_JOBS: frozenset[str] = frozenset({"omniagentos.loops"})


def builtin_for(task_template: dict[str, Any]) -> BuiltinJob | None:
    """Return the registered job for this template, or None (→ normal firing)."""
    module = (task_template.get("input") or {}).get("module")
    if not module or not isinstance(module, str):
        return None
    job = BUILTIN_JOBS.get(module)
    if job is None:
        return None
    if module in _TEMPLATE_AWARE_JOBS:
        return partial(job, task_template=task_template)
    return job


__all__ = [
    "BUILTIN_JOBS",
    "CURATOR_ENABLED_ENV",
    "BuiltinJob",
    "BuiltinResult",
    "builtin_for",
    "run_improve_dispatcher_tick",
    "run_lab_jobs_drain",
    "run_memlife_dream_cycle",
    "run_selfimprove_curator_tick",
]
