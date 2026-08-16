"""Fire every DUE routine once, then settle finished routine runs.

This is the piece W5 (store + API + UI) deliberately left out: a routine on
its own is inert — a row that describes a trigger, a task template, an
objective gate, and a hard stop-condition, with no code reading it on a
schedule. This module is that code. Run it periodically (see
``scripts/scheduler/com.omniagentos.routines.plist.template`` — installed by
hand after review; a 5-minute cadence is the suggested default) and it:

1. loads every routine and asks :func:`omniagentos.scheduler.routines
   .should_fire` whether each one is due — that function is the single
   source of truth for "paused / over its hard cap / under the acceptance
   floor / trigger not due" and is pure/DB-free so it's unit-tested directly;
2. for anything DUE, re-validates the routine's declared shape with
   :func:`omniagentos.scheduler.routines.validate_routine` one more time
   (belt-and-braces: a routine missing its objective gate must NEVER fire,
   even if it somehow reached the table with one — e.g. a direct DB edit
   bypassing :class:`~omniagentos.scheduler.store.RoutinesStore`);
3. instantiates the routine's ``task_template`` into a task + queued run via
   the same :mod:`omniagentos.api.services` creation seam the HTTP API uses
   (:func:`create_task_service`, :func:`create_run_service`) — so a routine
   firing is indistinguishable, from the runner's point of view, from a
   human enqueuing the same work by hand;
4. records a ``routine_runs`` row for the firing (via
   :meth:`RoutinesStore.record_run`, which also rolls the firing into the
   routine's acceptance-rate/cost rollups and auto-pauses it if the floor is
   breached) and stamps ``last_fired`` (via
   :meth:`RoutinesStore.record_fired`) so the next tick's DUE-check knows
   this trigger has already been served; and
5. settles terminal runs against their objective gates. Templates without an
   explicit harness still default to ``mock`` and log a warning: real work
   should always declare its harness.

The gate itself (was the fired run's result actually accepted?) is judged on
a later tick, once the run finishes.

BUILT-IN AND LOOP TICKS GO THROUGH THE SAME SETTLEMENT
------------------------------------------------------
A built-in job (memlife's dream cycle, any LangGraph loop) executes inside this
process instead of being dispatched to a harness, so there is no ``runs`` row
for it. That used to mean there was no ``run_id`` either — and
``settle_pending`` requires one, so a built-in run could never be settled and
its routine's ``gate_config.command`` was decoration. The firing tick papered
over that by writing the job's OWN report into ``gate_passed`` and stamping
``finished_at`` immediately, which is how w3-health-monitor reached ten runs at
acceptance 1.0 with ten parks, zero heals, and a declared gate that had never
executed.

Now a built-in firing records a PENDING row with its own ``run_id`` and its
claim in ``self_reported_status`` (migration 104), and the settle pass at the
end of this same tick composes that claim with the verdict of the executed
gate. One settlement seam for both kinds of run — deliberately not a second
path, because two verdict paths that can disagree is its own class of incident.
A routine whose task/run could not even be created (e.g. a task_template
pointing at a discipline_id that no longer exists) still gets a
``routine_runs`` row, marked ``accepted=False``, so a persistently broken
template trips the same 50% auto-pause floor as a persistently rejected one,
rather than retrying forever with no record.

ONE SLOW PROJECT MUST NOT BLOCK EVERY OTHER PROJECT (M3)
--------------------------------------------------------
A built-in firing used to be *executed* in the middle of the DUE-loop, so the
tick was a strictly sequential pipeline: project B's loop was not dispatched
until project A's loop had finished, and a loop may legitimately run for its
whole ``timeout_s`` (up to an hour, ``loop_jobs.MAX_TIMEOUT_S``). With one
routine per project that is head-of-line blocking by construction — the slowest
project in the table sets the latency of every project behind it, and a loop
that always runs to its timeout starves the rest of the system indefinitely.

So the tick is now split into a CLAIM phase and an EXECUTE phase:

* the claim phase stays in the tick, stays sequential and stays fast — it is
  pure bookkeeping (validate, mint the ``run_id``, stamp ``last_fired``) and it
  is what makes a firing idempotent; and
* the execute phase is handed to a bounded pool
  (:data:`ENV_DISPATCH_CONCURRENCY`, default
  :data:`DEFAULT_DISPATCH_CONCURRENCY`), so every due project is dispatched
  within milliseconds of the one before it and slow loops overlap instead of
  queueing behind each other.

Three properties are deliberately preserved, because an async dispatch that
loses any of them is worse than the blocking it removes:

1. **No double-fire.** ``last_fired`` is stamped when the firing is CLAIMED,
   before the work starts — not when it finishes, as it used to be. A second
   tick that overlaps a long-running one (an operator running this module by
   hand while the launchd job is mid-flight is the realistic case) therefore
   sees the trigger as already served and skips it, instead of launching a
   second worker for the same loop instance.
2. **No lost settlement.** The pool is DRAINED before the settle pass, so a
   built-in fired by this tick is settled by this tick, exactly as before. The
   run row is still written by the same code with the same fields — only the
   thread it is written on changed, and ``SqliteStore`` hands every thread its
   own connection and serializes writes behind one lock.
3. **No premature settlement.** An in-flight firing has a ``run_id`` but no
   ``self_reported_status`` yet, and ``settle_pending`` only considers a row
   whose claim has landed (or whose ``runs`` row is terminal). A loop still
   running is therefore invisible to settlement rather than judged early.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from omniagentos.api.services import ApiError, create_run_service, create_task_service
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import Store, default_db_path, new_id
from omniagentos.db.store import SqliteStore
from omniagentos.intake.board_sweep import sweep_board
from omniagentos.policy import PolicyConfig, load_policy
from omniagentos.scheduler.builtin_jobs import BuiltinJob, BuiltinResult, builtin_for
from omniagentos.scheduler.routines import (
    OUTCOME_ADVERSE,
    OUTCOME_FAVOURABLE,
    OUTCOME_NEUTRAL,
    SELF_REPORT_COLUMN,
    RoutineValidationError,
    should_fire,
    validate_routine,
)
from omniagentos.scheduler.routines_settle import settle_pending, utc_clock
from omniagentos.scheduler.store import RoutinesStore, _count_settled_runs
from omniagentos.sessions.dal import SessionsDal

logger = logging.getLogger(__name__)

#: How many CLAIMED firings may execute at once (M3). Built-in work is either a
#: subprocess the pool thread only waits on (a loop tick) or a DB-bound job that
#: takes the store's writer lock for itself, so this bounds fan-out rather than
#: CPU: it is the number of projects that can be mid-loop simultaneously.
#: Eight is the number of loops a single operator box is expected to carry;
#: raising it costs one idle thread per extra slot.
DEFAULT_DISPATCH_CONCURRENCY = 8
#: Ceiling on the env override. A tick that spawns unbounded workers is a
#: different outage from the one this lane fixes, not an improvement on it.
MAX_DISPATCH_CONCURRENCY = 32
ENV_DISPATCH_CONCURRENCY = "OMNIAGENTOS_ROUTINE_DISPATCH_CONCURRENCY"


def _dispatch_concurrency() -> int:
    """Resolved pool width. Unparseable/out-of-range values fall back, loudly.

    ``1`` is a legitimate value: it restores strictly serial EXECUTION (though
    never serial claiming) for an operator who needs to bisect a loop that
    misbehaves only when it overlaps another.
    """
    raw = os.environ.get(ENV_DISPATCH_CONCURRENCY, "").strip()
    if not raw:
        return DEFAULT_DISPATCH_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d",
            ENV_DISPATCH_CONCURRENCY,
            raw,
            DEFAULT_DISPATCH_CONCURRENCY,
        )
        return DEFAULT_DISPATCH_CONCURRENCY
    if value < 1 or value > MAX_DISPATCH_CONCURRENCY:
        logger.warning(
            "%s=%d is outside 1..%d; using %d",
            ENV_DISPATCH_CONCURRENCY,
            value,
            MAX_DISPATCH_CONCURRENCY,
            DEFAULT_DISPATCH_CONCURRENCY,
        )
        return DEFAULT_DISPATCH_CONCURRENCY
    return value


@dataclass(frozen=True)
class _Firing:
    """The result of CLAIMING one firing (see the module docstring's M3 section).

    ``summary`` is the entry this firing contributes to ``tick()['fired']`` and
    is complete the moment the firing is claimed — a caller reading it never has
    to wait for the work. ``execute`` is the deferred half: ``None`` for a
    firing that finished inside the claim (a dispatched task, or a refusal),
    and otherwise the callable the dispatch pool runs to execute the built-in
    and record its run row.
    """

    summary: dict[str, Any]
    execute: Callable[[], None] | None = None


def _latest_event_ts(store: SqliteStore, event_type: str) -> str | None:
    """Newest recorded timestamp for *event_type*, or ``None`` if never seen.

    A raw parameterized read against the shared connection — the same escape
    hatch :mod:`omniagentos.briefing.run` and :class:`RoutinesStore` itself
    already use for one-off queries outside their owning DAL — rather than a
    new method on :mod:`omniagentos.db.store`, which is outside this
    package's ownership.
    """
    row = store._connection.execute(
        "SELECT MAX(ts) AS ts FROM events WHERE type = ?", (event_type,)
    ).fetchone()
    return None if row is None else row["ts"]


def _task_kwargs(
    task_template: dict[str, Any], routine_project_id: str | None = None
) -> dict[str, Any]:
    """Task-create kwargs for one firing of a routine.

    M1: the task inherits the ROUTINE's project (migration 116) unless the
    template names one itself. Template-wins is deliberate — the template is
    the more specific statement, and it is the field that already existed, so
    every routine written before this column keeps firing into exactly the
    project it did before. Neither source present means an unscoped task, not
    a defaulted one.
    """
    input_value = dict(task_template.get("input") or {})
    if task_template.get("task_mode"):
        input_value["task_mode"] = task_template["task_mode"]
    template_project_id = task_template.get("project_id")
    return {
        "title": task_template.get("title") or "Untitled routine task",
        "discipline_id": task_template.get("discipline_id"),
        "project_id": template_project_id or routine_project_id or None,
        "input": input_value,
        "acceptance": task_template.get("acceptance"),
        "risk": task_template.get("risk"),
        "tools_allowed": task_template.get("tools_allowed"),
    }


def _run_kwargs(task_template: dict[str, Any]) -> dict[str, Any]:
    if not task_template.get("harness"):
        logger.warning(
            "routine task templates SHOULD set harness for real work; defaulting to mock"
        )
    return {
        # mock is the safe default when a template doesn't declare a harness:
        # it never spends money or touches a real workspace.
        "harness": task_template.get("harness") or "mock",
        "arm": task_template.get("arm"),
        "model": task_template.get("model"),
        "plan": task_template.get("plan"),
        "budget": task_template.get("budget"),
        "prompt": task_template.get("prompt"),
    }


def _fire(
    store: Store,
    policy_cfg: PolicyConfig,
    routines: RoutinesStore,
    routine: dict[str, Any],
    *,
    now_iso: str,
) -> _Firing:
    """CLAIM one firing of *routine*. Always records a routine_runs row.

    Returns the firing's summary plus, for a built-in, the deferred callable
    that actually executes it (M3 — see the module docstring). Everything this
    function does itself is bookkeeping: it must stay fast, because it runs
    inside the DUE-loop and every project behind this one waits for it.
    """
    iteration = int(routine.get("total_runs") or 0) + 1
    task_template = routine.get("task_template") or {}

    # Built-in jobs: the template's input.module names a registered handler
    # (e.g. memlife dream cycle). Execute it in this process instead of
    # enqueueing a mock harness task. A declared module is an execution
    # contract: if no handler owns it, fail closed instead of letting the mock
    # harness counterfeit a successful firing. Templates with no module keep
    # using the normal dispatched-task path below.
    builtin = builtin_for(task_template)
    declared_module = (task_template.get("input") or {}).get("module")
    if declared_module is not None and builtin is None:
        # A refusal is not a dispatch: it is decided, recorded and finished
        # here, synchronously, exactly as it was before the pool existed.
        message = f"no built-in job registered for declared module {declared_module!r}"
        routines.record_run(
            routine["id"],
            {
                "iteration": iteration,
                "gate_passed": False,
                "accepted": False,
                "cost_usd": 0.0,
                "stop_reason": "fire_failed",
                "notes": message,
                "started_at": now_iso,
                "finished_at": now_iso,
                "outcome_class": OUTCOME_ADVERSE,
            },
        )
        routines.record_fired(routine["id"], now_iso)
        return _Firing(
            {
                "routine_id": routine["id"],
                "name": routine.get("name"),
                "fired": False,
                "reason": f"fire_failed: {message}",
            }
        )

    if builtin is not None:
        # THE CLAIM. `run_id` is minted and `last_fired` is stamped before the
        # work starts, so this trigger is served the instant the tick decides to
        # serve it. Stamping it afterwards (as this did while firing was
        # synchronous) leaves a window — now a window as long as the loop's
        # whole timeout — in which a second, overlapping tick sees the routine
        # as still due and launches a second worker for the same loop instance.
        run_id = new_id("btrun")
        routines.record_fired(routine["id"], now_iso)

        def execute() -> None:
            _execute_builtin(
                store,
                routines,
                routine,
                builtin,
                run_id=run_id,
                iteration=iteration,
                now_iso=now_iso,
            )

        return _Firing(
            {
                "routine_id": routine["id"],
                "name": routine.get("name"),
                "fired": True,
                "builtin": (task_template.get("input") or {}).get("module"),
                # A built-in run has an id now. It names no `runs` row (this
                # process executes the work itself) but it is what the evidence
                # record, the settlement claim and the audit trail are keyed by
                # — and it is known at CLAIM time, not at completion.
                "run_id": run_id,
                # This firing's work is running on the dispatch pool, not on the
                # tick's thread. `fired` above is about the trigger having been
                # served, which is settled here; the verdict is settled later,
                # by the gate, exactly as for every other kind of run.
                "dispatched": True,
            },
            execute,
        )

    try:
        task = create_task_service(
            store, policy_cfg, **_task_kwargs(task_template, routine.get("project_id"))
        )
        run = create_run_service(
            store,
            policy_cfg,
            task_id=task["id"],
            origin="scheduled",
            **_run_kwargs(task_template),
        )
    except (ApiError, ValueError) as exc:
        message = exc.message if isinstance(exc, ApiError) else str(exc)
        routines.record_run(
            routine["id"],
            {
                "iteration": iteration,
                "gate_passed": False,
                "accepted": False,
                "cost_usd": 0.0,
                "stop_reason": "fire_failed",
                "notes": f"could not instantiate task_template: {message}",
                "started_at": now_iso,
                "finished_at": now_iso,
            },
        )
        routines.record_fired(routine["id"], now_iso)
        return _Firing(
            {
                "routine_id": routine["id"],
                "name": routine.get("name"),
                "fired": False,
                "reason": f"fire_failed: {message}",
            }
        )

    routines.record_run(
        routine["id"],
        {
            "run_id": run["id"],
            "iteration": iteration,
            "cost_usd": 0.0,
            "stop_reason": "",
            "notes": "fired: task and run created; gate result pending",
            "started_at": now_iso,
        },
    )
    routines.record_fired(routine["id"], now_iso)
    return _Firing(
        {
            "routine_id": routine["id"],
            "name": routine.get("name"),
            "fired": True,
            "task_id": task["id"],
            "run_id": run["id"],
        }
    )


def _execute_builtin(
    store: Store,
    routines: RoutinesStore,
    routine: dict[str, Any],
    builtin: BuiltinJob,
    *,
    run_id: str,
    iteration: int,
    now_iso: str,
) -> None:
    """Execute one CLAIMED built-in firing and record its run row.

    Runs on a dispatch-pool thread, never on the tick's. That is safe by
    construction rather than by convention: :class:`~omniagentos.db.store
    .SqliteStore` resolves ``_connection`` per calling thread and serializes
    every write behind one process lock, and :class:`RoutinesStore` reads that
    property live for exactly this reason.

    The row it writes is byte-for-byte the row the synchronous version wrote,
    including ``started_at`` — which is the tick's instant, because that is when
    this firing was claimed.
    """
    try:
        result = builtin(store)
    except Exception as exc:  # noqa: BLE001
        result = BuiltinResult(False, f"builtin job raised: {exc}")
    outcome = result.outcome_class
    # A built-in tick has EXECUTED by the time it returns, but it has not been
    # JUDGED. Those are different facts and this row must keep them apart,
    # because conflating them is the defect an earlier lane closed: the firing
    # tick used to write the worker's own status into `gate_passed` and stamp
    # `finished_at` in the same INSERT, so the run was born settled,
    # `settle_pending` (the only code that executes `gate_config.command`)
    # structurally never saw it, and the declared gate of every loop in
    # production had never run once — while the rows read gate_passed=1. Live at
    # the time of writing: rtn_1e5567b9f3314a2c9d76, 10/10 "accepted", all of
    # them parks, run_id NULL on every one.
    #
    # So the row is recorded PENDING with a real run_id and the claim in its own
    # column, and settlement composes claim + executed gate. The verdict is
    # written by the same seam that judges dispatched runs — one settlement path,
    # never two that can disagree.
    #
    # It is also written HERE, when the work is done, and never at claim time:
    # `self_reported_status` is what makes a row a settlement candidate, so a row
    # carrying a claim its job has not made yet would be judged while the job is
    # still running.
    if routines.supports_self_report():
        routines.record_run(
            routine["id"],
            {
                "run_id": run_id,
                "iteration": iteration,
                # No gate_passed, no accepted, no finished_at, and no declared
                # outcome_class: a tick that has not been judged has none of
                # those, and writing any of them here is what made a self-report
                # look like a verdict.
                "cost_usd": 0.0,
                "stop_reason": result.stop_reason,
                "notes": result.notes,
                "started_at": now_iso,
                SELF_REPORT_COLUMN: result.self_reported_status,
            },
        )
    else:
        # Schema behind the code (migration 104 not yet applied): the claim
        # cannot reach settlement, so there is no gate and there never will be
        # one for this run. Settle it inline with EXACTLY the composition an
        # absent gate produces — a favourable claim with no evidence is not an
        # acceptance, a non-result is neutral, and a self-reported failure still
        # counts. Never the pre-104 behaviour of writing the claim into
        # gate_passed: a transitional window is precisely when a silent lie is
        # most expensive.
        verdict: bool | None = False if outcome == OUTCOME_ADVERSE else None
        routines.record_run(
            routine["id"],
            {
                "run_id": run_id,
                "iteration": iteration,
                "gate_passed": verdict,
                "accepted": verdict,
                "cost_usd": 0.0,
                "stop_reason": (
                    result.stop_reason
                    if outcome != OUTCOME_FAVOURABLE
                    else "gate_evidence_unavailable"
                ),
                "notes": (
                    f"{result.notes} — settled without a gate: this database "
                    "predates migration 104, so the tick's claim could not "
                    "reach settlement (excluded from the acceptance floor)"
                ),
                "started_at": now_iso,
                "finished_at": now_iso,
                "outcome_class": (OUTCOME_NEUTRAL if outcome == OUTCOME_FAVOURABLE else outcome),
            },
        )


def tick(
    store: Store,
    policy_cfg: PolicyConfig,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fire every DUE routine once. Returns a JSON-serializable summary.

    ``dry_run=True`` runs the exact same DUE-check (including the
    re-validation belt-and-braces) but stops short of instantiating a
    task/run or writing ``routine_runs``/``last_fired`` — useful for the
    launchd template's operators to sanity-check what a tick *would* do.

    The DUE-loop only ever CLAIMS a firing; built-in work executes on a bounded
    pool so no project waits for another project's loop (M3, module docstring).
    The pool is drained before the settle pass — a tick still returns having
    settled everything it fired, and it is still the caller's whole tick.
    """
    moment = now or datetime.now(UTC)
    # Derived from *moment* (not a fresh utc_now_iso() call) so an injected
    # `now` (tests, or a caller re-driving a missed tick) stays consistent
    # with what gets stamped as last_fired/started_at — otherwise a
    # test/backfill using a fixed `now` would never see its own fire
    # reflected in the next DUE-check.
    now_iso = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sqlite_store = cast(SqliteStore, store)
    routines = RoutinesStore(sqlite_store)
    fired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Board hygiene is deliberately independent of routine firing. A stale-card
    # cleanup failure must never prevent due routines from running.
    if not dry_run:
        try:
            db_row = sqlite_store._connection.execute("PRAGMA database_list").fetchone()
            db_path = str(db_row[2]) if db_row is not None else ""
            if db_path:
                sessions_dal = SessionsDal(db_path)
                try:
                    sweep_board(store, CollabStore(db_path), sessions_dal, now=moment)
                finally:
                    sessions_dal.close()
        except Exception:  # noqa: BLE001 -- scheduler availability takes precedence.
            logger.exception("board hygiene sweep failed")

    # The dispatch pool is created on first use and only ever holds built-in
    # work: a tick with nothing to execute spawns no threads at all.
    dispatcher: ThreadPoolExecutor | None = None
    in_flight: list[tuple[dict[str, Any], Future[None]]] = []

    try:
        for routine in routines.list_routines():
            event_name = (routine.get("trigger_config") or {}).get("event")
            # Compute settled-only acceptance rate to avoid poisoned persisted counters
            settled_runs, settled_accepted = _count_settled_runs(
                sqlite_store._connection, routine["id"], limit=100
            )
            fire, reason = should_fire(
                routine,
                now=moment,
                latest_event_ts=(
                    _latest_event_ts(sqlite_store, event_name)
                    if routine.get("trigger_type") == "event" and event_name
                    else None
                ),
                settled_runs=settled_runs,
                settled_accepted=settled_accepted,
            )
            if not fire:
                skipped.append(
                    {"routine_id": routine["id"], "name": routine.get("name"), "reason": reason}
                )
                continue

            # Belt-and-braces (see module docstring): NEVER fire a routine that
            # doesn't pass the same objective-gate + hard-cap + notification
            # validation every write path already enforces.
            try:
                validate_routine(routine)
            except RoutineValidationError as exc:
                skipped.append(
                    {
                        "routine_id": routine["id"],
                        "name": routine.get("name"),
                        "reason": f"invalid routine, refusing to fire: {'; '.join(exc.errors)}",
                    }
                )
                continue

            if dry_run:
                fired.append(
                    {
                        "routine_id": routine["id"],
                        "name": routine.get("name"),
                        "fired": False,
                        "dry_run": True,
                    }
                )
                continue

            firing = _fire(store, policy_cfg, routines, routine, now_iso=now_iso)
            fired.append(firing.summary)
            if firing.execute is None:
                continue
            # Submit and move straight on to the next routine. This one line is
            # the whole M3 fix: the loop below no longer waits for this loop
            # tick, so the next project is dispatched now rather than in an
            # hour.
            if dispatcher is None:
                dispatcher = ThreadPoolExecutor(
                    max_workers=_dispatch_concurrency(),
                    thread_name_prefix="routine-dispatch",
                )
            in_flight.append((firing.summary, dispatcher.submit(firing.execute)))

        # Drain BEFORE settling. Async execution is what removes the blocking;
        # skipping the drain would remove the settlement too, because a run row
        # that has not been written yet cannot be a settlement candidate — the
        # firing would sit unjudged until some later tick, which is the exact
        # invisibility this scheduler has already been burned by.
        for summary, future in in_flight:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 -- one firing, not the tick.
                # `_execute_builtin` already absorbs anything the job itself
                # raises, so reaching here means the RECORDING failed (a routine
                # deleted mid-tick, a store fault). Report it on the firing's own
                # summary and let every other project's firing stand.
                logger.exception(
                    "routine %s dispatch failed after firing", summary.get("routine_id")
                )
                summary["dispatch_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if dispatcher is not None:
            # Never `cancel_futures`: a claimed firing must reach its row.
            dispatcher.shutdown(wait=True)

    settled: dict[str, Any] | None = None
    if not dry_run:
        try:
            # `moment` is this tick's RECORD stamp and settlement keeps using it
            # as one. It must NOT double as the clock gate evidence is judged
            # against: settlement executes each gate, and a gate may run for
            # minutes, so by the time a receipt is judged `moment` is long past.
            # Production never pins `now`, so it gets a live clock read at each
            # judgement; a caller that DID pin one (a test, or a backfill
            # re-driving a missed tick) keeps its frozen clock, which is why the
            # pin is passed through rather than resolved here.
            settled = settle_pending(
                sqlite_store,
                now=moment,
                clock=None if now is not None else utc_clock,
            )
        except Exception:  # noqa: BLE001 -- settlement must never fail the firing tick.
            logger.exception("routine settlement failed")
            settled = {"checked": 0, "settled": [], "errors": ["settlement failed"]}

    return {
        "checked_at": now_iso,
        "dry_run": dry_run,
        "fired": fired,
        "skipped": skipped,
        "settled": settled,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would fire without writing anything"
    )
    args = parser.parse_args(argv)
    store = SqliteStore(default_db_path())
    policy_cfg = load_policy()
    result = tick(store, policy_cfg, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True, default=str))
    for entry in result["fired"]:
        if entry.get("fired"):
            logger.info("fired routine %s (%s)", entry["routine_id"], entry.get("name"))
        else:
            logger.warning(
                "routine %s failed to fire: %s", entry["routine_id"], entry.get("reason")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "tick"]
