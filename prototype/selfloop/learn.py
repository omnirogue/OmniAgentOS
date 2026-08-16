"""Stages 3 to 10: the closed learning loop. This is the point of the package.

Stages 1 and 2 belong to the runtime — a tick acts, and
:func:`selfloop.outcome.compose` grades what it claimed against what an
independent gate ruled. Everything after that lives here: mining the settled
record for signals, clustering them, staging a candidate, gating its promotion,
recalling it, injecting it, attributing the result back to it, and retiring it
when it stops earning its place.

**The failure this module exists to prevent.** In the system this package was
extracted from, all three learning loops were correctly wired and one hundred
per cent starved: 207 candidates staged, zero ever promoted, forever, because
the promotion score was structurally always ``0.0``. Nothing was broken in a way
a test would catch. Every function existed, every call site was right, and the
gate was mathematically unsatisfiable. A design that is "correct" but starves is
the failure mode, so every default in this file has been checked against one
question — *can this ever actually fire?* — and the answer is written down beside
it.

Four independent routes to that same starvation were found in review, and each
one is closed by a specific rule below. They are listed here together because
they rhyme, and a future change that reopens any of them will look reasonable in
isolation:

1. **Promotion gated on post-injection counters.** ``helped`` and ``used`` are
   written by :func:`attribute`, which only runs after a lesson has been promoted
   and injected. At first promotion ``used == 0``, so
   :func:`~selfloop.stats.wilson_lower_bound` returns ``0.0`` and any positive
   threshold is unsatisfiable. :func:`promote` therefore reads **only**
   pre-injection evidence — ``support`` and ``evidence_consistency`` — and never
   touches either counter. The Wilson bound is used for :func:`recall` ranking
   and for regression retirement in :func:`attribute`, and nowhere else.
2. **A candidate id that moves as evidence arrives.** The predecessor derived the
   id from the content key *plus the evidence ids*, so every new contributing run
   minted a fresh row and no candidate ever accumulated support. Here the id is
   :func:`candidate_id`, derived from the stable content key alone, and evidence
   is an appended set inside the payload written through a compare-and-set.
3. **A content key that moves as evidence arrives.** The same bug wearing a
   different hat, and the one that is easiest to reintroduce: the source derived
   the key from the *tokens of the clustered signals*, which grow with every new
   report. :attr:`Cluster.key` is derived from ``(scope, failure_tag)`` alone —
   see :func:`cluster` for why that is not a loss of resolution.
4. **A recall floor.** A freshly promoted lesson has ``used == 0`` and therefore
   a Wilson bound of exactly ``0.0``. Applying a threshold in :func:`recall`
   would make it unrecallable, the feedback edge would never close, and the loop
   would starve one stage further along than it used to. :func:`recall` ranks by
   that bound and never filters on it.

**Three hard rules on where a signal may come from**, all learned expensively:

* Signals are mined **after the fact, from the durable record** — never from a
  hook on the hot path. That is what makes this pass re-runnable and idempotent:
  the cursor advances only when a pass completes, so a crash re-mines rather than
  skips, and re-mining lands on the same content-stable ids.
* **No signal may be derived from the actor's own output text.** The source
  system promoted knowledge on ``output_text LIKE '%fact_id=<id>%'``, so an agent
  that wrote ``fact_id=42`` into its own report promoted fact 42. Here, a
  signal's EXISTENCE and its PARTITION come from structured columns written by
  the grading path — an outcome class, a failure tag, a verifier's boolean.
  Free text only ever contributes *tokens* inside a partition that has already
  been established by those columns, where the worst it can do is change which
  wording a human reads on the approvals page.
* **No signal may be derived from a NEUTRAL status.** "A stage produced no
  artifact" is true of every idle, parked and authorization-blocked tick, all of
  which are working as designed, and at a similarity threshold of 0.3 that token
  soup clusters fast and auto-promotes noise.

**Import direction is one-way: ``kit`` imports ``learn``, and ``learn`` never
imports ``kit``.** :func:`lesson_block` lives here, not in the kit, precisely so
that edge cannot become a cycle — and there is no ``add_learn`` graph node, so
the learning pass has exactly one owner: ``runtime.run_once`` calls
:func:`learning_pass` after settlement, always.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from selfloop import approvals
from selfloop.context import LoopContext
from selfloop.contracts import (
    APPROVAL_FLOOR_TIER,
    DECIDED_LESSON_STATUSES,
    ActionClass,
    ApprovalState,
    GateVerdict,
    LearningSignal,
    Lesson,
    LessonStatus,
    LoopError,
    LoopTool,
    RecordKind,
    RiskTier,
    args_digest,
    digest_key,
    lesson_fingerprint,
)
from selfloop.guidance import guidance_for, is_template_derived
from selfloop.ledger import (
    DEFAULT_CURSOR_NAME,
    LESSON_USE_ATTRIBUTED,
    LESSON_USE_PENDING,
    RECEIPT_FAILED,
    LessonUseRecord,
    OutcomeRecord,
    ReceiptRecord,
    advance_cursor,
    emit,
    lesson_use_id,
    read_cursor,
    read_events,
    write_history,
)
from selfloop.outcome import acceptance_floor
from selfloop.ports import SignalSource
from selfloop.receipts import EFFECT_EVENT_KIND
from selfloop.stats import decay_weight, jaccard, normalise_tokens, wilson_lower_bound

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Event channel for everything this module records. An operator grepping a log
#: for why a lesson did or did not move reads exactly one channel.
LEARNING_EVENT_KIND = "learning"

#: The event action :mod:`selfloop.receipts` writes when an attempt ran and did
#: not take effect. Restated rather than imported because it is not a published
#: constant there; :data:`EFFECT_EVENT_KIND` is, and is imported, so the channel
#: cannot drift. A counterfeit entry mutates this string and requires the
#: end-to-end liveness test to go red, which is what stops the two spellings
#: separating silently — the exact failure mode
#: :class:`~selfloop.contracts.RecordKind` exists to prevent.
EFFECT_FAILED_ACTION = "effect_failed"

#: Failure tag for the disagreement between a tool's own report and its
#: independent verifier. The disagreement IS the lesson: a tool that says it
#: succeeded while the thing that went and looked says otherwise is the single
#: most informative row in the ledger, and it is invisible to any grading scheme
#: that reads only one of the two columns.
TAG_VERIFY_DISAGREEMENT = "effect_verify_disagreement"

#: Failure tag for a recorded effect failure where the tool and its verifier
#: agreed, or where no verifier ruled.
TAG_EFFECT_FAILED = "effect_failed"

#: How many events one extraction pass reads. A bound rather than "everything",
#: because the first pass over a long-lived log would otherwise read the whole
#: history in one tick. The cursor makes the truncation harmless: the next pass
#: continues from where this one stopped.
DEFAULT_EVENT_SCAN_LIMIT = 500

#: Jaccard similarity at or above which two signals in the same partition are
#: taken to describe the same failure. See :func:`cluster` for the much more
#: important rule: this only ever runs *inside* a ``(scope, failure_tag)``
#: partition, because raw-token similarity conflates unrelated failures — they
#: all contain "error", "failed" and "line" — into one trash cluster whose lesson
#: is an amalgamation of contradictory fixes.
SIMILARITY_THRESHOLD = 0.3

#: Hard cap on a rendered claim. The claim is stored, fingerprinted and shown to
#: a human; an unbounded one turns an approvals page into a stack trace.
MAX_CLAIM_CHARS = 200

#: How many of a scope's outcomes a baseline or post-promotion measurement reads.
DEFAULT_BASELINE_WINDOW = 50

#: How far the scope's acceptance bound must fall BELOW the baseline snapshotted
#: at promotion before :func:`attribute` retires a lesson for regression.
#:
#: Not zero, and the reason is stated honestly in :func:`attribute`: this is a
#: scope-level comparison, so it is confounded whenever several lessons are
#: injected together, and a margin is the cheap way to stop ordinary variance
#: retiring a good lesson. The correct fix is holdout runs, which v1 does not
#: ship.
REGRESSION_MARGIN = 0.10

#: Minimum gradeable post-promotion runs before a regression may retire a lesson.
#: Below this the comparison is noise, and auto-retiring on noise is how a flaky
#: weekend deletes everything the loop learned.
REGRESSION_MIN_RUNS = 4

#: Node name recorded on a lesson promotion's approval row. It is not a graph
#: node — there is no ``add_learn`` node, by design — but the approval machinery
#: describes every parked action by ``(instance, template, node, tool)`` and this
#: is the honest name for where the decision was taken.
PROMOTION_NODE = "learn.promote"

#: Tool name recorded on a lesson promotion's approval row. See
#: :func:`_promotion_authority` for why a promotion is described as a tool.
PROMOTION_TOOL = "selfloop.promote_lesson"

#: How many times a compare-and-set is re-attempted before this module gives up
#: and reports. Each attempt re-reads and recomputes; none of them retries
#: blindly, because a blind retry on a CAS is a lost update with extra steps.
CAS_ATTEMPTS = 3

#: Reasons a lesson can leave the promoted state, as stored on a retirement row.
RETIRED_REGRESSION = "regression"
RETIRED_DECAYED = "decayed"

#: Either shape of learning-signal source this module will call. See
#: :func:`_call_source`: :class:`~selfloop.ports.SignalSource` declares an object
#: with a ``name`` and an ``extract`` method, while
#: :attr:`~selfloop.context.LoopContext.signal_sources` describes plain
#: functions. Both are accepted rather than picking one and quietly dropping the
#: other caller's sources.
SignalSourceLike = SignalSource | Callable[..., Iterable[LearningSignal]]


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _now(ctx: LoopContext) -> str:
    """The context clock's record stamp, or ``""`` if the clock is unusable.

    A stamp is documentation on most rows here. The one place it is load-bearing
    is age — :func:`decay` and the post-promotion window in :func:`attribute` —
    and both of those treat an unreadable stamp as "cannot place this in time"
    and keep the lesson, rather than acting on a number they do not trust.
    """
    try:
        return ctx.clock.now_iso()
    except Exception:  # noqa: BLE001 - an unusable clock must not fail a learning pass
        return ""


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 record stamp, or ``None``. Naive stamps are read as UTC.

    ``None`` rather than an exception, because every caller in this module has a
    fail-closed answer for "I cannot place this in time" and an exception would
    take that decision away from the code that knows which direction is safe.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _age_days(ctx: LoopContext, stamp: Any) -> float | None:
    """Age of *stamp* in days as of the context clock, or ``None``.

    This is one of the few places in the package that reads a WALL-CLOCK record
    stamp rather than the monotonic clock, and it is a considered exception. A
    monotonic reading has a per-process origin, so it cannot express "this lesson
    was promoted eleven days ago" to a different process on a different day,
    which is exactly what decay is about. The cost is stated where it is paid:
    :func:`decay` KEEPS a lesson whose stamp it cannot read.
    """
    then = _parse_iso(stamp)
    if then is None:
        return None
    now = _parse_iso(_now(ctx))
    if now is None:
        return None
    return (now - then).total_seconds() / 86400.0


def _canonical_text(text: str) -> str:
    """One trimmed line, capped at :data:`MAX_CLAIM_CHARS`.

    Whitespace is collapsed rather than preserved because a claim is rendered
    into a one-lesson-per-line block, and a claim containing a newline would look
    like a second lesson to whatever reads that block next.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_CLAIM_CHARS:
        return collapsed
    return collapsed[:MAX_CLAIM_CHARS].rstrip() + " …"


def candidate_id(key: str) -> str:
    """``cand_<12 hex>`` for a stable content *key*.

    The id preimage is the key and **nothing else** — in particular not the
    evidence ids. Deriving it from the key plus the evidence, as the predecessor
    did, changes the id every time a new run contributes evidence, so an
    insert-once store mints a fresh row each time and no candidate ever
    accumulates support. That is starvation by a second, entirely independent
    route from the one the promotion gate had, and it would be invisible in any
    test that stages a candidate once.
    """
    return "cand_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _signal_id(scope: str, failure_tag: str, evidence_key: str) -> str:
    """``sig_<16 hex>`` naming one durable piece of evidence.

    Derived from the evidence's own identity — an outcome row id, a receipt
    attempt key — and never from the cursor or the run that mined it. That is
    what makes re-mining idempotent: a pass that crashes and re-runs produces the
    same ids, ``put_once`` refuses the duplicates, and the first attribution of
    each signal to a run stands. Without content-stable ids, a crash mid-pass
    would inflate support with evidence from runs that never happened.
    """
    return "sig_" + digest_key("signal", scope, failure_tag, evidence_key)[:16]


def read_lesson(ctx: LoopContext, lesson_id: str) -> Lesson | None:
    """The stored lesson, or ``None``. Never invents a row."""
    row = ctx.records.get(RecordKind.LESSON.value, lesson_id)
    if row is None:
        return None
    return _lesson_from(row)


def _lesson_from(row: Mapping[str, Any]) -> Lesson | None:
    """Rebuild a :class:`~selfloop.contracts.Lesson`, or ``None`` for a bad row.

    A row this package cannot parse is not a lesson. Returning ``None`` keeps it
    out of promotion, recall and attribution alike, which is the fail-closed
    direction: the alternative is an exception from deep inside a pass, which
    would abort the whole pass on account of one corrupt row and take every other
    lesson down with it.
    """
    try:
        return Lesson.from_payload(row)
    except (KeyError, TypeError, ValueError):
        return None


def _read_signal(ctx: LoopContext, signal_id: str) -> LearningSignal | None:
    """The stored signal, or ``None`` for a missing or unparseable row."""
    row = ctx.records.get(RecordKind.SIGNAL.value, signal_id)
    if row is None:
        return None
    try:
        return LearningSignal.from_payload(row)
    except (KeyError, TypeError, ValueError):
        return None


def _outcome_for_run(ctx: LoopContext, run_id: str) -> OutcomeRecord | None:
    """The composed report card for *run_id*, or ``None`` if there is not one.

    ``None`` is a refusal, not a neutral: a piece of evidence whose run never
    settled cannot be counted toward promotion, because the whole admission rule
    is that a lesson may not be built on non-results.
    """
    if not run_id:
        return None
    row = ctx.records.get(RecordKind.OUTCOME.value, run_id)
    if row is None:
        rows = ctx.records.query(RecordKind.OUTCOME.value, run_id=run_id)
        if not rows:
            return None
        row = sorted(rows, key=lambda r: (str(r.get("at", "")), str(r.get("id", ""))))[-1]
    try:
        return OutcomeRecord.from_payload(row)
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# STAGE 3 — SIGNAL. Mine the settled record; never the hot path, never prose.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Harvest:
    """What one extraction pass found, and how far it read.

    :attr:`high_water` is returned rather than derived from the signals, and the
    difference matters: a pass that reads four hundred events and finds no signal
    must still be allowed to advance, or the loop re-reads the same four hundred
    events on every tick forever and never reaches the new ones.

    It is ``since_cursor`` — that is, *no advance* — whenever a source raised.
    A source that failed did not see its window, and advancing past a window
    nobody mined loses that evidence permanently and silently. Re-mining costs
    time; skipping costs evidence, and only one of those is recoverable.
    """

    signals: tuple[LearningSignal, ...]
    high_water: int
    events_read: int = 0
    #: Human-readable notes: signals dropped for want of a run id, sources that
    #: raised. Carried rather than logged-and-forgotten so a caller can put them
    #: in front of an operator.
    notes: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[LearningSignal]:
        return iter(self.signals)

    def __len__(self) -> int:
        return len(self.signals)


def _source_name(source: SignalSourceLike) -> str:
    """A name for *source* for the record, whichever shape it is."""
    name = getattr(source, "name", None)
    if isinstance(name, str) and name:
        return name
    return getattr(source, "__name__", None) or type(source).__name__


def _call_source(
    source: SignalSourceLike, ctx: LoopContext, since_cursor: int
) -> Iterable[LearningSignal]:
    """Invoke *source* in whichever of its two documented shapes it has.

    :class:`~selfloop.ports.SignalSource` declares an object with a ``name``
    attribute and an ``extract`` method;
    :attr:`~selfloop.context.LoopContext.signal_sources` documents plain
    functions of ``(ctx, *, since_cursor)``. Both of those files are frozen and
    they disagree, so picking one would make the other a lie — and the cost of
    the lie falls on a caller whose source is silently never called, which is
    precisely the class of failure this module exists to refuse. Accepting both
    costs four lines.
    """
    extractor = getattr(source, "extract", None)
    if callable(extractor):
        return extractor(ctx, since_cursor=since_cursor)
    if callable(source):
        return source(ctx, since_cursor=since_cursor)
    raise TypeError(
        f"signal source {_source_name(source)!r} is neither callable nor an object with an "
        "extract() method; see selfloop.ports.SignalSource for the shape"
    )


def _receipt_for(ctx: LoopContext, attempt_key: str) -> ReceiptRecord | None:
    """The mirrored receipt row for one attempt key, or ``None``."""
    row = ctx.records.get(RecordKind.RECEIPT.value, attempt_key)
    if row is None:
        return None
    try:
        return ReceiptRecord.from_payload(row)
    except (KeyError, TypeError, ValueError):
        return None


def _effect_scope(receipt: ReceiptRecord | None, node: str, tool: str) -> str:
    """The learning scope an effect failure belongs to: its NODE.

    The node is the part of the loop the lesson is about, and it is what an
    operator classifies in
    :attr:`~selfloop.context.LoopContext.scope_tiers`. Falling back to the tool
    name keeps a signal minable when the event lost its node, and returning
    ``""`` — which yields no signal at all — is the honest answer when neither is
    known: an unscoped lesson cannot be tiered, and an untiered lesson would
    either park forever or auto-promote something nobody classified.
    """
    if node:
        return node
    if receipt is not None and receipt.node:
        return receipt.node
    return tool


@dataclass(frozen=True)
class _EffectFailureSource:
    """Shared walk for the two effect-derived sources.

    Both read the SAME events — every ``effect_failed`` the receipt guard wrote
    after the cursor — and then split on one structured column, ``declared_success``:
    a tool that claimed success while its verifier said no is a *disagreement*,
    and everything else is a plain recorded failure. Splitting on a column rather
    than on two independent scans is what stops the two sources from double-mining
    one event into two signals for the same fact.

    The facts come from the mirrored :class:`~selfloop.ledger.ReceiptRecord`,
    which the event does not carry; the event supplies the cursor ordering and
    the node. When the mirror is missing — it is a best-effort cache write — the
    disagreement source yields nothing for that attempt, because absence of the
    tool's own claim can never be read as a claim.
    """

    name: str
    #: True to claim the disagreement rows, False to claim everything else.
    disagreement: bool
    limit: int = DEFAULT_EVENT_SCAN_LIMIT

    def extract(self, ctx: LoopContext, *, since_cursor: int) -> list[LearningSignal]:
        signals: list[LearningSignal] = []
        for event in read_events(ctx, after=since_cursor, limit=self.limit):
            if event.kind != EFFECT_EVENT_KIND or event.action != EFFECT_FAILED_ACTION:
                continue
            attempt_key = str(event.payload.get("receipt") or "")
            if not attempt_key:
                continue
            receipt = _receipt_for(ctx, attempt_key)
            if receipt is not None and receipt.outcome != RECEIPT_FAILED:
                # Only a decisive negative is evidence. An ``unavailable`` row
                # proves nothing happened and an ``unknown`` row proves nothing
                # at all; mining either as a failure manufactures a verdict out
                # of an absence, which is the mistake the whole receipt taxonomy
                # exists to prevent.
                continue
            declared = None if receipt is None else receipt.declared_success
            disagrees = declared is True and self._verified(event, receipt) is False
            if disagrees != self.disagreement:
                continue

            tool = str(event.payload.get("tool") or (receipt.tool if receipt else ""))
            scope = _effect_scope(receipt, event.node, tool)
            if not scope:
                continue
            tag = TAG_VERIFY_DISAGREEMENT if self.disagreement else TAG_EFFECT_FAILED
            detail = str(event.payload.get("detail") or (receipt.detail if receipt else ""))
            signals.append(
                LearningSignal(
                    id=_signal_id(scope, tag, attempt_key),
                    scope=scope,
                    failure_tag=tag,
                    text=_canonical_text(f"{tool}: {detail}" if tool else detail),
                    # Left empty on purpose. A receipt carries no run id, so this
                    # source cannot honestly claim one; :func:`extract` fills it
                    # from the run whose pass is mining this window. See the
                    # note there for why that attribution is sound.
                    run_id=event.run_id,
                    cursor=event.cursor,
                )
            )
        return signals

    @staticmethod
    def _verified(event: Any, receipt: ReceiptRecord | None) -> bool | None:
        """What an INDEPENDENT verifier ruled, or ``None`` if none ruled.

        Read from the mirrored receipt first and from the event payload only as a
        fallback, because the receipt row is the record the seam wrote for
        exactly this purpose. ``None`` is never laundered into ``False``: an
        effect whose independent check did not run has not been checked, and
        reading that as a disagreement would invent the most interesting signal
        in the ledger out of nothing.
        """
        if receipt is not None:
            return receipt.verified
        value = event.payload.get("verified")
        return None if value is None else bool(value)


@dataclass(frozen=True)
class _AdverseOutcomeSource:
    """Adverse :class:`~selfloop.ledger.OutcomeRecord` rows carrying a failure tag.

    This source does **not** read the event cursor, and the reason is worth
    stating plainly rather than hiding: an outcome is written by the settlement
    path as a record, with no event of its own, so there is no cursor to bound it
    by. Its correctness comes from the other half of the mechanism —
    content-stable signal ids plus ``put_once`` — which makes re-mining idempotent
    whether or not a cursor narrowed the scan. The cursor bounds the *event*
    scan; it was never the thing that made extraction exactly-once.

    The cost is an over-fetch: every adverse outcome in the store is read on
    every pass. That is correct at prototype scale and wrong at any real scale,
    exactly as :class:`~selfloop.ports.RecordStore` says of its equality-only
    query, and a production backend should index ``(kind, scope)``.

    A row with no ``scope`` or no ``failure_tag`` yields **no signal**. That is
    the correct outcome and not a gap to be filled with a placeholder: clustering
    partitions by ``(scope, failure_tag)`` before it compares a single token, so
    an untagged signal would join every other untagged signal into one
    meaningless cluster.
    """

    name: str = "adverse_outcome"

    def extract(self, ctx: LoopContext, *, since_cursor: int) -> list[LearningSignal]:
        del since_cursor  # see the class docstring: outcomes carry no cursor
        signals: list[LearningSignal] = []
        for row in ctx.records.query(RecordKind.OUTCOME.value, outcome_class="adverse"):
            try:
                record = OutcomeRecord.from_payload(row)
            except (KeyError, TypeError, ValueError):
                continue
            if not record.scope or not record.failure_tag or not record.run_id:
                continue
            signals.append(
                LearningSignal(
                    id=_signal_id(record.scope, record.failure_tag, f"outcome:{record.id}"),
                    scope=record.scope,
                    failure_tag=record.failure_tag,
                    text=_canonical_text(record.detail or record.failure_tag),
                    run_id=record.run_id,
                    # Zero, meaning "this source is not cursor-bounded". A cursor
                    # it does not have is better stated than guessed.
                    cursor=0,
                )
            )
        return signals


#: An effect that self-declared success and failed its own independent verifier.
#: The disagreement between the two columns IS the lesson.
verify_disagreement_signals = _EffectFailureSource(
    name="verify_disagreement", disagreement=True
)

#: An effect attempt recorded as a decisive failure, where the tool and its
#: verifier agreed or no verifier ruled.
failed_effect_signals = _EffectFailureSource(name="failed_effect", disagreement=False)

#: An adverse run outcome carrying a structured failure tag.
adverse_outcome_signals = _AdverseOutcomeSource()

#: The sources used when a caller declared none.
#:
#: An empty :attr:`~selfloop.context.LoopContext.signal_sources` means "the
#: caller did not choose", not "the caller chose nothing". Reading it as the
#: latter would make the shipped configuration mine nothing, promote nothing and
#: starve — which is the same hole as a package that ships no default gate, moved
#: one module along. A deployment that genuinely wants no learning does not call
#: :func:`learning_pass`.
DEFAULT_SIGNAL_SOURCES: tuple[SignalSourceLike, ...] = (
    verify_disagreement_signals,
    failed_effect_signals,
    adverse_outcome_signals,
)


def extract(
    ctx: LoopContext,
    *,
    since_cursor: int,
    run_id: str = "",
    limit: int = DEFAULT_EVENT_SCAN_LIMIT,
) -> Harvest:
    """Mine the settled record after *since_cursor*. Persist what is found.

    Every source registered on the context is asked; when none is registered the
    three shipped sources are used (see :data:`DEFAULT_SIGNAL_SOURCES`). Each
    signal is written with ``put_once``, and **the stored row wins**: when the
    write is refused because the signal already exists, the existing row is used
    in place of the freshly mined one. That single rule is what makes a crash
    mid-pass free of consequence — the same evidence re-mined by a later run
    keeps its original run attribution instead of being credited to the run that
    happened to re-read it, so support cannot be inflated by re-mining.

    *run_id* fills in a signal's run only when the source could not establish one
    — which is every effect-derived signal, because a receipt carries no run id.
    The attribution is sound because of how the window is bounded: the previous
    completed pass advanced the cursor to the end of its own run's events, one
    tick runs per process under a per-instance lease, and this pass runs after
    that tick has settled. The events between the two are that tick's. A signal
    that still has no run after this fill is DROPPED, not counted: support is a
    count of distinct runs, and evidence that cannot name a run would let one bad
    tick look like several.

    Raises nothing on a source's behalf. A source that raises is recorded in
    :attr:`Harvest.notes` and pins :attr:`Harvest.high_water` back to
    *since_cursor*, so the pass does not advance past a window that source never
    saw.
    """
    events = read_events(ctx, after=since_cursor, limit=limit)
    # The floor at ``since_cursor`` is not redundant. An EventLog that hands back
    # rows at or below the cursor it was given — a stale replica, an adapter that
    # numbers per process — would otherwise walk the high-water mark backwards,
    # and a cursor that can go back re-mines settled history as if it were new.
    high_water = max(since_cursor, max((event.cursor for event in events), default=since_cursor))

    sources = list(ctx.signal_sources) or list(DEFAULT_SIGNAL_SOURCES)
    notes: list[str] = []
    mined: dict[str, LearningSignal] = {}
    failed_source = False

    for source in sources:
        name = _source_name(source)
        try:
            produced = list(_call_source(source, ctx, since_cursor))
        except Exception as exc:  # noqa: BLE001 - one broken source must not lose the rest
            failed_source = True
            notes.append(f"signal source {name!r} raised {type(exc).__name__}: {exc}")
            emit(
                ctx,
                LEARNING_EVENT_KIND,
                "signal_source_failed",
                {"source": name, "error": f"{type(exc).__name__}: {exc}"},
                run_id=run_id,
            )
            continue
        for signal in produced:
            if not isinstance(signal, LearningSignal):
                notes.append(
                    f"signal source {name!r} yielded a {type(signal).__name__}, not a "
                    "LearningSignal; ignoring it"
                )
                continue
            resolved = signal if signal.run_id else _with_run(signal, run_id)
            if not resolved.run_id:
                notes.append(
                    f"signal {signal.id} from {name!r} names no run and none could be "
                    "supplied; dropped rather than counted toward support"
                )
                continue
            mined.setdefault(resolved.id, resolved)

    stored = tuple(_persist_signal(ctx, signal) for signal in mined.values())
    if stored:
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "signals_extracted",
            {"count": len(stored), "sources": [_source_name(s) for s in sources]},
            run_id=run_id,
        )
    return Harvest(
        signals=stored,
        high_water=since_cursor if failed_source else high_water,
        events_read=len(events),
        notes=tuple(notes),
    )


def _with_run(signal: LearningSignal, run_id: str) -> LearningSignal:
    """A copy of *signal* attributed to *run_id*."""
    return LearningSignal(
        id=signal.id,
        scope=signal.scope,
        failure_tag=signal.failure_tag,
        text=signal.text,
        run_id=run_id,
        cursor=signal.cursor,
        evidence_grade=signal.evidence_grade,
    )


def _persist_signal(ctx: LoopContext, signal: LearningSignal) -> LearningSignal:
    """Write *signal* as history and return whichever row is authoritative.

    ``put_once`` refusing the write is the normal path on a re-mine, and the
    EXISTING row is the answer — including its run id. Overwriting it would
    re-credit settled evidence to whichever run last re-read it, and because
    support counts distinct runs, that is how a single crash would turn one bad
    tick into two runs' worth of evidence.
    """
    if write_history(ctx, RecordKind.SIGNAL, signal.id, signal.as_dict()):
        return signal
    return _read_signal(ctx, signal.id) or signal


# ---------------------------------------------------------------------------
# STAGE 4 — CLUSTER. Partition first, compare tokens second.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cluster:
    """One ``(scope, failure_tag)`` partition's worth of evidence, plus a claim.

    :attr:`key` is derived from ``scope`` and ``failure_tag`` alone, and that is
    a deliberate departure from the source system, which derived it from the
    *tokens of the clustered signals*. Tokens grow with every new report, so the
    key moved every time the same failure recurred, every recurrence minted a new
    candidate, and no candidate ever accumulated support. It is the same
    starvation as the unsatisfiable promotion score, reached from a completely
    different direction, and it would survive any test that clusters one batch.

    The resolution that is lost by keying on the tag is not real. A lesson's
    injected payload is its ``guidance``, and guidance is
    ``when <failure_tag> then <remedy>`` — a function of the tag and nothing
    else. Two "different" clusters inside one ``(scope, failure_tag)`` partition
    would therefore carry identical guidance, so they are one lesson with two
    descriptions, and keeping them apart would only duplicate a line in every
    injected block.
    """

    scope: str
    failure_tag: str
    claim: str
    key: str
    signals: tuple[LearningSignal, ...]

    @property
    def lesson_id(self) -> str:
        """The candidate id this cluster stages to."""
        return candidate_id(self.key)

    @property
    def signal_ids(self) -> tuple[str, ...]:
        return tuple(sorted(signal.id for signal in self.signals))

    @property
    def run_ids(self) -> tuple[str, ...]:
        """The DISTINCT runs that contributed evidence, sorted."""
        return tuple(sorted({signal.run_id for signal in self.signals if signal.run_id}))

    @property
    def support(self) -> int:
        """Distinct contributing runs in THIS batch — never the number of signals.

        Ten signals from one bad run are one run's worth of evidence. Counting
        them as ten is how a single flaky night promotes a lesson, and it is why
        every count in this module that feeds a gate is a count of runs.

        This is evidence BREADTH and not an admission number: it does not check
        that each run settled non-neutrally. :func:`_weigh_evidence` does, and
        the number it produces is the one stored on the lesson row and read by
        the gate — so the two can never disagree about what "support" means.
        """
        return len(self.run_ids)

    @property
    def evidence_consistency(self) -> float:
        """Share of this cluster's signals agreeing on one failure tag.

        ``1.0`` by construction under this module's clusterer, because a cluster
        IS a tag partition. It is computed honestly rather than hardcoded so that
        the number means the same thing when a caller supplies a looser clusterer
        of their own, and so :func:`promote` can apply one rule to both.
        """
        return _consistency(signal.failure_tag for signal in self.signals)


def _consistency(tags: Iterable[str]) -> float:
    """Share of *tags* equal to the most common one. Empty is ``0.0``.

    Zero for no evidence, never one. "Everything agrees" is a true statement
    about an empty set and a false statement about the world, and a gate that
    reads it as agreement would admit a candidate with nothing behind it.
    """
    counts: dict[str, int] = {}
    total = 0
    for tag in tags:
        counts[tag] = counts.get(tag, 0) + 1
        total += 1
    if total == 0:
        return 0.0
    return max(counts.values()) / total


def _components(signals: Sequence[LearningSignal]) -> list[list[LearningSignal]]:
    """Union-find over Jaccard similarity, within one partition.

    *signals* must already be sorted by id: component discovery over a
    similarity graph is order-dependent at the boundaries, and sorting first is
    what makes two passes over the same evidence produce the same components and
    therefore the same claim.
    """
    parent = list(range(len(signals)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]  # path halving
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    tokens = [normalise_tokens(signal.text) for signal in signals]
    for i in range(len(signals)):
        for j in range(i + 1, len(signals)):
            if jaccard(tokens[i], tokens[j]) >= SIMILARITY_THRESHOLD:
                union(i, j)

    grouped: dict[int, list[LearningSignal]] = {}
    for index, signal in enumerate(signals):
        grouped.setdefault(find(index), []).append(signal)
    return [grouped[root] for root in sorted(grouped)]


def cluster(signals: Iterable[LearningSignal]) -> list[Cluster]:
    """Group *signals* into candidate lessons. Partition first, then compare.

    **The partition is by ``(scope, failure_tag)`` and it happens before a single
    token is compared.** Raw-token Jaccard on its own conflates unrelated
    failures, because ``error``, ``failed``, ``in`` and ``line`` appear in
    everything, and at a threshold of 0.3 they form one enormous trash cluster
    whose "lesson" is an amalgamation of contradictory fixes. A group with no
    shared structured tag is not a cluster, however similar its words.

    Inside a partition, Jaccard union-find still runs, and it has one job:
    choosing the CLAIM. The structured tag decides membership; the tokens decide
    only which phrasing best represents the partition, which is the phrasing a
    human reads on the approvals page. The largest component with any text at all
    wins, ties broken lexicographically so the answer does not depend on
    iteration order.

    A partition whose signals are all empty of text is **rejected**, never
    emitted with ``claim=""``. Two empty token sets have a Jaccard similarity of
    exactly 1.0 — identical emptiness is identical — so without this rule a batch
    of textless signals would form one confident cluster around nothing.
    """
    partitions: dict[tuple[str, str], list[LearningSignal]] = {}
    for signal in sorted(signals, key=lambda s: s.id):
        if not signal.scope or not signal.failure_tag:
            continue
        partitions.setdefault((signal.scope, signal.failure_tag), []).append(signal)

    clusters: list[Cluster] = []
    for (scope, failure_tag), members in sorted(partitions.items()):
        claim = _claim_for(members)
        if not claim:
            continue
        clusters.append(
            Cluster(
                scope=scope,
                failure_tag=failure_tag,
                claim=claim,
                key=cluster_key(scope, failure_tag),
                signals=tuple(members),
            )
        )
    return clusters


def cluster_key(scope: str, failure_tag: str) -> str:
    """The stable content key of a ``(scope, failure_tag)`` partition.

    Legible prefix plus an unambiguous digest, so an operator reading a store can
    see which scope a candidate belongs to without decoding anything, while two
    different ``(scope, failure_tag)`` pairs can never compose the same key —
    which a plain ``f"{scope}/{failure_tag}"`` would allow the moment either
    value contained a slash.
    """
    return f"{scope}/{digest_key('lesson-key', scope, failure_tag)[:12]}"


def _claim_for(members: Sequence[LearningSignal]) -> str:
    """The canonical claim of a partition, or ``""`` when it has no text."""
    best: list[LearningSignal] | None = None
    best_rank: tuple[int, str] | None = None
    for component in _components(members):
        texts = sorted(
            {_canonical_text(signal.text) for signal in component if signal.text.strip()}
        )
        if not texts:
            continue
        rank = (-len(component), texts[0])
        if best_rank is None or rank < best_rank:
            best, best_rank = component, rank
    if best is None or best_rank is None:
        return ""
    return best_rank[1]


# ---------------------------------------------------------------------------
# EVIDENCE — the one definition of "support", shared by staging and the gate.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """Pre-injection evidence for one candidate. **Reads no counter.**

    Every field here comes from the ledger as it stood BEFORE the lesson was
    ever injected: distinct contributing runs, and whether those runs agree on
    one failure tag. Nothing in this record is derived from ``helped`` or
    ``used``, and nothing may be. Those counters are written by :func:`attribute`
    after a lesson has been promoted and injected, so at first promotion they are
    zero — and a gate that reads them is a gate that can never open.
    """

    support: int
    consistency: float
    run_ids: tuple[str, ...]
    counted: tuple[str, ...]
    #: ``signal id -> why it did not count``. Kept because "the candidate has
    #: support 1" is not an answer an operator can act on, and "three of its four
    #: evidence runs settled neutral" is.
    excluded: Mapping[str, str]


def _weigh_evidence(ctx: LoopContext, evidence_ids: Iterable[str]) -> Admission:
    """Weigh pre-injection evidence, and only that. **The one definition of support.**

    Called from :func:`stage` to write the counter and from :func:`promote` to
    decide admission, so the number an operator reads on a row and the number the
    gate reads can never mean two different things. A stored counter is a cache
    of this function's answer; the gate recomputes it rather than trusting the
    cache, because a counter is a thing a writer can be wrong about and the gate
    is the one place that must not be.

    Every evidence id must resolve to a stored signal, to a run, and to a
    **non-neutral** :class:`~selfloop.ledger.OutcomeRecord` for that run. A
    lesson may not be built on non-results: a park, an idle tick and an
    uncorroborated claim all settle neutral, and counting them would let a loop
    that produced nothing all week promote a rule about it.

    Absence at any step excludes the evidence. An unresolvable signal, a run with
    no report card, a report card this package cannot parse — none of them counts
    toward support, because absence of a verdict is never the most favourable
    outcome.
    """
    runs: dict[str, str] = {}
    counted: list[str] = []
    excluded: dict[str, str] = {}

    for signal_id in evidence_ids:
        signal = _read_signal(ctx, signal_id)
        if signal is None:
            excluded[signal_id] = "no stored signal for this evidence id"
            continue
        if not signal.run_id:
            excluded[signal_id] = "the signal names no run"
            continue
        outcome = _outcome_for_run(ctx, signal.run_id)
        if outcome is None:
            excluded[signal_id] = f"run {signal.run_id} has no outcome record"
            continue
        if outcome.outcome_class == "neutral":
            excluded[signal_id] = (
                f"run {signal.run_id} settled neutral ("
                f"{outcome.gate_unavailable_reason or 'nothing to grade'}); a lesson may "
                "not be built on non-results"
            )
            continue
        counted.append(signal_id)
        runs[signal.run_id] = signal.failure_tag

    return Admission(
        support=len(runs),
        consistency=_consistency(runs.values()),
        run_ids=tuple(sorted(runs)),
        counted=tuple(sorted(counted)),
        excluded=excluded,
    )


# ---------------------------------------------------------------------------
# STAGE 5 — STAGE. One row per key, content fixed at first staging.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageResult:
    """What one :func:`stage` call did to the store."""

    lesson_id: str
    key: str
    created: bool
    #: Evidence ids that were not already on the row.
    appended: tuple[str, ...] = ()
    support: int = 0
    #: Empty when the candidate was staged or extended; otherwise why not.
    refused: str = ""
    #: True when the refusal is the ordinary steady state — this key has already
    #: been promoted, rejected or retired. Flagged separately because the shipped
    #: outcome source re-mines settled evidence on every pass, so a promoted
    #: lesson's key is refused on every tick forever; surfacing that to an
    #: operator as a complaint would train them to ignore this module's notes,
    #: and the one time it says something real they would ignore that too.
    decided: bool = False

    @property
    def stored(self) -> bool:
        """True when this call created or extended a candidate.

        False for a refusal, including the ordinary ``decided`` one — where a row
        very much does exist, it is simply no longer a candidate.
        """
        return not self.refused


def stage(ctx: LoopContext, candidate: Cluster) -> StageResult:
    """Create the candidate row, or append this cluster's evidence to it.

    Three rules, and each one closes a hole that a reasonable implementation
    would leave open:

    **A decided key never resurrects.** A candidate that a human rejected, or
    that the machine retired for regression, must not come back as ``staged`` the
    next time the same failure recurs and the same cluster forms. Without that
    rule the retirement loop is a revolving door and every retirement is undone
    by the next occurrence of the failure that caused it.

    **The first staging fixes the content, forever.** Later passes append
    evidence and touch nothing else — not the claim, not the guidance, not the
    fingerprint. Rewriting the claim as evidence accumulates would move the
    content fingerprint under the promotion gate's feet, the time-of-check bind
    in :func:`promote` would see drift on every pass, and the candidate would be
    permanently unpromotable. That is a third independent route to starvation and
    it looks, in a diff, exactly like keeping the row up to date.

    **Guidance that is not template-derived forces the approval floor.** A
    failure tag with no entry in
    :attr:`~selfloop.context.LoopContext.remedy_table` yields guidance marked
    ``[needs-human]``, and such a lesson is staged at
    :data:`~selfloop.contracts.APPROVAL_FLOOR_TIER` whatever the scope's declared
    tier says — human text, human approval. The tier is computed once, here, and
    stored on the row, so a later edit of the guidance text cannot lower it.

    Evidence is appended through :meth:`~selfloop.ports.RecordStore.transition`,
    a compare-and-set, because the runtime's pass runs unattended while an
    operator may be at the CLI. Losing the CAS is not an error — someone else
    moved the row — so this re-reads and recomputes, up to
    :data:`CAS_ATTEMPTS` times, and never retries blindly.
    """
    lesson_id = candidate.lesson_id
    guidance = guidance_for(candidate, ctx.remedy_table)
    tier = ctx.scope_tier(candidate.scope)
    if not is_template_derived(guidance) and tier < APPROVAL_FLOOR_TIER:
        tier = APPROVAL_FLOOR_TIER

    for _ in range(CAS_ATTEMPTS):
        row = ctx.records.get(RecordKind.LESSON.value, lesson_id)
        if row is None:
            lesson = Lesson(
                id=lesson_id,
                key=candidate.key,
                scope=candidate.scope,
                claim=candidate.claim,
                guidance=guidance,
                status=LessonStatus.STAGED,
                support=_weigh_evidence(ctx, candidate.signal_ids).support,
                evidence_ids=candidate.signal_ids,
                fingerprint=lesson_fingerprint(candidate.scope, candidate.claim, guidance),
                tier=tier,
                created_at=_now(ctx),
            )
            if write_history(ctx, RecordKind.LESSON, lesson_id, lesson.as_dict()):
                emit(
                    ctx,
                    LEARNING_EVENT_KIND,
                    "lesson_staged",
                    {
                        "lesson": lesson_id,
                        "scope": candidate.scope,
                        "failure_tag": candidate.failure_tag,
                        "tier": tier.name,
                        "support": lesson.support,
                        "template_derived_guidance": is_template_derived(guidance),
                    },
                )
                return StageResult(
                    lesson_id=lesson_id,
                    key=candidate.key,
                    created=True,
                    appended=candidate.signal_ids,
                    support=lesson.support,
                )
            continue  # somebody created it between the read and the insert

        existing = _lesson_from(row)
        if existing is None:
            return StageResult(
                lesson_id=lesson_id,
                key=candidate.key,
                created=False,
                refused="the stored row is not a readable lesson; refusing to overwrite it",
            )
        if existing.status in DECIDED_LESSON_STATUSES:
            return StageResult(
                lesson_id=lesson_id,
                key=candidate.key,
                created=False,
                decided=True,
                refused=(
                    f"key {candidate.key!r} was already {existing.status.value}; a decided "
                    "key never resurrects, or retirement is a revolving door"
                ),
            )

        merged = tuple(sorted(set(existing.evidence_ids) | set(candidate.signal_ids)))
        new_ids = tuple(sorted(set(merged) - set(existing.evidence_ids)))
        support = _weigh_evidence(ctx, merged).support
        if not new_ids and support == existing.support:
            return StageResult(
                lesson_id=lesson_id,
                key=candidate.key,
                created=False,
                support=existing.support,
            )
        moved = ctx.records.transition(
            RecordKind.LESSON.value,
            lesson_id,
            expect={
                "status": existing.status.value,
                "evidence_ids": list(existing.evidence_ids),
            },
            set={"evidence_ids": list(merged), "support": support},
        )
        if moved:
            emit(
                ctx,
                LEARNING_EVENT_KIND,
                "lesson_evidence_appended",
                {"lesson": lesson_id, "added": list(new_ids), "support": support},
            )
            return StageResult(
                lesson_id=lesson_id,
                key=candidate.key,
                created=False,
                appended=new_ids,
                support=support,
            )

    return StageResult(
        lesson_id=lesson_id,
        key=candidate.key,
        created=False,
        refused=(
            f"lost the compare-and-set on {lesson_id} {CAS_ATTEMPTS} times; another writer "
            "is moving this row and the next pass will re-read it"
        ),
    )


# ---------------------------------------------------------------------------
# STAGE 6 — GATE. The honest promotion rule.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionResult:
    """What one :func:`promote` call decided."""

    lesson_id: str
    status: LessonStatus
    promoted: bool
    parked: bool
    reason: str
    approval_id: str = ""
    support: int = 0
    consistency: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "status": LessonStatus(self.status).value,
            "promoted": self.promoted,
            "parked": self.parked,
            "reason": self.reason,
            "approval_id": self.approval_id,
            "support": self.support,
            "consistency": self.consistency,
        }


def _verdict(
    lesson_id: str,
    status: LessonStatus,
    reason: str,
    *,
    promoted: bool = False,
    parked: bool = False,
    evidence: Admission | None = None,
    approval_id: str = "",
) -> PromotionResult:
    """Build a :class:`PromotionResult`. The gate has many exits and one shape.

    Every exit from :func:`promote` carries a reason a human can act on, and
    writing a dozen full constructor calls out longhand is how one of them
    quietly ends up missing the evidence numbers — which is exactly the field an
    operator needs when the answer is "not yet".
    """
    return PromotionResult(
        lesson_id=lesson_id,
        status=status,
        promoted=promoted,
        parked=parked,
        reason=reason,
        approval_id=approval_id,
        support=0 if evidence is None else evidence.support,
        consistency=0.0 if evidence is None else evidence.consistency,
    )


def _promotion_is_not_an_effect(**_: Any) -> Any:
    """The promotion "tool"'s callable, which exists only to refuse.

    :func:`selfloop.approvals.ensure_approval` describes every parked action in
    terms of a :class:`~selfloop.contracts.LoopTool`, because that is what an
    approval row binds to — and a lesson promotion IS an action, decided by the
    same machinery, so it is described the same way. Nothing invokes this: the
    tool is never registered in a
    :class:`~selfloop.contracts.ToolRegistry`, so no template can reach it, and
    it is never handed to the execution seam. It raises rather than returning,
    so that a future caller who does reach it gets a sentence instead of a
    silent ``None`` that looks like a successful effect.
    """
    raise LoopError(
        "a lesson promotion is not an invocable effect; this LoopTool exists only to give "
        "the approval row an action to bind to. Promotion is applied by learn.promote()."
    )


def _promotion_authority(tier: RiskTier) -> LoopTool:
    """The tool record a lesson promotion's approval row binds to.

    :attr:`~selfloop.contracts.ActionClass.ALWAYS_HUMAN` is declared rather than
    left to the tier default, which keeps the class — and therefore the approval
    binding — stable if a scope's tier is later raised from T2 to T3. A binding
    that shifts under a fixed id is how an approval becomes unusable and an
    effect parks forever with nothing in the record explaining why.
    """
    return LoopTool(
        name=PROMOTION_TOOL,
        tier=tier,
        call=_promotion_is_not_an_effect,
        action_class=ActionClass.ALWAYS_HUMAN,
        description="promote a learned lesson into the prompt of every future run in its scope",
    )


def _promotion_args(lesson: Lesson) -> dict[str, Any]:
    """What a human is asked to approve: the lesson's CONTENT, and nothing else.

    Deliberately free of ``support`` and of anything else that moves as evidence
    arrives. The argument digest is part of the approval id preimage, so a field
    that changes every tick would mint a fresh approval row every tick and page a
    human every tick — approval fatigue engineered from first principles. The
    fields that ARE here are exactly the ones whose change should invalidate a
    decision: a human approved this text, for this scope, at this fingerprint.
    """
    return {
        "lesson_id": lesson.id,
        "scope": lesson.scope,
        "claim": lesson.claim,
        "guidance": lesson.guidance,
        "fingerprint": lesson.fingerprint,
    }


def promote(ctx: LoopContext, lesson_id: str) -> PromotionResult:
    """The honest promotion gate. **Never reads ``helped`` or ``used``.**

    That prohibition is the single most important line in this package, so it is
    stated before anything else. ``helped`` and ``used`` are written by
    :func:`attribute`, which only runs after a lesson has been promoted and
    injected. At first promotion ``used == 0``, so
    :func:`~selfloop.stats.wilson_lower_bound` returns ``0.0`` and any threshold
    above zero makes this function unsatisfiable — correctly wired, mathematically
    always closed, 207 candidates staged and none ever promoted. Admission reads
    pre-injection evidence only: :class:`Admission`.

    Five conditions, all required:

    a. **Support.** At least :attr:`~selfloop.context.LoopContext.min_support`
       DISTINCT RUNS contributed evidence. Not signals — runs.
    b. **Every evidence id resolves to a non-neutral outcome.** A lesson may not
       be built on non-results.
    c. **Evidence consistency** at or above
       :attr:`~selfloop.context.LoopContext.min_evidence_consistency`: the
       counted evidence agrees on one failure tag.
    d. **A time-of-check/time-of-use re-bind.** The row is re-read and its
       content fingerprint recomputed immediately before the write, and any drift
       SKIPS. Accepting by id alone leaves a window in which the row's content
       changes between the check and the use, and unvalidated content then
       applies under a validated id — the source system had exactly that window
       in its proposal pipeline.
    e. **The risk tier of the lesson's SCOPE decides the path.** T0/T1
       auto-promotes on evidence. T2 and above goes through
       :func:`selfloop.approvals.ensure_approval` and PARKS — the same
       deterministic id, the same binding re-check, the same
       expiry-aborts-never-approves. *This is the design's central claim made
       literal: the learning loop's promotion gate IS the effect gate.*

    Every state change goes through :meth:`~selfloop.ports.RecordStore.transition`,
    a compare-and-set. Without it a lesson retired for regression can be
    resurrected to promoted by a writer that read the row a moment earlier.

    A rejected or expired approval **decides the candidate**: it moves to
    ``rejected`` and, because that is a decided status, its key never resurrects.
    The cost is stated rather than hidden — a failure kind nobody triaged stops
    being proposed, and the record says so. The alternative, re-parking on every
    pass, pages a human on a loop, and approval fatigue is how fifteen of sixteen
    approvals in one audited deployment expired undecided.
    """
    lesson = read_lesson(ctx, lesson_id)
    if lesson is None:
        return _verdict(lesson_id, LessonStatus.STAGED, "no lesson row exists for this id")
    if lesson.status in DECIDED_LESSON_STATUSES:
        return _verdict(lesson_id, lesson.status, f"already {lesson.status.value}")
    if not lesson.fingerprint_intact:
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "lesson_fingerprint_drift",
            {"lesson": lesson_id, "stored": lesson.fingerprint},
        )
        return _verdict(
            lesson_id,
            lesson.status,
            "the row's content no longer matches its stored fingerprint; skipping rather "
            "than recomputing, because recomputing to match is the bind being defeated",
        )

    evidence = _weigh_evidence(ctx, lesson.evidence_ids)
    if evidence.support < ctx.min_support:
        return _verdict(
            lesson_id,
            lesson.status,
            f"support {evidence.support} < min_support {ctx.min_support} "
            f"(distinct runs: {list(evidence.run_ids)}; excluded: {dict(evidence.excluded)})",
            evidence=evidence,
        )
    if evidence.consistency < ctx.min_evidence_consistency:
        return _verdict(
            lesson_id,
            lesson.status,
            f"evidence consistency {evidence.consistency:.2f} < "
            f"min_evidence_consistency {ctx.min_evidence_consistency:.2f}",
            evidence=evidence,
        )

    if lesson.tier >= APPROVAL_FLOOR_TIER:
        return _promote_through_approval(ctx, lesson, evidence)
    return _apply_promotion(ctx, lesson, evidence, approval_id="")


def _promote_through_approval(
    ctx: LoopContext, lesson: Lesson, evidence: Admission
) -> PromotionResult:
    """Route a T2+ lesson through the same approval machinery an effect uses.

    One row, one page, ever: the approval id is deterministic, so the next pass
    re-derives it, finds the row, and pages nobody. The tick that reaches here
    PARKS; resume is simply "the next :func:`learning_pass` retries the
    promotion", which is why there is no separate resume protocol and no
    ``add_learn`` node that would need one.
    """
    # Imported here rather than at module scope, for two reasons that both
    # matter. ``selfloop.tools`` installs the execution seam's sealer AT IMPORT,
    # and importing this module must not have that side effect for a caller who
    # only wants to read lessons. And the binding must be byte-identical to the
    # one ``ensure_approval`` stored — ``read_outcome`` refuses on any
    # difference, forever — so it is derived from the same function rather than
    # rebuilt here from the same six fields and left to drift.
    from selfloop.tools import effect_binding

    tool = _promotion_authority(lesson.tier)
    args = _promotion_args(lesson)
    verdict = GateVerdict(
        decision="park",
        tier=lesson.tier,
        action_class=ActionClass.ALWAYS_HUMAN,
        reason=(
            f"lesson {lesson.id} for scope {lesson.scope!r} is supported by "
            f"{evidence.support} distinct runs and would be injected into every future run "
            f"in that scope; scope tier is {lesson.tier.name}"
        ),
    )
    try:
        approvals.ensure_approval(
            ctx,
            node=PROMOTION_NODE,
            tool=tool,
            args=args,
            business_key=lesson.id,
            verdict=verdict,
        )
    except LoopError as exc:
        # A binding that drifted under a fixed id. Reported rather than raised,
        # because one undecidable lesson must not abort a pass that is also
        # promoting, attributing and retiring others.
        return _verdict(
            lesson.id,
            lesson.status,
            f"could not open an approval for this promotion: {exc}",
            evidence=evidence,
        )

    row_id = approvals.approval_id(
        ctx.instance_id, ctx.template, PROMOTION_NODE, PROMOTION_TOOL, lesson.id, args_digest(args)
    )
    binding = effect_binding(ctx, PROMOTION_NODE, tool, args)
    outcome = approvals.read_outcome(ctx, row_id, binding=binding)

    if outcome.approved:
        return _apply_promotion(ctx, lesson, evidence, approval_id=row_id)

    if outcome.state == ApprovalState.EXPIRED.value:
        # Close the row so an operator's inbox stops showing a request nobody can
        # usefully answer. The reading does not change: it was already expired.
        approvals.resolve_for_resume(ctx, row_id)

    if outcome.terminal:
        _transition(
            ctx,
            lesson,
            LessonStatus.REJECTED,
            {"approval_id": row_id, "retired_reason": outcome.state},
        )
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "lesson_rejected",
            {"lesson": lesson.id, "approval": row_id, "state": outcome.state},
        )
        return _verdict(
            lesson.id,
            LessonStatus.REJECTED,
            f"approval {row_id} is {outcome.state}: {outcome.reason}",
            evidence=evidence,
            approval_id=row_id,
        )

    if lesson.status != LessonStatus.PARKED:
        _transition(ctx, lesson, LessonStatus.PARKED, {"approval_id": row_id})
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "lesson_parked",
            {
                "lesson": lesson.id,
                "approval": row_id,
                "tier": lesson.tier.name,
                "support": evidence.support,
            },
        )
    return _verdict(
        lesson.id,
        LessonStatus.PARKED,
        f"awaiting a human on approval {row_id}",
        parked=True,
        evidence=evidence,
        approval_id=row_id,
    )


def _apply_promotion(
    ctx: LoopContext, lesson: Lesson, evidence: Admission, *, approval_id: str
) -> PromotionResult:
    """The write itself, behind a re-read and a recomputed fingerprint.

    The re-read is condition (d) of the gate, and it is deliberately done HERE,
    immediately before the compare-and-set, rather than at the top of
    :func:`promote`: the value of a time-of-check bind is entirely in how little
    happens between the check and the use.

    The scope's acceptance bound is SNAPSHOTTED here as ``baseline``, and that is
    the one place in the promotion path where a Wilson bound is computed. It is
    written and never compared: no threshold is applied to it, and a ``None``
    baseline — nothing gradeable in the scope yet — does not block the promotion.
    It exists so :func:`attribute` has a "before" to measure against later. A
    future change that turns this number into a condition would reintroduce the
    unsatisfiable gate from the other end, because a scope with no history has no
    bound at all.
    """
    fresh = read_lesson(ctx, lesson.id)
    if fresh is None or not fresh.fingerprint_intact or fresh.fingerprint != lesson.fingerprint:
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "lesson_fingerprint_drift",
            {"lesson": lesson.id, "stage": "promote"},
        )
        return _verdict(
            lesson.id,
            fresh.status if fresh else lesson.status,
            "the lesson's content moved between admission and promotion; skipped",
            evidence=evidence,
            approval_id=approval_id,
        )
    if fresh.status in DECIDED_LESSON_STATUSES:
        return _verdict(
            lesson.id,
            fresh.status,
            f"another writer moved it to {fresh.status.value} first",
            evidence=evidence,
            approval_id=approval_id,
        )

    baseline, gradeable = scope_acceptance(ctx, fresh.scope)
    changes: dict[str, Any] = {
        "status": LessonStatus.PROMOTED.value,
        "promoted_at": _now(ctx),
        "support": evidence.support,
        "baseline": baseline,
    }
    if approval_id:
        changes["approval_id"] = approval_id

    moved = ctx.records.transition(
        RecordKind.LESSON.value,
        fresh.id,
        expect={"status": fresh.status.value, "fingerprint": fresh.fingerprint},
        set=changes,
    )
    if not moved:
        return _verdict(
            fresh.id,
            fresh.status,
            "lost the compare-and-set on the promotion; the next pass will re-read",
            evidence=evidence,
            approval_id=approval_id,
        )

    emit(
        ctx,
        LEARNING_EVENT_KIND,
        "lesson_promoted",
        {
            "lesson": fresh.id,
            "scope": fresh.scope,
            "tier": fresh.tier.name,
            "support": evidence.support,
            "runs": list(evidence.run_ids),
            "baseline": baseline,
            "baseline_runs": gradeable,
            "approval": approval_id,
        },
    )
    return _verdict(
        fresh.id,
        LessonStatus.PROMOTED,
        f"promoted on {evidence.support} distinct runs of evidence",
        promoted=True,
        evidence=evidence,
        approval_id=approval_id,
    )


def _transition(
    ctx: LoopContext, lesson: Lesson, status: LessonStatus, extra: Mapping[str, Any] | None = None
) -> bool:
    """Compare-and-set a lesson's status from whatever it is now."""
    changes: dict[str, Any] = {"status": LessonStatus(status).value}
    changes.update(dict(extra or {}))
    return bool(
        ctx.records.transition(
            RecordKind.LESSON.value,
            lesson.id,
            expect={"status": lesson.status.value},
            set=changes,
        )
    )


def scope_acceptance(
    ctx: LoopContext,
    scope: str,
    *,
    since: str | None = None,
    window: int = DEFAULT_BASELINE_WINDOW,
) -> tuple[float | None, int]:
    """The Wilson lower bound on a scope's acceptance, and its sample size.

    ``(None, 0)`` when nothing in the window was gradeable, and that is a real
    answer rather than a zero. Neutral outcomes leave the sample entirely — they
    are removed from the numerator AND the denominator, exactly as
    :func:`selfloop.outcome.acceptance_floor` does it — so a week of idle ticks
    produces "cannot tell" and never "zero per cent accepted". A baseline of
    ``None`` snapshotted at promotion means :func:`attribute` has nothing to
    compare against and will not retire on regression, which is the correct
    fail-closed direction: you cannot demonstrate a fall from a level you never
    established.

    *since* restricts the window to outcomes stamped strictly after an ISO
    timestamp — the post-promotion half of the comparison. Records whose stamp
    cannot be parsed are EXCLUDED from a ``since`` window: evidence that cannot
    be placed in time cannot be shown to have come after anything.
    """
    boundary = _parse_iso(since) if since else None
    records: list[OutcomeRecord] = []
    for row in ctx.records.query(RecordKind.OUTCOME.value, scope=scope):
        try:
            record = OutcomeRecord.from_payload(row)
        except (KeyError, TypeError, ValueError):
            continue
        if boundary is not None:
            stamped = _parse_iso(record.at)
            if stamped is None or stamped <= boundary:
                continue
        records.append(record)

    records.sort(key=lambda r: (r.at, r.id))
    floor = acceptance_floor(records, window)
    if floor.gateable == 0:
        return None, 0
    return wilson_lower_bound(floor.ok, floor.gateable), floor.gateable


def pending_promotions(ctx: LoopContext) -> list[Lesson]:
    """Every lesson that is staged or parked, oldest first.

    Both statuses, and parked is the one that matters: a T2+ lesson waiting on a
    human is retried by every subsequent pass, which is the entire resume
    protocol for a parked promotion. Deliberately uncapped — a cap would put some
    lesson permanently at position N+1 and starve it, which is the failure this
    module exists to refuse, and the honest cost is that a store with thousands
    of open candidates does more work per tick.
    """
    rows = [
        *ctx.records.query(RecordKind.LESSON.value, status=LessonStatus.STAGED.value),
        *ctx.records.query(RecordKind.LESSON.value, status=LessonStatus.PARKED.value),
    ]
    lessons = [lesson for lesson in (_lesson_from(row) for row in rows) if lesson is not None]
    lessons.sort(key=lambda lesson: (lesson.created_at, lesson.id))
    return lessons


# ---------------------------------------------------------------------------
# STAGE 7 — RECALL. Promoted only, re-verified at read time.
# ---------------------------------------------------------------------------


def recall(
    ctx: LoopContext, scope: str, query: str = "", k: int | None = None
) -> list[Lesson]:
    """The promoted lessons for *scope*, best first, at most *k* of them.

    **Promoted only.** Never staged, never parked. A candidate that has not been
    through the gate must not reach a prompt by another door; that is the gate
    being bypassed, not a shortcut.

    **The fingerprint is re-verified at READ time**, not merely at promotion. A
    row whose content has drifted since it was promoted is skipped, because a
    verdict that attaches to an id rather than to content is transferable, and
    whatever text later occupies that id would inherit a promotion it never
    earned.

    Ranking is ``wilson_lower_bound(helped, used) * decay_weight(age)``, and
    **no floor is applied to it**. A lesson at its first recall has ``used == 0``
    and therefore a bound of exactly 0.0; filtering on that would make every
    newly promoted lesson unrecallable, the feedback edge would never close, and
    the loop would starve one stage further along than it used to. The threshold
    on :class:`~selfloop.context.LoopContext` is for ranking and for regression
    retirement — never for admission, at either end.

    *query* breaks ties by token overlap with the lesson's own text. It is a
    tie-break and never a filter: filtering on token overlap would silently hide
    a lesson that is relevant but differently phrased, and at ``k=3`` nobody
    would ever see that it had happened.
    """
    limit = ctx.recall_k if k is None else int(k)
    if limit <= 0:
        return []

    wanted = normalise_tokens(query)
    ranked: list[tuple[tuple[float, float, int, str], Lesson]] = []
    for row in ctx.records.query(
        RecordKind.LESSON.value, status=LessonStatus.PROMOTED.value, scope=scope
    ):
        lesson = _lesson_from(row)
        if lesson is None:
            continue
        if not lesson.fingerprint_intact:
            emit(
                ctx,
                LEARNING_EVENT_KIND,
                "lesson_skipped_on_drift",
                {"lesson": lesson.id, "scope": scope, "stage": "recall"},
            )
            continue
        age = _age_days(ctx, lesson.last_used_at or lesson.promoted_at or lesson.created_at)
        weight = 1.0 if age is None else decay_weight(age)
        score = wilson_lower_bound(lesson.helped, lesson.used) * weight
        relevance = jaccard(wanted, normalise_tokens(f"{lesson.claim} {lesson.guidance}"))
        ranked.append(((-score, -relevance, -lesson.support, lesson.id), lesson))

    ranked.sort(key=lambda pair: pair[0])
    return [lesson for _, lesson in ranked[:limit]]


# ---------------------------------------------------------------------------
# STAGE 8 — INJECT. The feedback edge, rendered once, recorded before the run.
# ---------------------------------------------------------------------------

#: Opening and closing delimiters of the injected block. Explicit, symmetrical
#: and boring, so that whatever consumes the prompt can tell where the loop's own
#: text starts and stops — and so a reader of a transcript can too.
LESSON_BLOCK_OPEN = "[selfloop lessons — scope: {scope}]"
LESSON_BLOCK_CLOSE = "[end selfloop lessons]"

#: One line of preamble. It says where the text came from, because an injected
#: block that reads like an operator's instruction is an injected block that will
#: eventually be obeyed over the operator's actual instruction.
LESSON_BLOCK_PREAMBLE = (
    "Promoted from this loop's own graded runs. Each line was admitted on evidence "
    "from distinct settled runs, not on anything a previous run asserted about itself."
)


def lesson_block(
    ctx: LoopContext, scope: str, query: str = "", *, run_id: str, k: int | None = None
) -> str:
    """Render the recalled lessons for *scope* into ONE bounded, labelled section.

    **This function is the feedback edge.** It is the only place in the package
    where something the loop learned turns back into something the loop reads,
    and ``kit`` calls it from exactly one line — pinned by
    ``test_lessons_are_injected_from_exactly_one_place`` and by a counterfeit
    entry that deletes that line and requires the end-to-end liveness test to go
    red. Everything upstream of here is bookkeeping; if this edge is severed the
    package is an audit trail with ambitions.

    Returns ``""`` when there is nothing to inject. An empty labelled section is
    noise in every prompt that carries it, and noise in a prompt is a cost paid
    on every tick forever.

    *run_id* is **required and keyword-only**. Passing it records a pending
    :class:`~selfloop.ledger.LessonUseRecord` for each injected lesson BEFORE the
    run produces any outcome, and that ordering is the whole reason stage 9 is an
    attribution rather than a correlation: the set of runs a lesson was in is
    committed in advance, not fished out of history afterwards, which is a shape
    that always finds something. Pass ``run_id=""`` for a preview render — the
    CLI does — and nothing is recorded. It has no default because a forgotten
    argument here does not fail; it silently stops attribution, the counters stay
    at zero forever, and the loop's ranking never learns anything. That is the
    failure mode this package exists to refuse, so the caller has to type it.
    """
    lessons = recall(ctx, scope, query, k)
    if not lessons:
        return ""

    # Recorded BEFORE the text is rendered, let alone returned. A use that could
    # not be persisted propagates and fails the tick, which is the fail-closed
    # direction: injecting a lesson whose use was not recorded means attribution
    # can never grade it, its counters stay at zero forever, and the loop quietly
    # stops learning from the one lesson it is actively using.
    if run_id:
        for lesson in lessons:
            record_use(ctx, lesson, run_id)

    lines = [LESSON_BLOCK_OPEN.format(scope=scope), LESSON_BLOCK_PREAMBLE]
    for index, lesson in enumerate(lessons, start=1):
        lines.append(f"{index}. {lesson.guidance}")
        lines.append(f'   evidence: {lesson.support} run(s); observed as "{lesson.claim}"')
    lines.append(LESSON_BLOCK_CLOSE)
    return "\n".join(lines)


def record_use(ctx: LoopContext, lesson: Lesson, run_id: str) -> bool:
    """Commit, in advance, that *lesson* was injected into *run_id*.

    Written ``put_once`` under a deterministic id, so the row exists exactly once
    even if a template renders the block twice within a tick, and so
    :func:`attribute` can address it directly instead of scanning for it.

    The row starts PENDING and its ``helped`` is ``None`` — not ``False``.
    ``None`` means "not attributed yet" and ``False`` means "attributed, did not
    help", and the difference between those two is the entire attribution signal.
    """
    record = LessonUseRecord(
        id=lesson_use_id(lesson.id, run_id),
        at=_now(ctx),
        lesson_id=lesson.id,
        run_id=run_id,
        instance_id=ctx.instance_id,
        template=ctx.template,
        scope=lesson.scope,
        state=LESSON_USE_PENDING,
        fingerprint=lesson.fingerprint,
    )
    return write_history(ctx, RecordKind.LESSON_USE, record.id, record.as_dict())


# ---------------------------------------------------------------------------
# STAGE 9 — ATTRIBUTE. The return edge, and the only place counters move.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionReport:
    """What :func:`attribute` did for one run."""

    run_id: str
    #: ``None`` when the run has no outcome record at all.
    outcome_class: str | None
    graded: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    skipped: str = ""

    @property
    def attributed(self) -> int:
        return len(self.graded)


def attribute(ctx: LoopContext, run_id: str) -> AttributionReport:
    """Grade the lessons that were injected into *run_id*, and retire regressions.

    **Only a non-neutral outcome finalises a use.** Parks, aborts, unknown effect
    states and idle ticks produce no judgeable result, and counting them as
    ``used`` without ``helped`` is how a flaky weekend auto-retires good lessons:
    the counters fill with non-results, the Wilson bound collapses, and the loop
    deletes what it learned on evidence that was never about the lessons at all.
    A neutral run therefore leaves its rows PENDING — permanently, since an
    outcome is history and a neutral run's report card never becomes non-neutral.
    A pending row that is never graded is the correct record of "this lesson was
    injected into a tick that produced nothing to judge"; rewriting it to
    ``attributed, helped=False`` would be the exact poisoning this rule forbids.

    **The regression check is scope-level, and that is a real limitation, stated
    plainly.** It compares the scope's Wilson bound over runs settled AFTER a
    lesson was promoted against the ``baseline`` snapshotted on the lesson at
    promotion time. When several lessons are injected together, that comparison
    cannot say which of them moved the number — it is confounded, and no amount
    of arithmetic here fixes it. Two guards make it survivable rather than
    correct: the drop must exceed :data:`REGRESSION_MARGIN`, and the post window
    must contain at least :data:`REGRESSION_MIN_RUNS` gradeable runs. **The
    correct fix is holdout runs** — inject a lesson into a randomly chosen
    fraction of runs in its scope and compare the two arms — which v1 does not
    ship, and which is where a reader who wants per-lesson attribution should
    start.

    A related caution, from the same review: the baseline and post windows should
    be measured by a gate that is not the proposal evaluator. When the same
    component both labels the training data and grades whether the lesson helped,
    a lesson that merely makes the evaluator happier scores as a lesson that
    worked.
    """
    outcome = _outcome_for_run(ctx, run_id)
    pending = [
        record
        for record in (
            _lesson_use_from(row)
            for row in ctx.records.query(
                RecordKind.LESSON_USE.value, run_id=run_id, state=LESSON_USE_PENDING
            )
        )
        if record is not None
    ]
    if outcome is None:
        return AttributionReport(
            run_id=run_id,
            outcome_class=None,
            skipped=(
                f"run {run_id} has no outcome record; its {len(pending)} lesson use(s) stay "
                "pending rather than being graded against a verdict that does not exist"
            ),
        )
    if outcome.outcome_class == "neutral":
        return AttributionReport(
            run_id=run_id,
            outcome_class=outcome.outcome_class,
            skipped=(
                f"run {run_id} settled neutral; its {len(pending)} lesson use(s) stay pending. "
                "Neutral outcomes are excluded from attribution exactly as they are from the "
                "acceptance floor"
            ),
        )

    helped = outcome.accepted
    graded: list[str] = []
    retired: list[str] = []
    for use in pending:
        moved = ctx.records.transition(
            RecordKind.LESSON_USE.value,
            use.id,
            expect={"state": LESSON_USE_PENDING},
            set={
                "state": LESSON_USE_ATTRIBUTED,
                "helped": helped,
                "outcome_class": outcome.outcome_class,
            },
        )
        if not moved:
            continue  # another writer graded it; its counters were moved with it
        if _bump_counters(ctx, use.lesson_id, helped=helped):
            graded.append(use.lesson_id)

    for lesson_id in dict.fromkeys(graded):
        if _retire_on_regression(ctx, lesson_id):
            retired.append(lesson_id)

    if graded:
        emit(
            ctx,
            LEARNING_EVENT_KIND,
            "lessons_attributed",
            {"lessons": graded, "helped": helped, "outcome_class": outcome.outcome_class},
            run_id=run_id,
        )
    return AttributionReport(
        run_id=run_id,
        outcome_class=outcome.outcome_class,
        graded=tuple(graded),
        retired=tuple(retired),
    )


def _lesson_use_from(row: Mapping[str, Any]) -> LessonUseRecord | None:
    try:
        return LessonUseRecord.from_payload(row)
    except (KeyError, TypeError, ValueError):
        return None


def _bump_counters(ctx: LoopContext, lesson_id: str, *, helped: bool) -> bool:
    """Move ``used`` and ``helped`` by one graded use, under a compare-and-set.

    The CAS expects the counters it read, so two passes grading two different
    runs of the same lesson cannot both write ``used = n + 1`` over each other.
    Losing it is not an error and is not retried blindly: the loop re-reads and
    tries again, and gives up after :data:`CAS_ATTEMPTS` rather than spinning.
    """
    for _ in range(CAS_ATTEMPTS):
        lesson = read_lesson(ctx, lesson_id)
        if lesson is None:
            return False
        moved = ctx.records.transition(
            RecordKind.LESSON.value,
            lesson_id,
            expect={"used": lesson.used, "helped": lesson.helped},
            set={
                "used": lesson.used + 1,
                "helped": lesson.helped + (1 if helped else 0),
                "last_used_at": _now(ctx),
            },
        )
        if moved:
            return True
    return False


def _retire_on_regression(ctx: LoopContext, lesson_id: str) -> bool:
    """Retire *lesson_id* when its scope has measurably regressed since promotion.

    Returns False — keeps the lesson — for every kind of "cannot tell": no
    baseline was snapshotted, the lesson is not promoted, the post window has too
    few gradeable runs, or the drop is inside the margin. Absence of a
    demonstrated regression is not a demonstrated regression.
    """
    lesson = read_lesson(ctx, lesson_id)
    if lesson is None or lesson.status != LessonStatus.PROMOTED:
        return False
    if lesson.baseline is None or not lesson.promoted_at:
        return False

    post, gradeable = scope_acceptance(ctx, lesson.scope, since=lesson.promoted_at)
    if post is None or gradeable < REGRESSION_MIN_RUNS:
        return False
    if post >= lesson.baseline - REGRESSION_MARGIN:
        return False

    return retire(
        ctx,
        lesson_id,
        reason=RETIRED_REGRESSION,
        detail=(
            f"scope {lesson.scope!r} acceptance bound fell from {lesson.baseline:.3f} at "
            f"promotion to {post:.3f} over {gradeable} gradeable runs since"
        ),
    )


def retire(ctx: LoopContext, lesson_id: str, *, reason: str, detail: str = "") -> bool:
    """Take a promoted lesson out of recall, and record why.

    The status change is a compare-and-set from ``promoted``, so a lesson cannot
    be retired twice and cannot be retired out from under a concurrent promotion.
    The retirement row is HISTORY, written ``put_once`` under an id derived from
    ``(lesson, reason)`` — a second retirement for the same reason is refused,
    which is correct, because it is the same event observed twice.

    ``retired`` is a decided status, so the key never resurrects: the next time
    the same failure recurs and the same cluster forms, :func:`stage` refuses it.
    That is deliberate. A machine that retires a lesson for regression and then
    re-learns it a week later has not learned anything.
    """
    lesson = read_lesson(ctx, lesson_id)
    if lesson is None or lesson.status != LessonStatus.PROMOTED:
        return False
    if not _transition(ctx, lesson, LessonStatus.RETIRED, {"retired_reason": reason}):
        return False

    record_id = f"ret_{digest_key('retirement', lesson_id, reason)[:20]}"
    write_history(
        ctx,
        RecordKind.RETIREMENT,
        record_id,
        {
            "id": record_id,
            "at": _now(ctx),
            "lesson_id": lesson_id,
            "scope": lesson.scope,
            "reason": reason,
            "detail": detail,
            "baseline": lesson.baseline,
            "used": lesson.used,
            "helped": lesson.helped,
            "instance_id": ctx.instance_id,
            "template": ctx.template,
        },
    )
    emit(
        ctx,
        LEARNING_EVENT_KIND,
        "lesson_retired",
        {"lesson": lesson_id, "scope": lesson.scope, "reason": reason, "detail": detail},
    )
    return True


# ---------------------------------------------------------------------------
# STAGE 10 — DECAY. Age out what nobody is using.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecayReport:
    """Which promoted lessons aged out of relevance, and which were kept."""

    retired: tuple[str, ...] = ()
    kept: int = 0
    #: Lessons kept because their age could not be established at all.
    undateable: tuple[str, ...] = ()


def decay(ctx: LoopContext) -> DecayReport:
    """Age promoted lessons that are not being used; retire below the floor.

    Age is measured from the most recent GRADED use — ``last_used_at``, which
    :func:`attribute` writes, else ``promoted_at``, else ``created_at`` — and
    weighed by the same linear :func:`~selfloop.stats.decay_weight` the ranking
    uses. With the default 7/14-day curve and a
    :attr:`~selfloop.context.LoopContext.retire_floor` of 0.2, a lesson that has
    gone ungraded for about twelve and a half days retires.

    Note what "used" means there, because it is stricter than it looks: a lesson
    injected into a fortnight of neutral ticks is never graded, so its
    ``last_used_at`` never moves and it ages out. That is deliberate. A lesson
    nobody can demonstrate is helping is precisely what decay is for, and keeping
    it on the strength of injections that produced nothing to judge would let the
    injected block grow forever on evidence of nothing.

    **A missing or unreadable timestamp returns ``None`` and the lesson is
    KEPT.** A malformed record must not be the first thing evicted: treating "I
    cannot read this row's stamp" as "this row is infinitely old" makes the
    corrupt rows the first casualties of a cleanup pass, which is precisely
    backwards — those are the rows an operator most needs to still be there when
    they go looking.
    """
    retired: list[str] = []
    undateable: list[str] = []
    kept = 0
    for row in ctx.records.query(RecordKind.LESSON.value, status=LessonStatus.PROMOTED.value):
        lesson = _lesson_from(row)
        if lesson is None:
            continue
        age = _age_days(ctx, lesson.last_used_at or lesson.promoted_at or lesson.created_at)
        if age is None:
            undateable.append(lesson.id)
            kept += 1
            continue
        if decay_weight(age) < ctx.retire_floor and retire(
            ctx,
            lesson.id,
            reason=RETIRED_DECAYED,
            detail=f"unused for {age:.1f} days; decay weight below retire_floor",
        ):
            retired.append(lesson.id)
            continue
        kept += 1
    return DecayReport(retired=tuple(retired), kept=kept, undateable=tuple(undateable))


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningPassReport:
    """One complete learning pass, as a value a caller can act on and record."""

    run_id: str
    cursor_before: int
    cursor_after: int
    signals: int = 0
    clusters: int = 0
    staged: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()
    parked: tuple[str, ...] = ()
    approval_ids: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    attributed: int = 0
    notes: tuple[str, ...] = ()

    @property
    def parks(self) -> bool:
        """True when a promotion is waiting on a human.

        The runtime reads this and renders the tick as
        ``RunReport(status=PARKED, approval_id=...)``. That is the whole resume
        protocol: the next ``run_once`` retries the promotion, finds the same
        deterministic approval row, and either promotes or parks again.
        """
        return bool(self.parked)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "signals": self.signals,
            "clusters": self.clusters,
            "staged": list(self.staged),
            "promoted": list(self.promoted),
            "parked": list(self.parked),
            "approval_ids": list(self.approval_ids),
            "retired": list(self.retired),
            "attributed": self.attributed,
            "notes": list(self.notes),
        }


def learning_pass(ctx: LoopContext, run_id: str) -> LearningPassReport:
    """Run stages 3 through 10 once. **The only entry point into this module.**

    ``runtime.run_once`` calls this after settlement, always, and nothing else
    does. An earlier design ran a pass here *and* mounted a learning graph node,
    which gave the pass two owners: double extraction, racing cursors, and a
    promotion parking outside the engine's park/resume protocol. One owner, one
    call site, no ``add_learn``.

    The order is attribute, extract, cluster, stage, promote, decay, and only
    then advance the cursor. Attribution runs first so that a lesson retired for
    regression this pass is not re-promoted by the same pass. **The cursor moves
    last, and only if everything before it completed** — an exception anywhere
    propagates with the cursor untouched, so the next tick re-mines the same
    window. Re-mining is idempotent (content-stable signal ids, ``put_once``);
    skipping is not, and evidence skipped is evidence nobody ever learns is
    missing.
    """
    cursor_before = read_cursor(ctx, DEFAULT_CURSOR_NAME)
    notes: list[str] = []

    attribution = attribute(ctx, run_id)
    if attribution.skipped:
        notes.append(attribution.skipped)

    harvest = extract(ctx, since_cursor=cursor_before, run_id=run_id)
    notes.extend(harvest.notes)

    clusters = cluster(harvest.signals)
    staged: list[str] = []
    for candidate in clusters:
        result = stage(ctx, candidate)
        if result.refused and not result.decided:
            notes.append(f"{candidate.key}: {result.refused}")
        elif result.created or result.appended:
            staged.append(result.lesson_id)

    promoted: list[str] = []
    parked: list[str] = []
    approvals_open: list[str] = []
    for lesson in pending_promotions(ctx):
        decision = promote(ctx, lesson.id)
        if decision.promoted:
            promoted.append(decision.lesson_id)
        elif decision.parked:
            parked.append(decision.lesson_id)
            if decision.approval_id:
                approvals_open.append(decision.approval_id)

    aged = decay(ctx)
    retired = (*attribution.retired, *aged.retired)
    for lesson_id in aged.undateable:
        notes.append(
            f"lesson {lesson_id} has no readable timestamp; kept rather than decayed, because "
            "a malformed record must not be the first thing evicted"
        )

    cursor_after = advance_cursor(ctx, DEFAULT_CURSOR_NAME, harvest.high_water)

    emit(
        ctx,
        LEARNING_EVENT_KIND,
        "learning_pass",
        {
            "signals": len(harvest.signals),
            "clusters": len(clusters),
            "staged": staged,
            "promoted": promoted,
            "parked": parked,
            "retired": list(retired),
            "cursor": cursor_after,
        },
        run_id=run_id,
    )
    return LearningPassReport(
        run_id=run_id,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        signals=len(harvest.signals),
        clusters=len(clusters),
        staged=tuple(staged),
        promoted=tuple(promoted),
        parked=tuple(parked),
        approval_ids=tuple(dict.fromkeys(approvals_open)),
        retired=retired,
        attributed=attribution.attributed,
        notes=tuple(notes),
    )


__all__ = [
    "CAS_ATTEMPTS",
    "DEFAULT_BASELINE_WINDOW",
    "DEFAULT_EVENT_SCAN_LIMIT",
    "DEFAULT_SIGNAL_SOURCES",
    "EFFECT_FAILED_ACTION",
    "LEARNING_EVENT_KIND",
    "LESSON_BLOCK_CLOSE",
    "LESSON_BLOCK_OPEN",
    "LESSON_BLOCK_PREAMBLE",
    "MAX_CLAIM_CHARS",
    "PROMOTION_NODE",
    "PROMOTION_TOOL",
    "REGRESSION_MARGIN",
    "REGRESSION_MIN_RUNS",
    "RETIRED_DECAYED",
    "RETIRED_REGRESSION",
    "SIMILARITY_THRESHOLD",
    "TAG_EFFECT_FAILED",
    "TAG_VERIFY_DISAGREEMENT",
    "Admission",
    "AttributionReport",
    "Cluster",
    "DecayReport",
    "Harvest",
    "LearningPassReport",
    "PromotionResult",
    "SignalSourceLike",
    "StageResult",
    "adverse_outcome_signals",
    "attribute",
    "candidate_id",
    "cluster",
    "cluster_key",
    "decay",
    "extract",
    "failed_effect_signals",
    "learning_pass",
    "lesson_block",
    "pending_promotions",
    "promote",
    "read_lesson",
    "recall",
    "record_use",
    "retire",
    "scope_acceptance",
    "stage",
    "verify_disagreement_signals",
]
