"""The three-valued honesty layer: what the loop claimed, what a gate ruled.

This is the cheapest module in the package and the one it cannot do without. It
does no I/O, holds no state and imports no port. Everything here is a pure
function over a claim and a receipt, which means every rule below is
independently testable and independently replaceable — and, more importantly,
that a reader can check the entire grading policy of an unattended loop by
reading one file.

**The rule, in one sentence: a gate may LOWER the loop's claim and may never
RAISE it.** A loop that says it succeeded and is corroborated is accepted. A
loop that says it succeeded and is contradicted is adverse. A loop that says it
succeeded and was never checked is *uncorroborated* — which is neither, and is
excluded from grading entirely rather than counted as either.

**Why three values and not two.** Both ways of collapsing the middle value are
defects that have shipped:

* Counting a non-result as a REJECTION auto-paused four production routines in
  one night. Nothing was broken; they had nothing to do, and the acceptance
  floor read a run of idle ticks as a run of failures.
* Counting a non-result as an ACCEPTANCE was worse. A routine that parked every
  single tick scored an acceptance rate of 1.0 across ten self-graded runs whose
  declared gate had never executed once — and that number was then fed back in
  as the loop's own training signal.

:func:`acceptance_floor` therefore removes the middle value from BOTH the
numerator and the denominator, and reports ``meets=None`` when nothing was
gateable. "I cannot tell" is a first-class answer here. It is the only honest
one when there is no evidence, and a floor that returns ``False`` in that case
is indistinguishable from a floor that found real failures.

**Two three-valued vocabularies live here, and they are about different things.**
:class:`Settlement` grades an ARTIFACT (did the work leave something behind that
can be judged?). ``outcome_class`` grades a RUN (favourable / neutral /
adverse). :func:`settlement_of` is the one bridge between them. Keeping them
apart matters because a stage can produce a perfectly good artifact during a
tick that is, overall, adverse.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from selfloop.contracts import GateReceipt, LoopStatus, outcome_class
from selfloop.ledger import OutcomeRecord


class Settlement(StrEnum):
    """Explicit three-valued outcome of one stage or run, judged by its artifact.

    The design rule this enum enforces: **a recorded status is a function of the
    ARTIFACT a stage produced, never of the code path that ran.** Before that
    rule existed, a nightly loop recorded a row whose stage columns all read
    ``ok`` while its byte count was 0 and no output file existed at all — the
    status was a transcript of which ``try`` blocks did not raise, not a
    statement about what the run produced.

    ``OK``
        A real artifact exists and is non-empty, or explicit positive evidence
        was supplied. The only value that may ever be recorded as a success, and
        never reachable without proof.
    ``FAILED``
        The stage raised, or an artifact it OWED is missing or empty.
    ``UNGATEABLE``
        There is nothing to grade. An explicit third outcome, so a run nobody
        can grade is excluded from the acceptance floor by construction instead
        of being silently bucketed as unfavourable.
    """

    OK = "ok"
    FAILED = "failed"
    UNGATEABLE = "ungateable"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def artifact_bytes(artifact: str | os.PathLike[str] | None) -> int | None:
    """Size of *artifact* in bytes, or ``None`` when it is not a readable file.

    ``None`` means "there is no artifact to judge" and it is never confused with
    ``0``, which means "an artifact exists and is empty". Those are different
    facts and they settle differently: an empty file is a producer that ran and
    wrote nothing, which is a failure; an absent file may simply be a stage that
    had no output to give.

    A directory, a broken symlink, an unreadable path and a value that is not a
    path at all all return ``None``. This function never raises — a stat that
    fails is an absence of information, and turning it into an exception would
    push the decision to a caller that has strictly less context than this
    function does.
    """
    if artifact is None:
        return None
    try:
        path = Path(os.fspath(artifact))
    except TypeError:
        return None
    try:
        if not os.path.isfile(path):
            return None
        return int(os.path.getsize(path))
    except OSError:
        return None


def classify_settlement(
    artifact: str | os.PathLike[str] | None = None,
    *,
    error: BaseException | str | None = None,
    evidence: bool | None = None,
    required: bool = True,
    min_bytes: int = 1,
) -> Settlement:
    """Settle one stage or run from what it left behind.

    :param artifact: The file the stage was supposed to produce. ``OK`` requires
        at least *min_bytes* on disk; a zero-byte artifact is never a success,
        because a zero-byte file is evidence that a writer ran, not evidence
        that it produced anything.
    :param error: A raised exception or a message. Any truthy value settles
        ``FAILED`` regardless of the artifact — a stage that raised does not get
        to be judged on a file it may have written before it raised.
    :param evidence: Explicit positive or negative evidence, for stages that
        legitimately have no file artifact ("validation produced decisions").
        ``None`` means UNKNOWN, and unknown can never settle ``OK``. This is the
        fail-closed hinge of the whole function: absence of a verdict is never
        the most favourable outcome.
    :param required: What a missing or empty artifact means. ``True`` settles
        ``FAILED`` (the producer OWED us this file), ``False`` settles
        ``UNGATEABLE`` (there is simply nothing to grade).
    :param min_bytes: Minimum size that counts as non-empty.

    ``required`` defaults to ``True``, which is a deliberate change from the
    system this rule was extracted from — it defaulted to ``False``. Naming an
    artifact in this call is a claim that the stage owed you the file, so the
    default should be the strict reading. Under the old default a caller who
    forgot the keyword got their missing artifact quietly excluded from the
    acceptance floor rather than counted against it, which is the same shape of
    hole as a vacuous gate: invisible non-verification.
    """
    if error is not None and (not isinstance(error, str) or error.strip()):
        return Settlement.FAILED

    missing = Settlement.FAILED if required else Settlement.UNGATEABLE

    if artifact is not None:
        size = artifact_bytes(artifact)
        if size is None or size < min_bytes:
            return missing
        if evidence is False:
            # The file is there, and something that looked closer says it does
            # not count. A negative check outranks mere existence; it does not
            # get to be a failure either, because the caller only told us the
            # evidence was negative, not that the producer owed us more.
            return Settlement.UNGATEABLE
        return Settlement.OK

    if evidence is True:
        return Settlement.OK
    return missing


def counts_toward_acceptance_floor(settlement: Settlement | str | None) -> bool:
    """``UNGATEABLE`` — and an unrecorded settlement — never enter the floor.

    Stated as its own predicate because "excluded from both the numerator and
    the denominator" is the rule people re-derive incorrectly. It is not a
    weighting and it is not a zero; the observation leaves the sample entirely.
    """
    if settlement is None:
        return False
    value = settlement.value if isinstance(settlement, Settlement) else str(settlement)
    return value in (Settlement.OK.value, Settlement.FAILED.value)


def compose(
    self_reported: LoopStatus | str,
    gate: GateReceipt | None,
    *,
    run_id: str = "",
    instance_id: str = "",
    template: str = "",
    at: str = "",
    scope: str = "",
    failure_tag: str = "",
    detail: str = "",
    gate_unavailable_reason: str = "",
    record_id: str = "",
) -> OutcomeRecord:
    """Compose a CLAIM and a VERDICT into the run's report card.

    The truth table, which is the specification and not an illustration:

    ==================  ==================  ==================  ================
    self-reported       gate                ``gate_passed``     ``outcome_class``
    ==================  ==================  ==================  ================
    favourable          passed              ``True``            favourable
    favourable          failed              ``False``           adverse
    favourable          absent / vacuous    ``None``            neutral
    neutral             passed              ``True``            neutral
    neutral             anything else       as ruled / ``None`` neutral
    adverse             anything            as ruled / ``None`` adverse
    ==================  ==================  ==================  ================

    Read the last two rows carefully, because they are where the value is.

    **Neutral plus a passing gate accepts nothing.** An idle tick with a green
    gate is still an idle tick: the gate graded a workspace, not a result the
    loop produced. If a passing gate could raise a neutral claim, then a loop
    that parks every tick in a repository whose tests happen to pass would score
    a perfect acceptance rate having healed nothing — which is the exact
    incident this module exists to prevent.

    **Adverse short-circuits.** A tick that crashed is adverse whatever any gate
    says, and the runtime need not run the gate at all in that case: a crashing
    tick must still trip the acceptance floor with no gate available. When a gate
    *was* run anyway, its ruling is still recorded in :attr:`gate_passed`,
    because that column is a factual statement about the gate and not a summary
    of the composition.

    **A vacuous receipt is an absent one.** A gate that collected zero checks did
    not rule, whatever its exit status said, so it lands in the ``None`` column
    beside "no gate configured". A ``GateRunner`` is obliged to raise
    :class:`~selfloop.contracts.GateUnavailable` rather than hand one back, but
    composition refuses it here too — a rule this important should not have
    exactly one enforcement point.

    ``gate_passed is None`` means the gate DID NOT RUN. It never means the gate
    failed. Conflating the two feeds the auto-pause floor with absences, and a
    loop that pauses itself because its verifier was uninstalled has been
    disabled by its own monitoring.
    """
    claim = outcome_class(self_reported)
    ruled: bool | None = None if gate is None or gate.is_vacuous else bool(gate.passed)

    if claim == "favourable":
        if ruled is True:
            composed = "favourable"
        elif ruled is False:
            composed = "adverse"
        else:
            composed = "neutral"
    else:
        # Neutral stays neutral (a gate cannot manufacture a result the loop did
        # not produce) and adverse stays adverse (a gate cannot argue a crash
        # away). May lower, never raise.
        composed = claim

    reason = gate_unavailable_reason
    if not reason and gate is not None and gate.is_vacuous:
        reason = "vacuous_gate"
    if not reason and gate is None:
        reason = "no_gate"

    status_value = (
        self_reported.value if isinstance(self_reported, LoopStatus) else str(self_reported)
    )
    return OutcomeRecord(
        id=record_id or run_id,
        run_id=run_id,
        instance_id=instance_id,
        template=template,
        at=at,
        self_reported_status=status_value,
        gate_passed=ruled,
        outcome_class=composed,
        scope=scope,
        failure_tag=failure_tag,
        detail=detail,
        gate_unavailable_reason="" if ruled is not None else reason,
        gate_detail="" if gate is None else gate.detail,
        checks_collected=0 if gate is None else int(gate.checks_collected),
    )


def settlement_of(record: OutcomeRecord) -> Settlement:
    """Read a composed outcome in the artifact vocabulary.

    The one bridge between the two three-valued scales in this module, and it is
    a projection rather than an identity: an accepted run settles ``OK``, an
    adverse run settles ``FAILED``, and everything else — every uncorroborated
    claim, every idle tick, every park — settles ``UNGATEABLE``.

    Note what this refuses to do. A favourable *claim* with no gate does not
    settle ``OK`` here, because :func:`compose` has already lowered it to
    neutral; there is no path through this function by which an ungraded run
    becomes a graded one. Anything not in the known vocabulary settles
    ``FAILED``, which follows :func:`selfloop.contracts.outcome_class`: a row
    the package cannot classify is a row it cannot vouch for, and it must trip
    the floor rather than quietly leave the denominator.
    """
    if record.outcome_class == "favourable" and record.gate_passed is True:
        return Settlement.OK
    if record.outcome_class == "neutral":
        return Settlement.UNGATEABLE
    if record.outcome_class == "favourable":
        # Favourable but uncorroborated. compose() should already have lowered
        # this to neutral; if a row arrives in that shape it was written by
        # something else, and an uncorroborated claim is not a pass.
        return Settlement.UNGATEABLE
    return Settlement.FAILED


@dataclass(frozen=True)
class AcceptanceFloor:
    """The result of an acceptance-floor evaluation, including "cannot tell".

    ``meets`` is ``None`` when nothing was gateable. An undecidable floor is
    reported as undecidable rather than as a pass or a failure, because both
    lies are load-bearing: reported as a pass, a loop that has verified nothing
    for a week looks healthy; reported as a failure, it pauses itself for having
    had nothing to do.
    """

    floor: float
    ok: int
    failed: int
    ungateable: int
    ratio: float | None
    meets: bool | None
    #: How many records were examined after the window was applied.
    considered: int = 0

    @property
    def gateable(self) -> int:
        """Records that could be graded: the floor's actual denominator."""
        return self.ok + self.failed

    def as_dict(self) -> dict[str, object]:
        return {
            "floor": self.floor,
            "ok": self.ok,
            "failed": self.failed,
            "ungateable": self.ungateable,
            "gateable": self.gateable,
            "ratio": self.ratio,
            "meets": self.meets,
            "considered": self.considered,
        }


def acceptance_floor(
    records: Iterable[OutcomeRecord],
    window: int = 20,
    *,
    floor: float = 1.0,
) -> AcceptanceFloor:
    """Evaluate the newest *window* outcomes against *floor*.

    Ungateable outcomes are removed from BOTH the numerator and the denominator,
    so a run that could not be graded neither passes nor fails the floor on its
    own account. That is the difference between a floor that measures the loop
    and a floor that measures how much work there was to do.

    *records* must be in oldest-to-newest order; the newest *window* are taken.
    Ordering is the caller's obligation because the ledger's natural order — the
    event cursor — is not visible on an :class:`~selfloop.ledger.OutcomeRecord`,
    and sorting by the record stamp here would silently reintroduce the
    wall-clock dependency the cursor exists to remove.

    ``window <= 0`` considers every record supplied.

    The default ``floor`` of 1.0 means "every gradeable run in the window must
    have been accepted". It is deliberately the strict end: a caller who wants a
    looser bar is making a policy statement and should have to type it.
    """
    considered = list(records)
    if window > 0:
        considered = considered[-window:]

    ok = failed = ungateable = 0
    for record in considered:
        settlement = settlement_of(record)
        if settlement is Settlement.OK:
            ok += 1
        elif settlement is Settlement.FAILED:
            failed += 1
        else:
            ungateable += 1

    gateable = ok + failed
    ratio = (ok / gateable) if gateable else None
    meets = None if ratio is None else ratio >= floor
    return AcceptanceFloor(
        floor=floor,
        ok=ok,
        failed=failed,
        ungateable=ungateable,
        ratio=ratio,
        meets=meets,
        considered=len(considered),
    )


__all__ = [
    "AcceptanceFloor",
    "Settlement",
    "acceptance_floor",
    "artifact_bytes",
    "classify_settlement",
    "compose",
    "counts_toward_acceptance_floor",
    "settlement_of",
]
