"""``run_once`` — one durable tick of one loop instance, in one short-lived process.

This is the whole driver. Everything below it — the executor, the seam, the
receipts, the approvals, the learning pass — is machinery this function arranges
in one fixed order, and the order is the contract:

1. **Take the instance's lease.** Losing it is a *non-result*, not a failure. The
   tick returns ``IDLE`` and writes no report card at all, because a worker that
   correctly stepped aside for a peer must not appear in the acceptance floor's
   denominator. Counting a stand-aside against the floor is how a fleet
   auto-pauses itself on a busy morning.
2. **Refuse to fire when the instance contract is not met.** A template whose
   required tool is not granted returns ``BLOCKED``, which is *adverse* — never
   ``IDLE`` and never ``COMPLETED``. This is the single loudest guard in the file
   and it exists because the alternative has happened: a fleet reported 68%
   acceptance while its executors were missing, because a loop with nothing to
   run rendered as a well-behaved loop with nothing to do.
3. **Invoke the compiled graph**, entering fresh, mid-run or resumed-from-a-park
   exactly as the durable checkpoint dictates. The runtime resolves the park's
   approval before handing a resume to the executor; it never decides one.
4. **Settle.** Run the declared gate, compose the claim against the verdict under
   may-lower-never-raise, and write the :class:`~selfloop.ledger.OutcomeRecord`
   with ``put_once`` — a run must not be able to overwrite its own report card.
   The returned report is lowered to match when a gate ruled AGAINST the tick,
   because ``RunReport.as_dict()`` derives ``accepted`` from the status and a
   contradicted tick must not print ``accepted: true`` to a scheduler.
5. **Run the learning pass, always** (:func:`selfloop.learn.learning_pass`).
   ``run_once`` is its ONLY caller. An earlier design also mounted a learning
   node in the graph, which gave the extract/stage/promote cycle two owners:
   double mining, a racing cursor, and a promotion parking outside the executor's
   park/resume protocol.
6. **Report.** ``LeaseHeld`` becomes ``IDLE``; ``BlockedLoopError`` becomes
   ``BLOCKED``; ``ParkRequested`` becomes ``PARKED``; ``RecursionExceeded``
   becomes ``ABORTED``; anything else becomes ``FAILED`` with a legible detail.
   **The tick reports; it does not crash.** A scheduler that receives a traceback
   learns nothing it can act on, and a process that dies before step 4 leaves the
   ledger with no account of the tick at all.

What a run id names
-------------------

``run_id`` names the WORK, not the process. A tick that parks for a human and is
resumed two hours later by a different process is one run with two report cards:
the checkpoint carries the run id forward, both invocations write an
:class:`~selfloop.ledger.OutcomeRecord` under it, and each record gets its own
per-invocation ``id``. That is why :func:`selfloop.outcome.compose` takes a
``record_id`` distinct from ``run_id``, and why
``selfloop.learn._outcome_for_run`` falls back to the newest row for a run.

The alternative — a fresh run id per invocation — was tried and has two holes.
The lesson-use rows written before the parked tick would stay pending forever,
so a lesson that demonstrably helped would never be credited; and the effect
events of a resumed tick would name a run whose only report card is the neutral
``PARKED`` one, so a failure on the resume path could never count toward any
lesson's support. Both are silent, and both are the shape of starvation this
package exists to refuse.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from selfloop.approvals import deep_link, resolve_for_resume
from selfloop.context import LoopContext
from selfloop.contracts import (
    BlockedLoopError,
    EvidenceGrade,
    GateReceipt,
    GateSpec,
    GateUnavailable,
    LeaseHeld,
    LoopState,
    LoopStatus,
    RecordKind,
    RecursionExceeded,
    RunReport,
    digest_key,
    initial_state,
    outcome_class,
)
from selfloop.engine import CompiledGraph, ParkRequested, Snapshot
from selfloop.kit import RUN_ID, failure_tag_of, run_id_of, scope_of
from selfloop.learn import learning_pass
from selfloop.ledger import DecisionRecord, EvidenceRecord, emit, write_history
from selfloop.outcome import compose
from selfloop.templates import get_template

#: Event channel for the runtime's own events, kept off the node channel the
#: executor writes so that "what did this tick decide about itself" and "which
#: nodes ran" are separately greppable.
RUNTIME_EVENT_KIND = "runtime"

#: ``state["data"]`` / ``params`` key naming the gate to execute for this tick.
#:
#: A tick whose artifact name depends on the day writes the spec into ``data``
#: from a node; an instance whose gate is the same every tick declares it once in
#: ``params``; a loop whose gate needs no argument (an
#: :class:`~selfloop.gates.ArtifactGate` constructed with its own artifact list)
#: declares nothing and gets :data:`DEFAULT_GATE_SPEC`.
#:
#: The value may be a :class:`~selfloop.contracts.GateSpec`, a mapping of its
#: fields, a sequence of command parts, or a single string.
GATE_SPEC = "gate_spec"

#: Head of the spec handed to a gate that was given no spec of its own. It is
#: deliberately not a real executable: a :class:`~selfloop.gates.CommandGate`
#: handed this reports ``command_not_found``, which raises
#: :class:`~selfloop.contracts.GateUnavailable` and settles the tick
#: neutral/uncorroborated. Visibly unverified is the correct outcome for a loop
#: whose gate was never told what to run — a fabricated pass is not.
DEFAULT_GATE_COMMAND = "selfloop:gate"

#: The spec used when nothing declared one. See :data:`DEFAULT_GATE_COMMAND`.
DEFAULT_GATE_SPEC = GateSpec(
    command=(DEFAULT_GATE_COMMAND,),
    label="the gate configured on this loop's context",
)

# ---------------------------------------------------------------------------
# Failure tags this module stamps
#
# Clustering partitions by ``(scope, failure_tag)`` before it compares a single
# token, so an adverse tick with no tag yields no signal at all — the loop
# notices its own failure, records it honestly, and learns nothing from it.
# Every adverse path below therefore carries one.
# ---------------------------------------------------------------------------

#: ``run_once`` was asked for a template this context is not for. See
#: :attr:`~selfloop.context.LoopContext.thread_id`.
TAG_TEMPLATE_MISMATCH = "template_mismatch"

#: The named template is not in the catalogue of this process.
TAG_TEMPLATE_UNKNOWN = "template_unknown"

#: A tool the template declares it requires is not granted to this instance.
TAG_TOOL_NOT_GRANTED = "tool_not_granted"

#: A required tool is granted but explicitly denied for this instance.
TAG_TOOL_DENIED = "tool_denied"

#: The tick executed more nodes than its budget allowed.
TAG_BUDGET_EXHAUSTED = "budget_exhausted"

#: The lease backend could not be used at all — not "held by a peer", which is
#: :class:`~selfloop.contracts.LeaseHeld` and is neutral.
TAG_LEASE_UNUSABLE = "lease_unusable"

#: The tick claimed success and the gate ruled against it. The disagreement is
#: the most valuable evidence this loop produces, so it gets a tag of its own
#: rather than inheriting whatever the tick happened to leave in ``data``.
TAG_GATE_CONTRADICTED = "gate_contradicted"

#: A fault nobody declared. The exception type and message go in the detail, and
#: token similarity inside this partition is what separates one crash from
#: another.
TAG_UNEXPECTED_ERROR = "unexpected_error"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _mint_run_id() -> str:
    """A fresh identity for a piece of work. Random, because it must be unique."""
    return f"run_{uuid.uuid4().hex[:20]}"


def _stamp(ctx: LoopContext) -> str:
    """``Clock.now_iso`` for a record stamp, or ``""`` when the clock is unusable.

    A record stamp is documentation and is never read by a freshness check —
    those read the monotonic clock — so a clock that cannot answer degrades a
    row's legibility and must not be able to fail a tick.
    """
    try:
        return ctx.clock.now_iso()
    except Exception:  # noqa: BLE001 - see the docstring: legibility, not a verdict
        return ""


def _fault(exc: BaseException) -> str:
    """``TypeName: message`` — what an operator reads in a ledger."""
    return f"{type(exc).__name__}: {exc}"


def _status_of(state: Mapping[str, Any]) -> LoopStatus:
    """The tick's terminal status, failing CLOSED on anything unrecognised.

    The executor's own finaliser already refuses to end a tick on an unknown or
    still-``running`` status, so reaching the fallback here means the state was
    written by something other than this package's executor. A runtime that
    cannot classify a tick cannot vouch for it, and the most favourable available
    outcome is exactly the wrong guess at exactly the point where the loop grades
    itself.
    """
    try:
        return LoopStatus(str(state.get("status") or ""))
    except ValueError:
        return LoopStatus.FAILED


def _gate_spec_for(state: LoopState) -> GateSpec:
    """The spec to execute for this tick. See :data:`GATE_SPEC`.

    A tick's own ``data`` wins over the instance's ``params``, because the node
    that named an artifact knows the tick and the params only know the instance.
    """
    raw = (state.get("data") or {}).get(GATE_SPEC)
    if raw is None:
        raw = (state.get("params") or {}).get(GATE_SPEC)
    if raw is None:
        return DEFAULT_GATE_SPEC
    if isinstance(raw, GateSpec):
        return raw
    if isinstance(raw, str):
        return GateSpec(command=(raw,))
    if isinstance(raw, Mapping):
        return GateSpec(
            command=tuple(str(part) for part in raw.get("command") or ()),
            cwd=str(raw.get("cwd", "")),
            timeout_s=float(raw.get("timeout_s", DEFAULT_GATE_SPEC.timeout_s)),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            label=str(raw.get("label", "")),
        )
    return GateSpec(command=tuple(str(part) for part in raw))


def _run_gate(
    ctx: LoopContext, state: LoopState, run_id: str
) -> tuple[GateReceipt | None, str, GateSpec | None]:
    """Execute the declared gate. Returns ``(receipt, unavailable_reason, spec)``.

    Every failure path here yields ``receipt=None`` and a reason, which composes
    to ``gate_passed=None`` — *the gate did not run*. It never yields a failing
    receipt, and the distinction is the whole reason this helper exists: a
    refusal recorded as a failure feeds the auto-pause floor, and a loop that
    paused itself because its verifier was uninstalled has been disabled by its
    own monitoring.

    A runner that raises something other than
    :class:`~selfloop.contracts.GateUnavailable`, or hands back an object that is
    not a :class:`~selfloop.contracts.GateReceipt`, is treated as unavailable for
    the same reason and never as a pass. Absence of a verdict is never the most
    favourable outcome.
    """
    if ctx.gate is None:
        return None, "no_gate", None
    try:
        spec = _gate_spec_for(state)
    except (TypeError, ValueError) as exc:
        emit(
            ctx,
            RUNTIME_EVENT_KIND,
            "gate_spec_invalid",
            {"error": _fault(exc)},
            run_id=run_id,
        )
        return None, "gate_spec_invalid", None

    try:
        receipt = ctx.gate.run(spec)
    except GateUnavailable as exc:
        emit(
            ctx,
            RUNTIME_EVENT_KIND,
            "gate_unavailable",
            {"reason": exc.reason, "detail": exc.detail, "spec": spec.as_dict()},
            run_id=run_id,
        )
        return None, exc.reason or "gate_unavailable", spec
    except Exception as exc:  # noqa: BLE001 - a broken runner rules nothing; see the docstring
        emit(
            ctx,
            RUNTIME_EVENT_KIND,
            "gate_runner_raised",
            {"error": _fault(exc), "spec": spec.as_dict()},
            run_id=run_id,
        )
        return None, "gate_runner_raised", spec

    if not isinstance(receipt, GateReceipt):
        return None, "gate_returned_non_receipt", spec
    # A vacuous receipt is returned rather than dropped: composition reads it as
    # an absence anyway, and recording ``checks_collected == 0`` next to a NULL
    # verdict is what an operator greps for when they suspect a gate has quietly
    # stopped testing.
    return receipt, "", spec


def _record_evidence(
    ctx: LoopContext,
    *,
    run_id: str,
    record_id: str,
    scope: str,
    receipt: GateReceipt,
    spec: GateSpec | None,
    state: LoopState,
) -> None:
    """File the gate's ruling bound to the exact content it ruled on.

    A receipt on its own says "something passed". Binding it to a digest of the
    spec that ran and the effects the tick recorded is what lets a later reader
    detect that the content moved on since the verdict was minted — without the
    bind, a verdict attaches to an id and whatever later occupies that id
    inherits a pass it never earned.

    The evidence grade is read off the runner, which is where the honest claim
    lives: an :class:`~selfloop.gates.ArtifactGate` declares ``LOCAL_ARTIFACT``
    and a probe that asks somebody else's system of record declares
    ``SYSTEM_OF_RECORD``. A runner that declares nothing is recorded at the
    lowest useful rung rather than being flattered upward.
    """
    grade = getattr(ctx.gate, "evidence_grade", EvidenceGrade.LOCAL_ARTIFACT)
    try:
        grade = EvidenceGrade(int(grade))
    except (TypeError, ValueError):
        grade = EvidenceGrade.LOCAL_ARTIFACT
    subject = spec.label or " ".join(spec.command) if spec is not None else scope
    record = EvidenceRecord(
        id=f"ev_{record_id}",
        at=_stamp(ctx),
        run_id=run_id,
        instance_id=ctx.instance_id,
        template=ctx.template,
        subject=subject or scope,
        subject_digest=digest_key(
            None if spec is None else spec.as_dict(), list(state.get("effects") or ())
        ),
        receipt=receipt,
        spec=spec,
        evidence_grade=grade,
    )
    write_history(ctx, RecordKind.EVIDENCE, record.id, record.as_dict())


#: How many report cards one run may file before this module stops numbering
#: them. A run reaches double figures only by resuming over and over, which is
#: an operator's problem rather than a numbering problem.
MAX_OUTCOME_RECORDS = 200


def _outcome_record_id(ctx: LoopContext, run_id: str) -> str:
    """A per-invocation id for this run's report card, **sortable by invocation**.

    Two properties, and the second one is the one that is easy to lose.

    It is not the run id, because a parked run that is later resumed files a
    second card and ``put_once`` would refuse it — leaving the neutral
    ``PARKED`` row as the only account of a run that went on to complete.

    And it sorts in the order the cards were filed, because
    ``selfloop.learn._outcome_for_run`` resolves a run with more than one card
    by taking the newest, ordered by ``(at, id)``. Record stamps come from a
    clock a caller may legitimately pin, and two invocations minutes apart can
    carry the same stamp, so a random id would make "newest" arbitrary — and an
    arbitrary choice there means a resumed run's evidence resolves to whichever
    card won a coin toss, which decides whether that evidence counts toward a
    lesson at all.

    The uuid fallback past :data:`MAX_OUTCOME_RECORDS` keeps the row rather than
    dropping it, and sorts after every numbered one.
    """
    for sequence in range(1, MAX_OUTCOME_RECORDS + 1):
        candidate = f"out_{run_id}#{sequence:03d}"
        if ctx.records.get(RecordKind.OUTCOME.value, candidate) is None:
            return candidate
    return f"out_{run_id}#z{uuid.uuid4().hex[:12]}"


def _stage_after(ctx: LoopContext, graph: CompiledGraph | None) -> tuple[str, str]:
    """``(stage, approval_id)`` read from the durable checkpoint after the tick.

    The stage carries no meaning for the acceptance floor, and it is the whole
    difference between an operator reading "parked" and reading "parked at
    draft_approve_send/send". It is read from the checkpoint rather than tracked
    in memory so that it describes what actually survived, including after a
    node raised.
    """
    if graph is None:
        return "", ""
    try:
        snap = graph.snapshot(ctx, ctx.thread_id)
    except Exception:  # noqa: BLE001 - a diagnostic must not fail the report
        return "", ""
    return snap.parked_at or snap.next_node, snap.approval_id


# ---------------------------------------------------------------------------
# Settlement — the one tail every path under the lease goes through
# ---------------------------------------------------------------------------


def _settle(
    ctx: LoopContext,
    *,
    state: LoopState,
    run_id: str,
    status: LoopStatus,
    detail: str,
    failure_tag: str = "",
    stage: str = "",
    approval_id: str | None = None,
    resumed: bool = False,
    graph: CompiledGraph | None = None,
    learn: bool = True,
) -> RunReport:
    """Grade the tick, record it, then run the learning pass. Returns the report.

    The gate is executed **only when the tick's own claim is favourable**, and
    that is the truth table in :func:`selfloop.outcome.compose` made operational
    rather than an economy. A gate may lower a claim and may never raise one, so
    a neutral tick with a green gate is still a neutral tick and an adverse tick
    is adverse whatever any gate says. Running a subprocess to learn something
    that cannot change the answer costs an idle loop a gate execution per tick
    forever; the reason the gate did not run is recorded in the row either way.

    The report card is written with ``put_once`` under a per-invocation id (see
    :func:`_outcome_record_id`), so a run cannot overwrite its own account of
    itself and a resumed run does not have to. A refusal at that point is
    therefore not the ordinary resume path — it means two workers are settling
    one run — so it is recorded rather than shrugged at.

    **The returned status is lowered when a gate ruled AGAINST the tick, and not
    when a gate merely failed to run.** That asymmetry is the whole taxonomy in
    one line, and it matters because
    :meth:`~selfloop.contracts.RunReport.as_dict` derives ``accepted`` from the
    status: a tick that claimed success while an independent verifier said
    otherwise must not print ``accepted: true`` to a scheduler, which is the
    "68% acceptance while nothing works" number this package exists to refuse.
    A tick that nobody checked is a different thing — absence of evidence is not
    evidence of failure — so it keeps its ``COMPLETED`` claim, and the row it
    just wrote (``gate_passed = NULL``, ``outcome_class = neutral``) is what
    stops that claim from counting as an acceptance anywhere it matters. Both
    disagreements between the claim and the settlement are named in the detail,
    because the moment the report and the record differ is exactly the moment a
    reader needs to be told.
    """
    claim = outcome_class(status)
    scope = scope_of(state)
    tag = failure_tag or failure_tag_of(state)

    receipt: GateReceipt | None = None
    spec: GateSpec | None = None
    reason = f"claim_{claim}_gate_not_run"
    if claim == "favourable":
        receipt, reason, spec = _run_gate(ctx, state, run_id)

    ruled = None if receipt is None or receipt.is_vacuous else bool(receipt.passed)
    if claim == "favourable" and ruled is False:
        # The tick said it succeeded and an independent verifier said otherwise.
        # Without a tag here the composition is adverse and yields no signal at
        # all, so the single most informative failure this loop can produce would
        # be the one it never learns from. The gate's own complaint becomes the
        # row's detail when the tick offered none, because that sentence is what
        # the mined signal's text is built from — and "release-notes.md (34
        # bytes)" is what lets clustering tell one contradiction from another
        # inside the same partition.
        tag = tag or TAG_GATE_CONTRADICTED
        detail = detail or (receipt.detail if receipt is not None else "")

    record_id = _outcome_record_id(ctx, run_id)
    record = compose(
        status,
        receipt,
        run_id=run_id,
        instance_id=ctx.instance_id,
        template=ctx.template,
        at=_stamp(ctx),
        scope=scope,
        failure_tag=tag,
        detail=detail,
        gate_unavailable_reason=reason,
        record_id=record_id,
    )
    reported = status
    if claim == "favourable" and ruled is False:
        reported = LoopStatus.FAILED
        detail = (
            f"the tick reported {status.value} and the gate ruled against it — "
            f"{record.gate_detail or detail}"
        )
    elif claim == "favourable" and ruled is None:
        detail = (
            f"{detail} | uncorroborated ({record.gate_unavailable_reason}): recorded "
            "neutral, which is not an acceptance — nothing verified this tick"
        ).strip(" |")

    if not write_history(ctx, RecordKind.OUTCOME, record.id, record.as_dict()):
        # Unreachable while the lease holds — the id was probed a moment ago and
        # only this process may be ticking this instance. Recorded rather than
        # ignored, because if it ever fires it means two workers are settling one
        # run and the acceptance floor is being written by a race.
        emit(
            ctx,
            RUNTIME_EVENT_KIND,
            "outcome_record_refused",
            {"record": record.id, "outcome_class": record.outcome_class},
            run_id=run_id,
        )
    if receipt is not None:
        _record_evidence(
            ctx,
            run_id=run_id,
            record_id=record_id,
            scope=scope,
            receipt=receipt,
            spec=spec,
            state=state,
        )

    emit(
        ctx,
        RUNTIME_EVENT_KIND,
        "tick_settled",
        {
            "self_reported": record.self_reported_status,
            "gate_passed": record.gate_passed,
            "outcome_class": record.outcome_class,
            "scope": scope,
            "failure_tag": tag,
            "outcome_record": record.id,
        },
        run_id=run_id,
    )

    report = RunReport(
        instance_id=ctx.instance_id,
        template=ctx.template,
        status=reported,
        detail=detail,
        effects=list(state.get("effects") or []),
        approval_id=approval_id,
        resumed=resumed,
        stage=stage,
        run_id=run_id,
        tick=int(state.get("tick") or 0),
    )
    if graph is not None and not stage:
        found_stage, found_approval = _stage_after(ctx, graph)
        report = replace(
            report,
            stage=found_stage,
            approval_id=report.approval_id or (found_approval or None),
        )
    return _learning_tail(ctx, report) if learn else report


def _learning_tail(ctx: LoopContext, report: RunReport) -> RunReport:
    """Run the one learning pass, and surface a parked promotion on the report.

    Two rules, both load-bearing.

    **A broken learner must not fail a working tick.** The report card is already
    written and immutable by the time this runs, so an exception here cannot
    change what the acceptance floor sees — but if it escaped, the tick would be
    reported ``FAILED``, and a loop that auto-pauses because its *learning* broke
    has been disabled by the part of itself that was supposed to improve it. The
    failure is recorded on the event stream and appended to the report's detail,
    which is loud without being fatal.

    **Surfacing a park may lower the report and may never raise it.** A parked
    lesson promotion renders the tick ``PARKED``, which is neutral — so it is
    surfaced only when the tick was not adverse. An adverse tick whose learning
    pass happens to be waiting on a human keeps its adverse status and carries
    the approval id anyway, because laundering a failure into "waiting for a
    human" is exactly the absence-as-favourable move this package refuses.
    """
    try:
        passed = learning_pass(ctx, report.run_id)
    except Exception as exc:  # noqa: BLE001 - see the docstring: loud, not fatal
        emit(
            ctx,
            RUNTIME_EVENT_KIND,
            "learning_pass_failed",
            {"error": _fault(exc)},
            run_id=report.run_id,
        )
        return replace(
            report,
            detail=(
                f"{report.detail} | the learning pass raised {_fault(exc)}; the tick's own "
                "outcome stands and the cursor did not move, so the next tick re-mines "
                "this window"
            ).strip(" |"),
        )

    if not passed.parks:
        return report

    approval = passed.approval_ids[0] if passed.approval_ids else None
    link = deep_link(ctx, approval) if approval else ""
    note = (
        f"lesson promotion parked for a human on approval {approval or '(unrecorded)'}"
        f"{f' — {link}' if link else ''}; the next run_once retries it"
    )
    if report.outcome == "adverse":
        return replace(
            report,
            approval_id=report.approval_id or approval,
            detail=f"{report.detail} | {note}".strip(" |"),
        )
    return replace(
        report,
        status=LoopStatus.PARKED,
        approval_id=report.approval_id or approval,
        detail=(
            f"{report.detail} | tick settled {report.status.value}; {note}"
        ).strip(" |"),
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def run_once(
    ctx: LoopContext, template_name: str, *, params: Mapping[str, Any] | None = None
) -> RunReport:
    """Run ONE tick of *template_name* for ``ctx``'s instance. Never raises.

    *template_name* is not a way to choose a template. The checkpoint thread is
    ``f"{ctx.template}:{ctx.instance_id}"``, so a context whose ``template`` says
    one thing and a call that says another would resume a half-finished graph
    into a different template's node names — which is a silent no-op, not a
    crash. The argument is the assertion that this context is the one for that
    template, and a disagreement is refused rather than quietly resolved.

    *params* is overlaid on :attr:`~selfloop.context.LoopContext.params`: the
    context carries what is true of every tick of this instance, the call carries
    what is true of this one. The merged mapping becomes ``state["params"]`` and
    is stamped with the run id, which is how
    :func:`selfloop.kit.inject_lessons` can record "this lesson was in this run"
    *before* the run produces an outcome.
    """
    run_id = _mint_run_id()
    base_params = {**dict(ctx.params), **dict(params or {})}
    # Built before anything can fail, so that every refusal path below still has
    # a state to read the learning scope off — an unscoped refusal produces no
    # signal, and a loop that cannot learn from its own refusals is a loop that
    # keeps making them.
    seed = initial_state(ctx.instance_id, template_name, {**base_params, RUN_ID: run_id})

    try:
        lease = ctx.lease.hold(ctx.instance_id)
    except LeaseHeld as held:
        return _stood_aside(ctx, seed, run_id, held)
    except Exception as exc:  # noqa: BLE001 - an unusable lease is adverse, not neutral
        # No lease, so no learning pass: that pass reads and re-writes lesson
        # rows and the shared cursor while another process may be mid-tick. One
        # insert under this invocation's own id is safe; a multi-row
        # read-modify-write is not.
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=(
                f"the lease backend could not be used for instance {ctx.instance_id!r}: "
                f"{_fault(exc)}. This is not contention — a peer holding the lease is "
                "reported as IDLE — so it counts against the acceptance floor and needs "
                "an operator."
            ),
            failure_tag=TAG_LEASE_UNUSABLE,
            learn=False,
        )

    with lease:
        return _ticked(ctx, seed, template_name, run_id)


def _stood_aside(
    ctx: LoopContext, seed: LoopState, run_id: str, held: LeaseHeld
) -> RunReport:
    """A peer holds the lease. Report ``IDLE`` and write **no report card**.

    Stepping aside is a non-result, and the absence of an
    :class:`~selfloop.ledger.OutcomeRecord` is the point rather than an
    omission: a stand-aside must be out of the acceptance floor's numerator AND
    its denominator, because the tick it stood aside for is about to file the
    only honest account of that work. A row here would double-count one tick and,
    on a busy morning, would read as a run of failures.

    A :class:`~selfloop.ledger.DecisionRecord` is written instead — this is a
    point where the system chose between proceeding and stopping, which is
    exactly what that row is for — and its id carries this invocation's run id so
    that repeated contention is visible as repeated rows rather than collapsing
    into one.
    """
    holder = dict(held.holder)
    record = DecisionRecord(
        id=f"dec_{digest_key('lease_refused', ctx.instance_id, run_id)[:20]}",
        at=_stamp(ctx),
        instance_id=ctx.instance_id,
        template=ctx.template,
        node="",
        subject=f"lease:{ctx.instance_id}",
        decision="refused",
        by=ctx.actor,
        reason=held.detail,
        run_id=run_id,
        detail={"holder": holder},
    )
    write_history(ctx, RecordKind.DECISION, record.id, record.as_dict())
    emit(
        ctx,
        RUNTIME_EVENT_KIND,
        "lease_refused",
        {"holder": holder, "detail": held.detail},
        run_id=run_id,
    )
    return RunReport(
        instance_id=ctx.instance_id,
        template=ctx.template,
        status=LoopStatus.IDLE,
        detail=(
            f"another worker holds this instance's lease, so this process stood aside: "
            f"{held.detail}. Stepping aside is a non-result — it is neither accepted nor "
            "counted against the acceptance floor."
        ),
        run_id=run_id,
        tick=int(seed.get("tick") or 0),
    )


def _ticked(
    ctx: LoopContext,
    seed: LoopState,
    template_name: str,
    run_id: str,
) -> RunReport:
    """Everything that happens while the lease is held."""
    if template_name != ctx.template:
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=(
                f"this context is for template {ctx.template!r} and run_once was asked for "
                f"{template_name!r}. The checkpoint thread is derived from the context's "
                "template, so running the other one here would resume a half-finished graph "
                "into node names it does not have — a silent no-op. Build the context for "
                "the template you mean (dataclasses.replace(ctx, template=...))."
            ),
            failure_tag=TAG_TEMPLATE_MISMATCH,
        )

    try:
        template = get_template(template_name)
    except KeyError as exc:
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=(
                f"{exc}. Register it with selfloop.templates.register_template() before the "
                "first tick; a template that is not in this process's catalogue cannot be "
                "run, and reporting that as an idle tick would hide a broken deployment "
                "behind a well-behaved loop."
            ),
            failure_tag=TAG_TEMPLATE_UNKNOWN,
        )

    # THE loudest guard in this file. A loop whose executor is missing must be
    # adverse, not idle and not completed: "missing executor renders favourable"
    # is how a fleet reports 68% acceptance while nothing runs at all. Both
    # checks happen before anything is built or invoked, so nothing has been
    # done to the world by the time the refusal is reported.
    missing = template.missing_tools(ctx)
    if missing:
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=(
                f"template {template_name!r} requires tool(s) {list(missing)}, which this "
                f"instance was not granted (granted: {sorted(ctx.tools.names())}). Refusing "
                "to fire: a loop that cannot reach its executor has not had a quiet tick, "
                "it has had a broken one."
            ),
            failure_tag=TAG_TOOL_NOT_GRANTED,
        )
    denied = tuple(tool for tool in template.required_tools if tool in ctx.denied_tools)
    if denied:
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=(
                f"template {template_name!r} requires tool(s) {list(denied)}, which are "
                "granted but denied for this instance. The gate would refuse them halfway "
                "through the tick, after the earlier nodes had already touched the world, "
                "so the refusal is taken here instead."
            ),
            failure_tag=TAG_TOOL_DENIED,
        )

    try:
        graph = template.build(ctx)
        snap: Snapshot = graph.snapshot(ctx, ctx.thread_id)
    except Exception as exc:  # noqa: BLE001 - a build or checkpoint fault is still a report
        return _settle(
            ctx,
            state=seed,
            run_id=run_id,
            status=LoopStatus.FAILED,
            detail=f"could not prepare a tick of {template_name!r}: {_fault(exc)}",
            failure_tag=TAG_UNEXPECTED_ERROR,
        )

    # A resumed tick continues work that already has an id; a fresh tick starts
    # its own. See the module docstring for what a run id names and for the two
    # attribution holes that minting a new one on every invocation opens.
    carried = run_id_of(snap.state)
    if (snap.parked or snap.mid_run) and carried:
        run_id = carried
    resumed = snap.mid_run

    resume: Mapping[str, Any] | None = None
    if snap.parked:
        decided = resolve_for_resume(ctx, snap.approval_id)
        if not decided.terminal:
            # Nothing is invoked — not the parked node, not the entry node. The
            # alternative reading ("nothing is running, so start a fresh tick")
            # would re-run every node before the park on each scheduled
            # invocation, re-drafting and re-fetching the world once per tick for
            # as long as the approval sat undecided, while presenting as a
            # well-behaved parked loop.
            link = deep_link(ctx, snap.approval_id)
            return _settle(
                ctx,
                state=snap.state,
                run_id=run_id,
                status=LoopStatus.PARKED,
                detail=(
                    f"waiting for a human on approval {snap.approval_id} at node "
                    f"{snap.parked_at!r}{f' — {link}' if link else ''}"
                ),
                stage=snap.parked_at,
                approval_id=snap.approval_id,
                resumed=False,
                graph=graph,
            )
        resume = decided.as_dict()
        resumed = True

    try:
        final = graph.invoke(ctx, ctx.thread_id, state=seed, resume=resume)
    except ParkRequested as park:
        # The executor catches its own parks, so reaching this means a park was
        # raised outside a node. Rendered rather than re-raised, because a park
        # is the machinery working correctly and a caller must never see it as a
        # crash.
        return _settle(
            ctx,
            state=snap.state,
            run_id=run_id,
            status=LoopStatus.PARKED,
            detail=f"parked awaiting approval {park.approval_id}",
            approval_id=park.approval_id,
            resumed=resumed,
            graph=graph,
        )
    except RecursionExceeded as exc:
        # The executor already settled the durable state as ABORTED and reset the
        # thread before raising. Reporting FAILED here would put the report card
        # and the checkpoint in disagreement about the same tick.
        return _settle(
            ctx,
            state=snap.state,
            run_id=run_id,
            status=LoopStatus.ABORTED,
            detail=exc.detail,
            failure_tag=TAG_BUDGET_EXHAUSTED,
            resumed=resumed,
            graph=graph,
        )
    except BlockedLoopError as exc:
        # The system caused it and only the system can fix it: a revoked grant, a
        # dead refresh token, an exhausted quota. Adverse on purpose, so it trips
        # the floor and reaches an operator instead of idling green for a week.
        return _settle(
            ctx,
            state=snap.state,
            run_id=run_id,
            status=LoopStatus.BLOCKED,
            detail=exc.detail,
            failure_tag=exc.cause or TAG_UNEXPECTED_ERROR,
            resumed=resumed,
            graph=graph,
        )
    except Exception as exc:  # noqa: BLE001 - the tick reports; it does not crash
        return _settle(
            ctx,
            state=snap.state,
            run_id=run_id,
            status=LoopStatus.FAILED,
            detail=f"tick raised: {_fault(exc)}",
            failure_tag=TAG_UNEXPECTED_ERROR,
            resumed=resumed,
            graph=graph,
        )

    status = _status_of(final)
    return _settle(
        ctx,
        state=final,
        run_id=run_id,
        status=status,
        detail=str(final.get("error") or ""),
        resumed=resumed,
        graph=graph,
    )


__all__ = [
    "DEFAULT_GATE_COMMAND",
    "DEFAULT_GATE_SPEC",
    "GATE_SPEC",
    "RUNTIME_EVENT_KIND",
    "TAG_BUDGET_EXHAUSTED",
    "TAG_GATE_CONTRADICTED",
    "TAG_LEASE_UNUSABLE",
    "TAG_TEMPLATE_MISMATCH",
    "TAG_TEMPLATE_UNKNOWN",
    "TAG_TOOL_DENIED",
    "TAG_TOOL_NOT_GRANTED",
    "TAG_UNEXPECTED_ERROR",
    "run_once",
]
