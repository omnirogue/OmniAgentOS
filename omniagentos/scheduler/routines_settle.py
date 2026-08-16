"""Settle terminal routine runs against their objective gates.

Branch→gate_passed mapping:
- run FAILED/CANCELLED: False (run failed, gate not evaluated)
- self-reported ADVERSE: False (the tick reported its own failure — same rule as
  a FAILED run, keeping the machine-readable cause)
- gate_workspace is None: None (absence of configured workspace)
- outcome "refused": False (gate actively rejected evidence — a fact about the
  CANDIDATE: an unrecognised verifier, a target that does not exist, a
  manipulated execution)
- outcome "unavailable": None (absence of evidence: fetch failed, or the
  WORKSPACE was unusable — missing, not a checkout, or dirty. GateWorkspaceUnusable
  separates that class at the raise site, because a workspace that goes dirty
  between configuration and settlement judged nothing and must not be counted
  against the acceptance floor)
- outcome "evidence" + rejections: False (gate evaluated and failed)
- outcome "evidence" + no rejections: True (gate evaluated and passed)
- outcome unknown: None (absence of clear verdict)
- gate_type not in EXECUTED_GATE_TYPES: None (no trusted evidence path)

Only runs with gate_passed IS NOT NULL are counted by _count_settled_runs()
in the acceptance-floor denominator. None = excluded from floor.

COMPOSING A SELF-REPORT WITH AN EXECUTED GATE
---------------------------------------------
Built-in and loop ticks run inside the scheduler and have no ``runs`` row, so
they arrive here carrying their own account of themselves instead
(``routine_runs.self_reported_status``, migration 104; the class recovered from
the row's reason code by :func:`~omniagentos.scheduler.routines
.self_reported_outcome`). Two facts, two columns, composed by ONE rule:

    **A self-report may lower a verdict. It may never raise one.**

``gate_passed`` therefore only ever holds what an executed gate found, and
``accepted`` — the floor's numerator — is true only where a favourable claim
MET a passing gate:

===============  ============  ==============  ==========  ================
claim            gate          gate_passed     accepted    outcome_class
===============  ============  ==============  ==========  ================
favourable       passed        True            True        favourable
favourable       failed        False           False       adverse
favourable       absent        None            None        neutral
neutral          passed        True            None        neutral
neutral          failed        False           False       adverse
neutral          absent        None            None        neutral
adverse          not executed  False           False       adverse
===============  ============  ==============  ==========  ================

Read the rows that matter:

* **favourable + failed gate = ADVERSE.** This is the invariant that makes the
  verdict independent. A loop reporting ``completed`` whose declared gate goes
  red settles against the acceptance floor, and nothing the worker can say
  prevents it.
* **favourable + absent gate = NEUTRAL, never accepted.** A claim with no
  executed evidence is uncorroborated: loud (NULL/NULL is exactly what the
  health sentinel's ``gate_settlement`` streak check alarms on) and out of the
  denominator — never a success, never a failure.
* **neutral + passing gate keeps the evidence and drops the acceptance.** A
  park is still a park; the gate's verdict is recorded because it was really
  produced, while the run stays out of the denominator via the ``stop_reason``
  backstop in ``_count_settled_runs``.
* **adverse short-circuits the gate**, exactly as a FAILED dispatched run does:
  the tick already failed, there is nothing left to verify, and a self-reported
  failure is the one direction in which trusting the worker is safe.

WHERE A CLASS P (LIVE PROBE) VERDICT PLUGS IN — NOT IMPLEMENTED HERE
--------------------------------------------------------------------
Everything above is Class M: hermetic, mechanical, executed by
``PytestGateRunner`` in a throwaway worktree whose environment is sanitised down
to PATH/HOME/LANG/LC_ALL/TMPDIR/SYSTEMROOT (``gate_runner._sanitized_env``).
That sanitisation is *why* M evidence is trustworthy, and it is also why a probe
that must ask an outside authority — ``launchctl print``, a Gmail Sent search, a
``gh api`` commit list — structurally cannot be a pytest gate. Widening the
runner's environment to admit one would trade the hermetic property for the
probe, which is the wrong trade.

Class P therefore runs PARENT-SIDE, here, in this process, which already holds
the credentials legitimately (same argument as ``loop_jobs._deliver_page``: the
Slack webhook never crosses into the worker). The seam is deliberately shaped so
that adding it changes no other line of this module:

1. the branch chain above reduces to exactly three locals —
   ``gate_passed`` / ``stop_reason`` / ``notes``;
2. a Class P probe is one more ``elif`` producing the same three, placed AFTER
   the Class M block and gated on the routine declaring one (the natural home is
   a per-instance declaration beside the loop's ``register()``, resolved by
   ``instance_id``, never a command read from the row — a row is data);
3. the composition overlay below then applies to it unchanged, so a probe result
   meets the claim under the same "may lower, never raise" rule.

The one rule the probe MUST copy verbatim is the absence-vs-failure split at
``gate_evidence.GateWorkspaceUnusable`` (and its mapping to ``unavailable`` in
``produce_gate_evidence``): a probe that could not reach its authority — dead
credential, API change, network — is ABSENCE. It settles NULL/NULL and stays out
of the acceptance floor, loudly, exactly like a missing gate workspace. It must
never settle ``gate_passed=0``, because "we could not ask" is not "the answer was
no", and scoring it as failure is the defect that auto-paused four routines on
2026-07-31. Only a probe that reached its authority and got a negative answer
settles 0. The first probe to ship must arrive with that negative control:
revoke the credential, observe ``unavailable``, not ``failed``, not ``passed``.

TWO CLOCKS, AND WHY THEY ARE NOT THE SAME CLOCK
-----------------------------------------------
Settlement reads time for two unrelated purposes, and collapsing them into one
instant is what produced the 2026-08-01 false-adverse incident (routine_runs
741/742/744-749: ``gate failed: gate evidence is dated in the future`` on gates
whose own receipts say ``exit_code=0``, 20/20 checks passed).

* ``now`` / ``now_iso`` is the batch's LOGICAL reference instant. It is a
  RECORD STAMP: the fallback ``finished_at`` for a row that has no finish time
  of its own. It is deliberately stable across the whole batch so every row one
  pass settles carries one coherent stamp, and so a caller re-driving a missed
  tick with an injected ``now`` writes the stamps that tick would have written.

* ``clock`` is the PHYSICAL clock, and it is read AT THE MOMENT OF JUDGEMENT —
  after :func:`produce_gate_evidence` returns. Evidence freshness (stale beyond
  ``MAX_EVIDENCE_AGE_SECONDS``; forged, i.e. dated more than 60s in the future)
  is a question about real elapsed time, and since the merge that made
  ``settle_pending`` EXECUTE the gate, minutes of real time pass inside this
  function — a ``PytestGateRunner`` gate may run for its full 900s timeout and
  stamps its receipt with wall-clock at completion. Judging that receipt against
  the instant the batch started makes every gate slower than 60s look
  future-dated, i.e. forged. The anti-forgery direction and the 60s window are
  both correct and stay exactly as they are; the clock they are compared against
  is what was wrong.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import TERMINAL_RUN_STATES, RunState, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.gate_evidence import (
    MERGE_GATE_ROUTINE_ID,
    MERGE_GATE_TYPE,
    GateEvidenceStore,
    evidence_rejections,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    TrustedGateRunner,
    default_gate_runner,
    default_gate_workspace,
    produce_gate_evidence,
)
from omniagentos.scheduler.routines import (
    OUTCOME_ADVERSE,
    OUTCOME_NEUTRAL,
    SELF_REPORT_COLUMN,
    self_reported_outcome,
)
from omniagentos.scheduler.store import RoutineRunAlreadySettled, RoutinesStore
from omniagentos.steward.notify import send_piedpiper_email, send_slack

logger = logging.getLogger(__name__)

EXECUTED_GATE_TYPES = frozenset({"exit_code", "test_command", MERGE_GATE_TYPE})

Clock = Callable[[], datetime]


def utc_clock() -> datetime:
    """Read the wall clock, in UTC. The default judging clock (see module docs).

    Public because the tick that owns the batch instant (``routines_tick.tick``)
    must hand settlement a LIVE clock explicitly: it always passes a concrete
    ``now``, so settlement could not otherwise tell "production" from "a caller
    that deliberately pinned time".
    """
    return datetime.now(UTC)


def _judging_clock(now: datetime | None, clock: Clock | None) -> Clock:
    """Resolve the physical clock used to judge evidence freshness.

    An explicit ``clock`` always wins. Failing that, a caller who pinned ``now``
    pinned time on purpose (tests, a backfill re-driving a missed tick) and gets
    a frozen clock. Only the unpinned default — which is what production takes —
    reads the wall clock, and it reads it every time it is called.
    """
    if clock is not None:
        return clock
    if now is not None:
        return lambda: now
    return utc_clock


def evaluate_gate(
    gate_type: str,
    gate_config: dict[str, Any],
    run: dict[str, Any],
    *,
    evidence: object = None,
    routine_id: str = "",
    iteration: int = 0,
    workspace_digest: str = "",
    now: datetime | None = None,
    evidence_store: GateEvidenceStore | None = None,
) -> bool:
    """Decide goal-met from trusted evidence, never from the run's own claims.

    Judges evidence it is HANDED; it never executes a gate, so no time passes
    inside it and its ``now`` is already the moment of judgement.
    """
    if run.get("state") != RunState.COMPLETED.value:
        return False

    if gate_type in EXECUTED_GATE_TYPES:
        if evidence_store is None:
            logger.info(
                "routine %s run %s gate rejected: no evidence store provided for verification",
                routine_id,
                run.get("id"),
            )
            return False

        rejections = evidence_rejections(
            evidence,
            routine_id=routine_id,
            run_id=str(run.get("id") or ""),
            iteration=int(iteration),
            gate_type=gate_type,
            gate_config=gate_config,
            workspace_digest=workspace_digest,
            now=now or utc_clock(),
            verifier=evidence_store.verify,
        )
        if rejections:
            logger.info(
                "routine %s run %s gate rejected: %s",
                routine_id,
                run.get("id"),
                "; ".join(rejections),
            )
            return False
        return True

    return False


def _self_reported_run(routine_run: dict[str, Any], *, now_iso: str) -> dict[str, Any]:
    """A terminal ``runs``-shaped view of a tick that has no ``runs`` row.

    A built-in/loop tick is executed by the scheduler process, so nothing ever
    writes it a `runs` row — yet everything downstream of here is written in
    terms of one. Rather than fork the settlement path (two verdict paths that
    can disagree is its own incident class), the claim is projected onto the
    same terminal vocabulary: a tick that returned is COMPLETED, i.e. "finished
    running", which is all a run state has ever meant. It is emphatically NOT
    "succeeded" — the composition in :func:`settle_routine_run` decides that,
    and a self-reported ADVERSE claim short-circuits the gate there exactly as
    ``RunState.FAILED`` does here.

    The state is deliberately COMPLETED even for an adverse claim so that the
    adverse branch keeps the executor's specific reason code instead of being
    flattened into ``run_failed``.
    """
    return {
        "id": str(routine_run.get("run_id") or ""),
        "state": RunState.COMPLETED.value,
        "finished_at": str(routine_run.get("started_at") or now_iso),
        # A built-in tick spends no harness budget; the firing tick recorded 0.0
        # and settlement must not invent a cost delta for it.
        "cost_usd": None,
    }


def _notify(routine: dict[str, Any], run: dict[str, Any], gate_passed: bool | None) -> None:
    """Notify of settlement outcome. None gate_passed means no gate evidence to report."""
    if gate_passed is None:
        # No gate evidence; no notification required
        return

    target = routine.get("notification_target") or {}
    channel = target.get("channel")
    routine_name = str(routine.get("name") or routine.get("id") or "routine")
    run_id = str(run.get("id") or "unknown")
    outcome = "passed" if gate_passed else "failed"
    text = f"Routine {routine_name}: gate_passed={gate_passed} (run_id={run_id})"

    try:
        if channel == "slack":
            send_slack(text)
        elif channel == "email":
            email_target = target.get("target")
            if not email_target:
                logger.warning("routine %s email notification has no target", routine.get("id"))
                return
            send_piedpiper_email(
                str(email_target),
                f"Routine {routine_name}: gate {outcome}",
                (
                    f"<p>Routine <strong>{html.escape(routine_name)}</strong> "
                    f"gate {outcome}.</p><p>Run: {html.escape(run_id)}</p>"
                ),
            )
        elif channel in {"webhook", "desktop"}:
            logger.info(
                "routine %s notification channel %s is not implemented; skipping",
                routine.get("id"),
                channel,
            )
        else:
            logger.warning(
                "routine %s has unknown notification channel %r; skipping",
                routine.get("id"),
                channel,
            )
    except Exception:  # noqa: BLE001 -- notifications are always best effort.
        logger.exception("routine %s %s notification failed", routine.get("id"), channel)


def settle_routine_run(
    store: SqliteStore,
    routine_run: dict[str, Any],
    routine: dict[str, Any],
    run: dict[str, Any],
    *,
    now_iso: str | None = None,
    evidence_store: GateEvidenceStore | None = None,
    gate_runner: TrustedGateRunner | None = None,
    workspace: Path | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Evaluate and settle one terminal run, then notify best-effort.

    gate_passed semantics:
    - False: run failed, OR gate evaluated and rejected, OR gate actively refused evidence
    - True: gate evaluated and passed
    - None: gate not evaluated (no workspace, no evidence path, evidence fetch failed)

    Only runs with gate_passed IS NOT NULL are counted in the acceptance-floor denominator.

    For a self-reported (built-in / loop) run, ``accepted`` is composed from the
    claim AND the gate rather than mirroring ``gate_passed`` — see the module
    docstring for the full table and why the composition only ever runs
    downhill.

    ``now_iso`` is a record stamp; ``clock`` is the physical clock evidence
    freshness is judged against, read after the gate has run. ``now`` is the
    deprecated way to pin that clock to a fixed instant — see the module
    docstring's two-clocks section, and do not pin it from a batch loop that
    executes gates.
    """
    terminal_values = {state.value for state in TERMINAL_RUN_STATES}
    run_state = str(run.get("state") or "")
    if run_state not in terminal_values:
        raise ValueError(f"run is not terminal: {run.get('id')}")

    gate_type = str(routine.get("gate_type") or "")
    gate_config = routine.get("gate_config") or {}
    # What KIND of work this routine does, read straight off the row and passed
    # to the executor: a loops-module gate may not be judged by the loop
    # machinery, and only the executor can check that against the workspace and
    # SHA the gate really runs at (gate_runner.refuse_loop_gate_on_the_machinery).
    task_template = routine.get("task_template")
    if isinstance(task_template, str):
        try:
            task_template = json.loads(task_template)
        except (TypeError, ValueError):
            task_template = {}
    task_input = task_template.get("input") if isinstance(task_template, dict) else None
    task_module = (
        str((task_input or {}).get("module") or "") if isinstance(task_input, dict) else ""
    )
    routine_id = str(routine.get("id") or "")
    run_id = str(run.get("id") or "")
    iteration = int(routine_run.get("iteration") or 0)

    # The tick's own account of itself, if it made one. Empty for a dispatched
    # run: there the `runs` row IS the account, and the state machine that wrote
    # it is not the code being graded.
    self_report = str(routine_run.get(SELF_REPORT_COLUMN) or "")
    claim = self_reported_outcome(routine_run.get("stop_reason")) if self_report else ""
    claimed_reason = str(routine_run.get("stop_reason") or "")
    claimed_notes = str(routine_run.get("notes") or "")

    gate_passed: bool | None = False
    stop_reason = "gate_failed"
    notes = ""
    judging_clock = _judging_clock(now, clock)

    if run_state == RunState.FAILED.value:
        stop_reason = "run_failed"
        notes = f"settled terminal run state={run_state}; run failed"
    elif run_state == RunState.CANCELLED.value:
        stop_reason = "run_cancelled"
        notes = f"settled terminal run state={run_state}; run cancelled"
    elif claim == OUTCOME_ADVERSE:
        # The tick reported its OWN failure. Identical in kind to the FAILED
        # branch above — a run that did not produce a result has nothing for a
        # gate to verify — except that the machine-readable cause the executor
        # named (loop_blocked vs loop_failed vs builtin_failed) is worth more to
        # an operator than a generic "run_failed", so it is preserved. This is
        # the one direction in which believing the worker is safe: a self-report
        # may lower a verdict, never raise one.
        stop_reason = claimed_reason or "builtin_failed"
        notes = f"self-reported {self_report}; gate not executed (the tick failed)"
    elif gate_type in EXECUTED_GATE_TYPES:
        gate_workspace = workspace if workspace is not None else default_gate_workspace()
        if gate_workspace is None:
            # No workspace configured: gate cannot execute → absence of evidence
            stop_reason = "gate_evidence_unavailable"
            notes = f"settled terminal run state={run_state}; no configured gate workspace — excluded from acceptance floor"
            gate_passed = None
        else:
            workspace_digest = workspace_digest_for(gate_workspace)
            ev_store = evidence_store if evidence_store is not None else GateEvidenceStore()
            # Merge-candidate receipts are the wire product merge-gate.sh consumes:
            # routine_id is fixed, run_id is the candidate tip SHA, and both SHAs
            # must be on the GateRunRequest so the runner can bind them.
            candidate_sha = str(gate_config.get("candidate_sha") or "")
            merge_base_sha = str(gate_config.get("merge_base_sha") or "")
            if gate_type == MERGE_GATE_TYPE:
                evidence_routine_id = MERGE_GATE_ROUTINE_ID
                evidence_run_id = candidate_sha or run_id
            else:
                evidence_routine_id = routine_id
                evidence_run_id = run_id
            outcome = produce_gate_evidence(
                gate_runner,
                ev_store,
                GateRunRequest(
                    routine_id=evidence_routine_id,
                    run_id=evidence_run_id,
                    iteration=iteration,
                    gate_type=gate_type,
                    gate_config=gate_config,
                    workspace=gate_workspace,
                    candidate_sha=candidate_sha,
                    merge_base_sha=merge_base_sha,
                    # Which POLICY the executor applies to this gate's targets:
                    # a loops-module row may not be judged by the loop machinery
                    # (gate_runner.refuse_loop_gate_on_the_machinery). The write
                    # side checks the same rule against whatever checkout it ran
                    # in; only here is it checked against the workspace and SHA
                    # the gate actually executes at.
                    task_module=task_module,
                ),
            )
            if outcome.status == "in_progress":
                return {}
            elif outcome.status == "refused":
                # Gate actively refused evidence → actual rejection. Workspace
                # refusals do NOT arrive here: GateWorkspaceUnusable is mapped to
                # "unavailable" by produce_gate_evidence, so a dirty or missing
                # workspace settles NULL and stays out of the acceptance floor.
                stop_reason = "gate_refused"
                notes = f"settled terminal run state={run_state}; gate refused: {outcome.detail}"
                gate_passed = False
            elif outcome.status == "unavailable":
                # Evidence fetch failed → absence of evidence (not rejection)
                stop_reason = "gate_evidence_unavailable"
                notes = f"settled terminal run state={run_state}; gate evidence unavailable: {outcome.detail} — excluded from acceptance floor"
                gate_passed = None
            elif outcome.status == "evidence":
                evidence = outcome.evidence
                # READ THE CLOCK HERE, not at the top of this function and not
                # at the top of the batch: `produce_gate_evidence` above may
                # have just spent minutes EXECUTING the gate (up to the runner's
                # 900s timeout), and the receipt it returned is stamped with
                # wall-clock at completion. Freshness and the anti-forgery
                # future-check are questions about real elapsed time, so they
                # get the real elapsed time. Judging a 3-minute gate against the
                # instant the batch started is what scored passing gates
                # `adverse` with "gate evidence is dated in the future".
                judged_at = judging_clock()
                rejections = evidence_rejections(
                    evidence,
                    routine_id=evidence_routine_id,
                    run_id=evidence_run_id,
                    iteration=iteration,
                    gate_type=gate_type,
                    gate_config=gate_config,
                    workspace_digest=workspace_digest,
                    now=judged_at,
                    verifier=ev_store.verify,
                )
                if rejections:
                    stop_reason = "gate_failed"
                    notes = f"settled terminal run state={run_state}; gate failed: {'; '.join(rejections)}"
                    gate_passed = False
                else:
                    gate_passed = True
                    stop_reason = "gate_passed"
                    notes = f"settled terminal run state={run_state}; objective gate passed"
            else:
                # Unknown outcome status → absence of clear verdict (not rejection)
                stop_reason = "gate_evidence_unavailable"
                notes = f"settled terminal run state={run_state}; gate evidence unavailable: {outcome.detail} — excluded from acceptance floor"
                gate_passed = None
    else:
        # Gate type has no trusted evidence path → absence of evidence
        gate_passed = None
        stop_reason = "gate_unconfigured"
        notes = f"settled terminal run state={run_state}; no configured gate — excluded from acceptance floor"

    # `accepted` is the acceptance floor's numerator; `gate_passed` is what the
    # gate found. For a dispatched run they are the same fact. For a
    # self-reported run they are two, and the composition only runs downhill.
    accepted: bool | None = gate_passed
    if claim == OUTCOME_NEUTRAL and gate_passed is not False:
        # The tick BEHAVED but produced no judgeable result (parked on a human,
        # nothing to do). Keep whatever the gate actually found — deleting real
        # evidence would blind the sentinel's evidence-free streak alarm — but
        # take the run out of the denominator, and restore the executor's own
        # reason code, which is what the floor's backstop and any operator query
        # read. A FAILED gate is deliberately exempt: that is evidence of
        # failure, not absence of it, and it stays adverse.
        accepted = None
        stop_reason = claimed_reason or stop_reason
    if claim:
        # The claim itself is durable in `self_reported_status`; the executor's
        # prose (which node, which detail) is preserved here because settlement
        # is otherwise the point where it would be overwritten.
        notes = f"{claimed_notes} — {notes}" if claimed_notes else notes

    # ISSUE-8: `run["cost_usd"]` is NULL for two different reasons, and only
    # one of them is "unknown". A self-reported/built-in run's `cost_usd` is
    # None because `_self_reported_run` says so explicitly — it spent no
    # harness budget, a genuine known zero already recorded by the firing
    # tick, and settlement must not invent a delta for it. A DISPATCHED run's
    # `cost_usd` (`runs.cost_usd`, nullable since migration 001) is None when
    # every provider it called failed to report a cost — that is genuinely
    # unknown, not zero, and must poison the routine's rollup rather than
    # silently add nothing on top of it (see RoutinesStore.settle_run).
    run_cost_usd = run.get("cost_usd")
    cost_is_unknown = not self_report and run_cost_usd is None

    try:
        settled = RoutinesStore(store).settle_run(
            int(routine_run["id"]),
            gate_passed=gate_passed,
            accepted=accepted,
            finished_at=str(run.get("finished_at") or now_iso or utc_now_iso()),
            notes=notes,
            cost_usd=(float(run_cost_usd) if run_cost_usd is not None else None),
            cost_unknown=cost_is_unknown,
            stop_reason=stop_reason,
        )
    except RoutineRunAlreadySettled:
        return {}

    # Notify on the COMPOSED verdict, not the raw gate result: identical for a
    # dispatched run, and silent for a non-result that happens to carry gate
    # evidence (paging a human about "gate_passed=True" for a tick that parked
    # waiting on that same human is noise).
    _notify(routine, run, accepted)
    return settled


def settle_pending(
    store: SqliteStore,
    *,
    now: datetime | None = None,
    limit: int = 100,
    evidence_store: GateEvidenceStore | None = None,
    gate_runner: TrustedGateRunner | None = None,
    workspace: Path | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Settle up to ``limit`` terminal runs that still lack a finish stamp.

    ``now`` is the batch's record stamp and stays fixed for the whole batch.
    ``clock`` is the physical clock each candidate's evidence is judged against,
    read once per candidate AFTER that candidate's gate has finished executing —
    this loop runs the gates, so batch-start time is minutes stale by then. See
    the module docstring's two-clocks section.

    Two kinds of run are candidates, and they are found by the same query on
    purpose:

    * a DISPATCHED run, whose ``runs`` row has reached a terminal state; and
    * a SELF-REPORTED run — a built-in or loop tick this process executed
      itself, which has no ``runs`` row at all and is terminal by construction
      (it returned). The inner JOIN used to be what silently excluded every one
      of them, because ``settle_pending`` could only see a run that had a JOIN
      partner. That is why ``gate_config.command`` had never executed for a
      loop.
    """
    terminal_values = tuple(sorted(state.value for state in TERMINAL_RUN_STATES))
    placeholders = ",".join("?" for _ in terminal_values)
    # Probed, never assumed: a database that predates migration 104 has no
    # self-report column, and on it the firing tick settles built-ins inline
    # (see routines_tick._fire), so this query must stay exactly what it was.
    self_report_supported = RoutinesStore(store).supports_self_report()
    if self_report_supported:
        join_sql = (
            f"LEFT JOIN runs r ON r.id = rr.run_id "
            f"WHERE rr.run_id IS NOT NULL "
            f"AND rr.finished_at IS NULL "
            f"AND (rr.{SELF_REPORT_COLUMN} IS NOT NULL OR r.state IN ({placeholders})) "
        )
    else:
        join_sql = (
            f"JOIN runs r ON r.id = rr.run_id "
            f"WHERE rr.run_id IS NOT NULL "
            f"AND rr.finished_at IS NULL "
            f"AND r.state IN ({placeholders}) "
        )
    candidates = store._connection.execute(
        f"SELECT rr.* FROM routine_runs rr {join_sql}ORDER BY rr.created_at ASC LIMIT ?",
        (*terminal_values, max(0, int(limit))),
    ).fetchall()

    # The batch's LOGICAL instant: a record stamp only (the fallback finish time
    # for a row that has none of its own), deliberately identical for every row
    # this pass settles. It is NOT the clock any freshness rule may use.
    moment = now or utc_clock()
    now_iso = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    judging_clock = _judging_clock(now, clock)
    routines = RoutinesStore(store)
    settled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    gate_workspace = workspace if workspace is not None else default_gate_workspace()
    needs_gate_chain = gate_workspace is not None and any(
        (routines.get_routine(str(dict(row)["routine_id"])) or {}).get("gate_type")
        in EXECUTED_GATE_TYPES
        for row in candidates
    )
    if needs_gate_chain and evidence_store is None:
        evidence_store = GateEvidenceStore()
    if needs_gate_chain and gate_runner is None and evidence_store is not None:
        gate_runner = default_gate_runner(evidence_store)

    for row in candidates:
        routine_run = dict(row)
        routine = routines.get_routine(str(routine_run["routine_id"]))
        run: dict[str, Any] | None
        if routine_run.get(SELF_REPORT_COLUMN):
            run = _self_reported_run(routine_run, now_iso=now_iso)
        else:
            run = store.get_run(str(routine_run["run_id"]))
        if routine is None or run is None:
            errors.append(
                {
                    "routine_run_id": routine_run["id"],
                    "error": "routine or run not found",
                }
            )
            continue
        try:
            res = settle_routine_run(
                store,
                routine_run,
                routine,
                run,
                now_iso=now_iso,
                evidence_store=evidence_store,
                gate_runner=gate_runner,
                workspace=gate_workspace,
                # Hand down the CLOCK, never this batch's instant: settlement
                # executes the gate, so the moment of judgement is later than
                # `moment` by however long the gate took to run.
                clock=judging_clock,
            )
            if res:
                settled.append(res)
        except RoutineRunAlreadySettled:
            logger.info("routine_run %s already settled by concurrent task", routine_run["id"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to settle routine run %s", routine_run["id"])
            errors.append(
                {
                    "routine_run_id": routine_run["id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "checked": len(candidates),
        "settled": settled,
        "errors": errors,
    }


__all__ = [
    "EXECUTED_GATE_TYPES",
    "Clock",
    "evaluate_gate",
    "settle_pending",
    "settle_routine_run",
    "utc_clock",
]
