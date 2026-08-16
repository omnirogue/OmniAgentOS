"""THE LIVENESS TEST. Does the loop actually learn, or is it merely wired to?

Every one of the three learning loops in the system this package was extracted
from was correctly wired and one hundred per cent starved: 207 candidates
staged, zero promoted, ever, behind a promotion score that was structurally
always ``0.0``. Every function existed. Every call site was right. A wiring test
— "``promote`` is called", "the injection line is present", "the store has a
lesson row" — would have been green throughout, and would have been green for
the entire life of the deployment.

So this file asserts FLOW, not connectivity. The headline test runs the real
runtime, with production defaults, for the true minimum number of ticks, and
requires that something the loop learned changed something the loop did.

**The collusion this file refuses (FIX-15).** The tempting way to green a
liveness test is to score the tick on whether the lesson's text appears in the
prompt. It passes on the first run, it never flakes, and it proves precisely
nothing: the lesson raises the score by being present, the tick settles
favourable, the counters rise, and nothing in the world has changed. The same
hole opens whenever the component that labels the training data also grades
whether the lesson helped. So the favourable settle here is tied to a **world
artifact** — a file on disk, its size ruled on by a gate that never sees the
prompt, never sees the score, and reads nothing but bytes. Prompt containment is
asserted too, but only as evidence that *injection happened*; it is never the
thing that makes the tick pass.

The other nine tests each pin one correction that a reviewer forced, and each
one is a regression test for a specific, previously-shipped route to starvation:

* :func:`test_a_virgin_candidate_promotes_with_used_zero` — FIX-1. Promotion
  admission must not read post-injection counters.
* :func:`test_candidate_id_is_stable_as_evidence_accumulates` — FIX-2. A
  candidate id derived from its evidence never accumulates support.
* :func:`test_neutral_outcomes_never_become_evidence` — FIX-12.
* :func:`test_a_regressing_lesson_is_auto_retired` and
  :func:`test_a_park_does_not_increment_used` — FIX-11.
* :func:`test_clusters_do_not_merge_across_failure_tags` — FIX-14.

Everything here runs against the in-memory adapters: no files beyond a tmp
artifact, no network, no model, no API key.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import pytest
from selfloop import learn
from selfloop.adapters.memory import MemoryClock, build_memory_context
from selfloop.context import LoopContext
from selfloop.contracts import (
    APPROVAL_FLOOR_TIER,
    ApprovalState,
    LearningSignal,
    Lesson,
    LessonStatus,
    LoopStatus,
    LoopTool,
    RecordKind,
    RiskTier,
    RunReport,
    ToolRegistry,
)
from selfloop.gates import ArtifactGate, NullGate
from selfloop.guidance import NEEDS_HUMAN_MARKER
from selfloop.learn import (
    LESSON_BLOCK_OPEN,
    RETIRED_DECAYED,
    RETIRED_REGRESSION,
    attribute,
    candidate_id,
    cluster,
    decay,
    promote,
    read_lesson,
    recall,
    record_use,
    stage,
)
from selfloop.ledger import (
    LESSON_USE_ATTRIBUTED,
    LESSON_USE_PENDING,
    OutcomeRecord,
    read_cursor,
    write_history,
)
from selfloop.runtime import TAG_GATE_CONTRADICTED, run_once
from selfloop.stats import decay_weight, jaccard, normalise_tokens, wilson_lower_bound
from selfloop.templates.propose_evaluate_promote import (
    NAME as TEMPLATE_NAME,
)
from selfloop.templates.propose_evaluate_promote import (
    TRAJECTORY_KIND,
    default_propose,
    default_tools,
)

# ---------------------------------------------------------------------------
# The scenario the end-to-end tests run
# ---------------------------------------------------------------------------

#: The learning scope every test in this file works in.
SCOPE = "release-notes"

#: The proposer's brief. Read it carefully, because what is NOT in it is the
#: point: it contains no ``include:`` directive and none of :data:`REMEDY_TERMS`.
#: The only route by which those terms can reach the artifact is a lesson the
#: loop promoted from its own graded runs.
BRIEF = "Draft this week's release notes."

#: What the loop's OWN evaluator scores against. One term, and it is satisfied
#: on every tick — including the ticks that settle adverse. That is deliberate:
#: it means the evaluator cannot be the thing that flips the final tick to
#: favourable, so the flip has to have come from the gate, which reads bytes.
SPEC: Mapping[str, Any] = {"must_include": ["breaking-changes"]}

#: The remedy a human wrote down, in advance, for this class of failure. The
#: machine chooses WHICH pre-authored sentence applies; it never authors one.
REMEDY_TERMS = ("upgrade-steps", "rollback-plan", "checksums")
REMEDY = f"include: {', '.join(REMEDY_TERMS)}"

#: The publishing rule the independent gate enforces: notes shorter than this
#: are a stub, not release notes. Chosen so that the un-taught loop's artifact
#: falls short of it and the taught loop's artifact clears it — the gap between
#: those two numbers is the entire behaviour change this file exists to prove.
GATE_MIN_BYTES = 60

#: :class:`~selfloop.context.LoopContext`'s own declared defaults, read off the
#: dataclass rather than copied. The liveness test asserts that the values it ran
#: with are these, so a future edit that greens it by quietly lowering
#: ``min_support`` fails instead — which is precisely the manoeuvre FIX-4 was
#: written against.
CONTEXT_DEFAULTS: Mapping[str, Any] = {f.name: f.default for f in fields(LoopContext)}


class _RecordingProposer:
    """The shipped default proposer, plus a note of every prompt it was handed.

    A black-box observation: the brief recorded here is the string the tool
    actually received through the execution seam, not something read back out of
    a checkpoint. That matters because the assertion it supports — "the promoted
    lesson reached the next run's prompt" — is about what the loop DID, and a
    test that reconstructs the prompt from stored state is asserting about its
    own reconstruction.

    It is emphatically not the liveness assertion. See this module's docstring:
    prompt containment shows injection happened and nothing more.
    """

    def __init__(self) -> None:
        self.briefs: list[str] = []

    def __call__(
        self,
        *,
        brief: str,
        history: Sequence[Mapping[str, Any]],
        feedback: str,
        round_index: int,
    ) -> dict[str, Any]:
        self.briefs.append(str(brief))
        return default_propose(
            brief=brief, history=history, feedback=feedback, round_index=round_index
        )


@dataclass
class Loop:
    """One built loop instance, plus the handles a test needs to interrogate it."""

    ctx: LoopContext
    clock: MemoryClock
    notes: Path
    proposer: _RecordingProposer
    reports: list[RunReport] = field(default_factory=list)

    def tick(self) -> RunReport:
        """Run one real tick, then move the clock on as a scheduler would.

        The clock advance is not decoration. In production each tick is a
        separate process minutes or hours apart, and several rules in the
        learning loop — decay, the post-promotion window in attribution, the
        ordering of report cards — are functions of the record stamp. A test
        whose ticks all share one instant is a test that cannot see them.
        """
        report = run_once(self.ctx, TEMPLATE_NAME)
        self.reports.append(report)
        self.clock.advance(3600)
        return report

    @property
    def artifact(self) -> str:
        return self.notes.read_text(encoding="utf-8") if self.notes.exists() else ""

    @property
    def artifact_size(self) -> int:
        return self.notes.stat().st_size if self.notes.exists() else 0

    def outcome_for(self, run_id: str) -> OutcomeRecord:
        """The report card the runtime filed for *run_id*. Fails if there is none."""
        rows = self.ctx.records.query(RecordKind.OUTCOME.value, run_id=run_id)
        assert rows, f"run {run_id} filed no outcome record at all"
        newest = sorted(rows, key=lambda row: (str(row.get("at", "")), str(row.get("id", ""))))[-1]
        return OutcomeRecord.from_payload(newest)

    def best_score_in(self, run_id: str) -> float:
        """The best score the loop's OWN evaluator gave any round of *run_id*."""
        rows = self.ctx.records.query(TRAJECTORY_KIND, run_id=run_id)
        assert rows, f"run {run_id} recorded no evaluated rounds"
        return max(float(row.get("score", 0.0)) for row in rows)


def build_loop(tmp_path: Path, **overrides: Any) -> Loop:
    """Wire the quickstart's loop: one real effect, one independent gate, no model.

    Deliberately close to ``examples/quickstart.py``, because a liveness test
    that exercises a bespoke rig proves the rig works. The one addition is the
    recording proposer.

    **No learning knob is set here.** ``min_support``, ``promote_threshold`` and
    ``min_evidence_consistency`` are left at the context's own defaults, and
    :data:`CONTEXT_DEFAULTS` is asserted against them in the headline test, so a
    demo-only setting cannot creep in and green the cycle by lowering a bar.
    """
    notes = tmp_path / "release-notes.md"

    def publish(*, candidate: Mapping[str, Any], evaluation: Mapping[str, Any]) -> dict[str, Any]:
        """The one effect: it puts something in the world. Everything else reads."""
        del evaluation
        notes.write_text(str(candidate.get("text") or ""), encoding="utf-8")
        return {"path": str(notes), "bytes": notes.stat().st_size}

    def wrote_the_file(result: Any, args: Mapping[str, Any]) -> bool:
        """Independent evidence that ``publish`` did it, rather than its own word for it."""
        del args
        return Path(str((result or {}).get("path") or "")).is_file()

    proposer = _RecordingProposer()
    clock = MemoryClock()
    tools = ToolRegistry()
    _, evaluate_tool = default_tools()
    tools.register(
        LoopTool(
            name="propose",
            tier=RiskTier.T0,
            call=proposer,
            description="draft a candidate that obeys the include: directives it was given",
        )
    )
    tools.register(evaluate_tool)
    tools.register(
        LoopTool(name="promote", tier=RiskTier.T1, call=publish, verify=wrote_the_file)
    )

    defaults: dict[str, Any] = {
        "instance_id": "liveness",
        "template": TEMPLATE_NAME,
        "tools": tools,
        "clock": clock,
        # The independent verifier. It looks at a file and nothing else — not the
        # prompt, not the score, not the loop's own report of itself.
        "gate": ArtifactGate(clock=clock, artifacts=[notes], min_bytes=GATE_MIN_BYTES),
        "params": {"scope": SCOPE, "brief": BRIEF, "spec": SPEC},
        # T1: a reversible local rule, so a lesson for this scope may promote on
        # evidence alone. An undeclared scope takes the approval floor and parks.
        "scope_tiers": {SCOPE: RiskTier.T1},
        "remedy_table": {TAG_GATE_CONTRADICTED: REMEDY},
    }
    defaults.update(overrides)
    return Loop(
        ctx=build_memory_context(**defaults), clock=clock, notes=notes, proposer=proposer
    )


# ---------------------------------------------------------------------------
# Seeding the ledger directly, for the tests that exercise one stage in isolation
# ---------------------------------------------------------------------------


def build_bare_context(**overrides: Any) -> LoopContext:
    """A context with no tools and no graph, for testing the learning stages alone.

    The scope is declared T1 by default so that promotion takes the auto path;
    the tests that want the approval path say so explicitly, because an
    undeclared scope parking is itself a behaviour worth naming rather than
    inheriting.
    """
    defaults: dict[str, Any] = {
        "instance_id": "stages",
        "template": "stages",
        "scope_tiers": {SCOPE: RiskTier.T1},
        "remedy_table": {TAG_GATE_CONTRADICTED: REMEDY},
    }
    defaults.update(overrides)
    return build_memory_context(**defaults)


def seed_outcome(
    ctx: LoopContext,
    run_id: str,
    *,
    outcome_class: str,
    scope: str = SCOPE,
    failure_tag: str = TAG_GATE_CONTRADICTED,
    detail: str = "",
) -> OutcomeRecord:
    """Write the report card a settled run would have filed. **History, not a cache.**

    Written through ``put_once`` under a per-invocation id exactly as the runtime
    writes it, so the tests that read evidence back are reading the same shape
    production reads. ``gate_passed`` is derived from the class rather than
    passed in, because the two are not independent: an accepted run is one an
    executed gate corroborated, and a row that claimed otherwise could not have
    been produced by :func:`selfloop.outcome.compose`.
    """
    gate_passed: bool | None = {"favourable": True, "adverse": False, "neutral": None}[
        outcome_class
    ]
    record = OutcomeRecord(
        id=f"out_{run_id}#001",
        run_id=run_id,
        instance_id=ctx.instance_id,
        template=ctx.template,
        at=ctx.clock.now_iso(),
        self_reported_status=LoopStatus.COMPLETED.value,
        gate_passed=gate_passed,
        outcome_class=outcome_class,
        scope=scope,
        failure_tag=failure_tag if outcome_class == "adverse" else "",
        detail=detail,
        gate_unavailable_reason="no_gate" if gate_passed is None else "",
        checks_collected=0 if gate_passed is None else 1,
    )
    assert write_history(ctx, RecordKind.OUTCOME, record.id, record.as_dict())
    return record


def seed_signal(
    ctx: LoopContext,
    run_id: str,
    *,
    text: str,
    scope: str = SCOPE,
    failure_tag: str = TAG_GATE_CONTRADICTED,
    suffix: str = "",
) -> LearningSignal:
    """Persist one mined signal attributed to *run_id*.

    *suffix* distinguishes several signals mined from ONE run, which is the
    ordinary case — a bad tick usually leaves more than one trace — and is what
    :func:`test_support_counts_distinct_runs_not_signals` needs to tell the two
    kinds of counting apart.
    """
    signal = LearningSignal(
        id=f"sig_{scope}_{failure_tag}_{run_id}{suffix}",
        scope=scope,
        failure_tag=failure_tag,
        text=text,
        run_id=run_id,
        cursor=0,
    )
    assert write_history(ctx, RecordKind.SIGNAL, signal.id, signal.as_dict())
    return signal


def seed_evidence(
    ctx: LoopContext,
    runs: Iterable[str],
    *,
    outcome_class: str = "adverse",
    text: str = "the published notes are 34 bytes, below the publishing rule",
    scope: str = SCOPE,
    failure_tag: str = TAG_GATE_CONTRADICTED,
) -> list[LearningSignal]:
    """One settled run and one mined signal per id in *runs*, in that order."""
    signals: list[LearningSignal] = []
    for run_id in runs:
        seed_outcome(
            ctx, run_id, outcome_class=outcome_class, scope=scope, failure_tag=failure_tag
        )
        signals.append(seed_signal(ctx, run_id, text=text, scope=scope, failure_tag=failure_tag))
    return signals


def stage_from(ctx: LoopContext, signals: Sequence[LearningSignal]) -> learn.StageResult:
    """Cluster *signals* and stage the single candidate they form."""
    clusters = cluster(signals)
    assert len(clusters) == 1, f"expected one cluster, got {[c.key for c in clusters]}"
    return stage(ctx, clusters[0])


def lesson_rows(ctx: LoopContext) -> list[Mapping[str, Any]]:
    return ctx.records.query(RecordKind.LESSON.value)


def status_of(ctx: LoopContext, lesson_id: str) -> LessonStatus:
    """The stored lesson's status. Fails loudly if the row has gone missing.

    A helper rather than ``read_lesson(...).status`` at each call site, because
    the row vanishing and the row not having moved are different findings, and
    an attribute access on ``None`` reports neither of them legibly.
    """
    lesson = read_lesson(ctx, lesson_id)
    assert lesson is not None, f"lesson {lesson_id} is no longer in the store"
    return lesson.status


# ---------------------------------------------------------------------------
# 1. THE LIVENESS TEST
# ---------------------------------------------------------------------------


def test_a_signal_becomes_an_injected_lesson_and_changes_behaviour(tmp_path: Path) -> None:
    """The whole cycle, end to end, with production defaults and no demo knobs.

    The arithmetic is fixed by FIX-4 and is not negotiable: a candidate needs
    ``min_support`` DISTINCT RUNS of evidence before it may be admitted, and a
    promoted lesson can only change a run that starts AFTER it — so the minimum
    is ``min_support + 1`` ticks. The first ``min_support`` ticks each contribute
    one graded failure and the promotion happens in the learning pass at the end
    of the last of them; the final tick is the first run the lesson is in front
    of. This test runs exactly that many ticks and no more. Running a
    generous number and asserting "something got promoted eventually" would hide
    the difference between a loop that learns and a loop that is merely given
    enough attempts.

    **What makes the final tick's pass honest (FIX-15).** The loop's own
    evaluator is satisfied on every single tick — it scores 1.0 on the two that
    settle ADVERSE, and the test asserts that. So the evaluator cannot be what
    flips the last tick. What disagrees on the first two ticks is the gate, which
    never sees the prompt, never sees the score, and only measures the file on
    disk. The third tick's file is larger because it contains
    :data:`REMEDY_TERMS`, which appear in neither the brief nor the spec, and
    whose only route into the artifact is the lesson the loop promoted from its
    own graded failures.
    """
    loop = build_loop(tmp_path)
    ctx = loop.ctx

    # Production defaults, asserted against the context's own declared values, so
    # that the two ways of cheating this test fail differently and legibly.
    assert ctx.min_support == CONTEXT_DEFAULTS["min_support"], (
        "this loop is not running the shipped default. A liveness test that lowers "
        "min_support greens while production starves, which is exactly the manoeuvre "
        "FIX-4 was written against"
    )
    assert ctx.promote_threshold == CONTEXT_DEFAULTS["promote_threshold"]
    assert ctx.min_evidence_consistency == CONTEXT_DEFAULTS["min_evidence_consistency"]
    assert CONTEXT_DEFAULTS["min_support"] == 2, (
        "the shipped min_support moved. That is allowed, but it is a doctrine change and "
        "not a tuning one: 'minimum ticks to a first promotion is min_support + 1' is "
        "stated in the README, the quickstart and this file. Update them together."
    )

    # The counterfactual, stated up front rather than inferred afterwards: the
    # remedy terms are reachable from nowhere the loop is given at the start.
    given = f"{BRIEF} {json.dumps(SPEC)}"
    for term in REMEDY_TERMS:
        assert term not in given, f"{term!r} is in the loop's own inputs; the test proves nothing"

    # --- the evidence ticks -------------------------------------------------
    for index in range(ctx.min_support):
        report = loop.tick()
        assert report.outcome == "adverse", (
            f"tick {index + 1} settled {report.outcome}; the evidence ticks must be graded "
            f"failures or there is nothing to learn from ({report.detail})"
        )
        assert loop.artifact_size < GATE_MIN_BYTES
        assert loop.best_score_in(report.run_id) == 1.0, (
            "the loop's own evaluator must be SATISFIED on the failing ticks — otherwise "
            "the final tick could pass because the evaluator changed its mind, and this "
            "test would be grading the loop's homework with the loop's own marker"
        )
        assert not any(
            LESSON_BLOCK_OPEN.format(scope=SCOPE) in brief for brief in loop.proposer.briefs
        ), "a lesson was injected before anything had been promoted"

    # --- the promotion happened in the last evidence tick's pass ------------
    promoted = recall(ctx, SCOPE)
    assert len(promoted) == 1, (
        f"after {ctx.min_support} graded failures nothing was promoted: {lesson_rows(ctx)}"
    )
    lesson = promoted[0]
    assert lesson.status is LessonStatus.PROMOTED
    assert lesson.support == ctx.min_support
    assert lesson.guidance == f"when {TAG_GATE_CONTRADICTED} then {REMEDY}"
    assert lesson.used == 0 and lesson.helped == 0, (
        "a lesson is promoted before it has ever been used; if these are non-zero the "
        "admission rule has been fed post-injection counters"
    )

    size_before = loop.artifact_size
    prompts_before = len(loop.proposer.briefs)

    # --- the tick that uses it ----------------------------------------------
    final = loop.tick()

    # Injection happened. This is evidence of the feedback edge being live, and
    # it is NOT what makes the tick pass — see the assertions below it.
    injected = loop.proposer.briefs[prompts_before:]
    assert injected, "the final tick never reached the proposer"
    assert LESSON_BLOCK_OPEN.format(scope=SCOPE) in injected[0]
    assert lesson.guidance in injected[0]

    # THE LIVENESS ASSERTION: a world artifact the lesson caused.
    artifact = loop.artifact
    for term in REMEDY_TERMS:
        assert term in artifact, (
            f"{term!r} never reached the file. The lesson was injected and changed nothing, "
            "which is the failure this package exists to refuse"
        )
    assert size_before < GATE_MIN_BYTES <= loop.artifact_size

    # And the gate — which reads bytes and nothing else — agreed, for the first time.
    settled = loop.outcome_for(final.run_id)
    assert settled.gate_passed is True
    assert settled.outcome_class == "favourable"
    assert settled.accepted
    assert settled.checks_collected >= 1, (
        "the acceptance this whole test rests on was backed by zero collected checks — "
        "a vacuous gate, which is worse than no gate, because no gate settles every tick "
        "visibly uncorroborated and a vacuous one settles them invisibly accepted"
    )
    assert final.status is LoopStatus.COMPLETED
    assert final.outcome == "favourable"

    # The return edge closed too: the use was committed before the run and graded
    # after it, so the loop's ranking now knows something it did not know before.
    graded = read_lesson(ctx, lesson.id)
    assert graded is not None
    assert (graded.used, graded.helped) == (1, 1)
    uses = ctx.records.query(RecordKind.LESSON_USE.value, run_id=final.run_id)
    assert [use["state"] for use in uses] == [LESSON_USE_ATTRIBUTED]
    assert uses[0]["helped"] is True

    assert len(loop.reports) == ctx.min_support + 1, "the cycle took more than the minimum"


# ---------------------------------------------------------------------------
# 2. FIX-1 — the regression test for the starvation bug
# ---------------------------------------------------------------------------


def test_a_virgin_candidate_promotes_with_used_zero() -> None:
    """A lesson that has never been used MUST promote when support is met.

    This is the whole of FIX-1, made mechanical. The predecessor required
    ``wilson_lower_bound(helped, used) >= threshold`` to promote, but both
    counters are written by attribution, which only runs after a lesson has been
    promoted and injected. At first promotion ``used == 0``, Wilson returns
    ``0.0``, and any positive threshold makes the gate unsatisfiable — correctly
    wired and mathematically always closed, 207 staged and none ever promoted.

    The arithmetic that makes this test a trap for that exact mutation is
    asserted rather than assumed: the bound on this candidate's counters is
    ``0.0`` and the context's threshold is above it, so a ``promote()`` that
    reads either counter cannot return ``promoted``. Nothing here is a proxy for
    that claim; the counters are read back after the promotion and are still zero.
    """
    ctx = build_bare_context()
    signals = seed_evidence(ctx, ["run-a", "run-b"])
    staged = stage_from(ctx, signals)
    assert staged.created

    virgin = read_lesson(ctx, staged.lesson_id)
    assert virgin is not None
    assert (virgin.used, virgin.helped) == (0, 0)
    assert virgin.status is LessonStatus.STAGED

    # The trap, stated as arithmetic: any admission rule that consults these
    # counters is closed for this candidate, whatever else is true of it.
    assert wilson_lower_bound(virgin.helped, virgin.used) == 0.0
    assert ctx.promote_threshold > 0.0

    result = promote(ctx, staged.lesson_id)
    assert result.promoted, f"a virgin candidate was refused: {result.reason}"
    assert result.status is LessonStatus.PROMOTED
    assert result.support == ctx.min_support

    after = read_lesson(ctx, staged.lesson_id)
    assert after is not None
    assert after.status is LessonStatus.PROMOTED
    assert (after.used, after.helped) == (0, 0)
    # And it is recallable at once. A recall floor on the same bound would starve
    # the loop one stage further along than the promotion gate used to.
    assert [lesson.id for lesson in recall(ctx, SCOPE)] == [staged.lesson_id]


# ---------------------------------------------------------------------------
# 3. FIX-2 — the id must not move as evidence arrives
# ---------------------------------------------------------------------------


def test_candidate_id_is_stable_as_evidence_accumulates() -> None:
    """A second contributing run APPENDS to the candidate; it does not mint a new one.

    Starvation by a second, entirely independent route. The predecessor derived
    the candidate id from the content key *plus the evidence ids*, so every new
    run that contributed evidence produced a different id, the insert-once store
    created a fresh row, and no candidate ever reached a support of two. It would
    survive any test that stages a candidate once — which is why this one stages
    twice and counts the rows.
    """
    ctx = build_bare_context()

    first = seed_evidence(ctx, ["run-a"])
    initial = stage_from(ctx, first)
    assert initial.created
    assert initial.support == 1
    assert len(lesson_rows(ctx)) == 1

    # The second pass re-mines the first run's evidence alongside the new run's,
    # exactly as the shipped outcome source does, and clusters both together.
    second = [*first, *seed_evidence(ctx, ["run-b"])]
    appended = stage_from(ctx, second)

    assert appended.lesson_id == initial.lesson_id, "a new evidence run minted a new candidate"
    assert not appended.created
    assert appended.appended == (second[1].id,)
    assert appended.support == 2

    rows = lesson_rows(ctx)
    assert len(rows) == 1, f"evidence accumulation created {len(rows)} rows instead of one"
    stored = Lesson.from_payload(rows[0])
    assert stored.support == 2
    assert set(stored.evidence_ids) == {signal.id for signal in second}

    # The id derives from the content key ALONE. Two clusters over different
    # evidence sets share a key, so they share an id.
    keys = {candidate.key for candidate in cluster(second)}
    assert keys == {cluster(first)[0].key}
    assert candidate_id(next(iter(keys))) == initial.lesson_id


def test_support_counts_distinct_runs_not_signals() -> None:
    """Ten traces of one bad night are ONE run's worth of evidence.

    The other half of FIX-2's arithmetic, and the half a test that seeds one
    signal per run cannot see. A failing tick usually leaves several traces — an
    adverse outcome, a failed effect, a verifier that disagreed — and if each
    counted separately then a single flaky night would clear any support
    threshold on its own. Support is a count of DISTINCT RUNS everywhere in this
    package precisely so it cannot.

    The candidate below carries three signals and is refused; adding a fourth
    signal from a SECOND run — one more signal, but the first new run — admits
    it. The number of signals is not what moved.
    """
    ctx = build_bare_context()
    seed_outcome(ctx, "run-a", outcome_class="adverse")
    traces = (
        "the published notes are 34 bytes, below the rule",
        "the published notes are 34 bytes, under the rule",
        "the published notes were 34 bytes, below the rule",
    )
    crowded = [
        seed_signal(ctx, "run-a", text=text, suffix=f"-{index}")
        for index, text in enumerate(traces)
    ]
    candidate = cluster(crowded)[0]
    assert len(candidate.signals) == 3
    assert candidate.support == 1, "three traces of one run were counted as three runs"

    staged = stage(ctx, candidate)
    assert staged.support == 1
    refused = promote(ctx, staged.lesson_id)
    assert not refused.promoted
    assert refused.support == 1

    # One more signal, but the first from a second run — and that is what admits it.
    second = seed_evidence(ctx, ["run-b"])
    admitted = stage_from(ctx, [*crowded, *second])
    assert admitted.lesson_id == staged.lesson_id
    assert admitted.support == 2
    assert promote(ctx, staged.lesson_id).promoted


# ---------------------------------------------------------------------------
# 4. Cold start
# ---------------------------------------------------------------------------


def test_cold_start_from_an_empty_store_reaches_a_promotion(tmp_path: Path) -> None:
    """From a provably empty store, ``min_support`` graded ticks reach a promotion.

    Distinct from the headline test in what it pins: not that the promotion
    changes behaviour, but that reaching it needs no primed state — no seeded
    lesson, no hand-written signal, no cursor. The store is asserted empty first,
    because a fixture that quietly leaves a row behind would let this pass on
    somebody else's evidence.

    It also fixes WHEN the promotion happens. By the end of tick ``min_support``
    the lesson must already be promoted — before the tick that uses it — so the
    ``min_support + 1`` arithmetic is a statement about the injection, not a
    third tick the gate secretly needs.
    """
    loop = build_loop(tmp_path)
    ctx = loop.ctx

    for kind in (RecordKind.LESSON, RecordKind.SIGNAL, RecordKind.OUTCOME):
        assert ctx.records.query(kind.value) == [], f"the store already holds {kind.value} rows"
    assert read_cursor(ctx) == 0
    assert not loop.notes.exists()
    assert recall(ctx, SCOPE) == []

    for _ in range(ctx.min_support):
        assert loop.tick().outcome == "adverse"

    promoted = recall(ctx, SCOPE)
    assert len(promoted) == 1, (
        "a cold loop given exactly min_support graded failures promoted nothing; "
        f"staged rows: {lesson_rows(ctx)}"
    )
    assert promoted[0].status is LessonStatus.PROMOTED
    assert promoted[0].support == ctx.min_support
    # The cursor moved, so the next pass mines new evidence rather than re-reading
    # the whole log forever.
    assert read_cursor(ctx) > 0


# ---------------------------------------------------------------------------
# 5. The promotion gate IS the effect gate
# ---------------------------------------------------------------------------


def test_a_t2_scoped_lesson_parks_instead_of_promoting() -> None:
    """A lesson whose SCOPE is T2 goes through the approval machinery and parks.

    This is the design's central claim made literal: a learned lesson is promoted
    through exactly the machinery an outbound email is sent through. The same
    deterministic id, the same one-row-one-page rule, the same
    expiry-binds-over-approval reading — and until a human answers, the lesson is
    not recallable and cannot reach a prompt.

    The second half matters as much as the first. Parking is not a dead end: the
    resume protocol is "the next pass retries the promotion", so once a person
    decides, the very next call promotes without anything else having changed.
    A gate that parks and then cannot be released is indistinguishable from a
    gate that always refuses.
    """
    ctx = build_bare_context(scope_tiers={SCOPE: RiskTier.T2})
    assert ctx.scope_tier(SCOPE) >= APPROVAL_FLOOR_TIER

    staged = stage_from(ctx, seed_evidence(ctx, ["run-a", "run-b"]))
    parked = promote(ctx, staged.lesson_id)

    assert parked.parked and not parked.promoted
    assert parked.status is LessonStatus.PARKED
    assert parked.approval_id
    assert parked.support == ctx.min_support, "it parked for a human, not for want of evidence"

    row = ctx.approvals.get(parked.approval_id)
    assert row is not None
    assert row["state"] == ApprovalState.PENDING.value
    assert row["requested_by"] == ctx.actor
    assert recall(ctx, SCOPE) == [], "a parked lesson reached recall without a human"

    # One row, one page, ever — however many passes retry the promotion.
    again = promote(ctx, staged.lesson_id)
    assert again.parked
    assert again.approval_id == parked.approval_id
    assert len(ctx.notifier.pages) == 1, f"the same park paged twice: {ctx.notifier.pages}"

    # A human decides, and the next pass promotes. The loop's own actor could
    # never satisfy this: the approval read refuses an automation identity.
    assert ctx.approvals.decide(
        parked.approval_id,
        state=ApprovalState.APPROVED.value,
        by="ops@example.com",
        note="reviewed the wording",
        at=ctx.clock.now_iso(),
    )
    released = promote(ctx, staged.lesson_id)
    assert released.promoted, f"an approved promotion did not apply: {released.reason}"
    assert released.approval_id == parked.approval_id
    assert [lesson.id for lesson in recall(ctx, SCOPE)] == [staged.lesson_id]


def test_a_lesson_without_template_derived_guidance_is_forced_to_the_approval_floor() -> None:
    """No remedy in the table means no auto-promotion, whatever the scope's tier says.

    FIX-13's teeth. A lesson's guidance is what reaches the next prompt, so it
    must be a template fill from a sentence a human wrote in advance — never
    free-form prose, and never a description of the failure standing in for a
    remedy. A failure tag nobody has written a remedy for therefore yields
    guidance marked ``[needs-human]``, and such a lesson is staged at the
    approval floor even though this scope is declared T1 and would otherwise
    auto-promote. The tier is computed once, at staging, and stored on the row,
    so editing the text afterwards cannot lower it.
    """
    ctx = build_bare_context(remedy_table={})  # nobody wrote down what to do

    staged = stage_from(ctx, seed_evidence(ctx, ["run-a", "run-b"]))
    lesson = read_lesson(ctx, staged.lesson_id)
    assert lesson is not None
    assert lesson.guidance.startswith(NEEDS_HUMAN_MARKER)
    assert lesson.tier >= APPROVAL_FLOOR_TIER
    assert ctx.scope_tier(SCOPE) < APPROVAL_FLOOR_TIER, "the scope itself was never T2"

    result = promote(ctx, staged.lesson_id)
    assert result.parked and not result.promoted
    assert recall(ctx, SCOPE) == []


# ---------------------------------------------------------------------------
# 6. The time-of-check / time-of-use bind
# ---------------------------------------------------------------------------


class _DriftBetweenTheGatesTwoReads:
    """A ``RecordStore`` that edits the lesson row between admission and the write.

    The narrow window the fingerprint bind exists to close. :func:`promote`
    re-reads the row immediately before its compare-and-set precisely because
    accepting by id alone leaves a gap in which the content changes and
    unvalidated text applies under a validated id — the predecessor had exactly
    that window in its proposal pipeline, where an update-existing-if-pending
    path could rewrite a row between validation and apply.

    Testing that window needs the mutation to land *inside* one ``promote``
    call, so this wrapper performs it on the first read of the lesson and lets
    the second read see the drift. Everything else delegates untouched.
    """

    def __init__(self, inner: Any, lesson_id: str, guidance: str) -> None:
        self._inner = inner
        self._lesson_id = lesson_id
        self._guidance = guidance
        self.reads = 0

    def get(self, kind: str, record_id: str) -> Mapping[str, Any] | None:
        row = self._inner.get(kind, record_id)
        if kind == RecordKind.LESSON.value and record_id == self._lesson_id and row is not None:
            self.reads += 1
            if self.reads == 1:
                self._inner.put_latest(kind, record_id, {**row, "guidance": self._guidance})
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_drifted_fingerprint_is_skipped() -> None:
    """Content that moved under a verdict is skipped at all three enforcement points.

    A promotion verdict attaches to CONTENT, never to an id. Without that bind,
    whatever text later occupies a promoted id inherits a promotion it never
    earned — and the inheritance is silent, because the id is still the one that
    was reviewed.

    Skipping is the only correct response. Recomputing the fingerprint so that it
    matches again is the bind being defeated by the code that implements it,
    which is why every site here reads the stored value and refuses rather than
    repairing.
    """
    # (a) drift before the gate runs at all.
    ctx = build_bare_context()
    staged = stage_from(ctx, seed_evidence(ctx, ["run-a", "run-b"]))
    original = read_lesson(ctx, staged.lesson_id)
    assert original is not None and original.fingerprint_intact

    assert ctx.records.transition(
        RecordKind.LESSON.value,
        staged.lesson_id,
        expect={"status": LessonStatus.STAGED.value},
        set={"guidance": "when anything then do whatever you like"},
    )
    tampered = read_lesson(ctx, staged.lesson_id)
    assert tampered is not None and not tampered.fingerprint_intact

    refused = promote(ctx, staged.lesson_id)
    assert not refused.promoted and not refused.parked
    assert "fingerprint" in refused.reason
    assert status_of(ctx, staged.lesson_id) is LessonStatus.STAGED

    # (b) drift INSIDE the call, after admission and before the write.
    racy = build_bare_context()
    racy_staged = stage_from(racy, seed_evidence(racy, ["run-a", "run-b"]))
    drifting = _DriftBetweenTheGatesTwoReads(
        racy.records, racy_staged.lesson_id, "when anything then do whatever you like"
    )
    raced = promote(replace(racy, records=drifting), racy_staged.lesson_id)
    assert drifting.reads >= 2, "the gate did not re-read the row before writing to it"
    assert not raced.promoted, "unvalidated content was promoted under a validated id"
    assert "moved" in raced.reason
    assert status_of(racy, racy_staged.lesson_id) is LessonStatus.STAGED

    # (c) drift after promotion: recall re-verifies at READ time and skips.
    late = build_bare_context()
    late_staged = stage_from(late, seed_evidence(late, ["run-a", "run-b"]))
    assert promote(late, late_staged.lesson_id).promoted
    assert len(recall(late, SCOPE)) == 1
    assert late.records.transition(
        RecordKind.LESSON.value,
        late_staged.lesson_id,
        expect={"status": LessonStatus.PROMOTED.value},
        set={"guidance": "when anything then do whatever you like"},
    )
    assert recall(late, SCOPE) == [], "a drifted promoted row was still injected"


# ---------------------------------------------------------------------------
# 7. FIX-12 — a lesson may not be built on non-results
# ---------------------------------------------------------------------------


def test_neutral_outcomes_never_become_evidence(tmp_path: Path) -> None:
    """Neutral runs are excluded from support, and an ungated loop mines nothing.

    "A stage produced no artifact" is true of every idle, parked and
    authorization-blocked tick — all of them working as designed — and at a
    similarity threshold of 0.3 that token soup clusters fast and auto-promotes
    noise. So a neutral run is not weak evidence to be discounted; it leaves the
    sample entirely, exactly as it leaves the acceptance floor.

    The second half is the honest consequence, stated in ``gates.NullGate``'s own
    docstring and pinned here: a loop with no independent verifier settles every
    tick neutral, writes no non-neutral evidence, and therefore CANNOT learn. It
    can run forever and promote nothing. That is not a defect in the gate — it is
    what having no verifier costs, made visible rather than hidden behind a
    passing tick.
    """
    ctx = build_bare_context()
    signals = seed_evidence(ctx, ["run-a", "run-b"], outcome_class="neutral")
    staged = stage_from(ctx, signals)

    assert staged.support == 0, "runs that settled neutral were counted toward support"
    starved = promote(ctx, staged.lesson_id)
    assert not starved.promoted and not starved.parked
    assert starved.support == 0
    assert "neutral" in starved.reason
    assert recall(ctx, SCOPE) == []

    # One of the two runs is re-settled adverse; the candidate is still one run
    # short, which shows the exclusion is per-run and not a blanket refusal.
    mixed = build_bare_context()
    mixed_signals = [
        *seed_evidence(mixed, ["run-a"], outcome_class="neutral"),
        *seed_evidence(mixed, ["run-b"], outcome_class="adverse"),
    ]
    partial = stage_from(mixed, mixed_signals)
    assert partial.support == 1
    assert not promote(mixed, partial.lesson_id).promoted

    # End to end: no verifier, no evidence, no lesson — ever.
    ungated = build_loop(tmp_path, gate=NullGate())
    for _ in range(ungated.ctx.min_support + 1):
        report = ungated.tick()
        settled = ungated.outcome_for(report.run_id)
        # Note which of the two disagrees with which. The tick keeps its own
        # COMPLETED claim, because a tick nobody checked has not been ruled
        # against — absence of evidence is not evidence of failure. The LEDGER is
        # where the honesty lives: gate_passed NULL, outcome_class neutral, not
        # accepted. That split is the entire mechanism by which a loop's own
        # optimism is stopped from becoming its training signal.
        assert report.status is LoopStatus.COMPLETED
        assert settled.gate_passed is None
        assert settled.outcome_class == "neutral"
        assert not settled.accepted
        assert "uncorroborated" in report.detail
    assert ungated.ctx.records.query(RecordKind.SIGNAL.value) == []
    assert lesson_rows(ungated.ctx) == []
    assert recall(ungated.ctx, SCOPE) == []


# ---------------------------------------------------------------------------
# 8. FIX-11 — attribution grades results, and only results
# ---------------------------------------------------------------------------


def _promoted_lesson(
    ctx: LoopContext,
    runs: Sequence[str] = ("run-a", "run-b"),
    *,
    failure_tag: str = TAG_GATE_CONTRADICTED,
) -> Lesson:
    """Stage and auto-promote one lesson from *runs*' adverse evidence.

    *failure_tag* is a parameter because a candidate's content key is derived
    from ``(scope, failure_tag)`` alone — that is what makes the key stable as
    evidence accumulates — so two lessons in one scope are two different KINDS of
    failure, not two batches of the same one. A caller that wants a second lesson
    has to name a second failure, and has to have written a remedy for it.
    """
    staged = stage_from(ctx, seed_evidence(ctx, runs, failure_tag=failure_tag))
    assert promote(ctx, staged.lesson_id).promoted
    lesson = read_lesson(ctx, staged.lesson_id)
    assert lesson is not None
    return lesson


def test_a_regressing_lesson_is_auto_retired() -> None:
    """A lesson whose scope measurably got worse after it shipped is removed by the machine.

    A real per-lesson pre/post comparison against a baseline snapshotted at
    promotion, replacing the predecessor's global "if five runs failed anywhere
    in the system, roll everything back" — a rule that attributed any five
    subsequent failures to every monitored proposal and had never once fired.

    Two guards keep it survivable, and both are exercised by the numbers here:
    the drop must exceed a margin, and the post window must hold at least
    ``REGRESSION_MIN_RUNS`` gradeable runs. The comparison is scope-level and
    therefore confounded when several lessons ship together; that limitation is
    documented at :func:`selfloop.learn.attribute` rather than papered over, and
    holdout runs are the correct fix v1 does not ship.
    """
    ctx = build_bare_context()
    # A healthy track record for the scope, so the baseline is a real number.
    for index in range(8):
        seed_outcome(ctx, f"good-{index}", outcome_class="favourable")
    lesson = _promoted_lesson(ctx)
    assert lesson.baseline is not None and lesson.baseline > 0.4
    assert lesson.promoted_at

    # Time passes, and everything in the scope goes wrong after the lesson shipped.
    ctx.clock.advance(3600)
    post_runs = [f"post-{index}" for index in range(learn.REGRESSION_MIN_RUNS)]
    for run_id in post_runs:
        seed_outcome(ctx, run_id, outcome_class="adverse")

    assert record_use(ctx, lesson, post_runs[-1])
    report = attribute(ctx, post_runs[-1])

    assert report.outcome_class == "adverse"
    assert report.graded == (lesson.id,)
    assert report.retired == (lesson.id,), "a measured regression did not retire the lesson"

    retired = read_lesson(ctx, lesson.id)
    assert retired is not None
    assert retired.status is LessonStatus.RETIRED
    assert (retired.used, retired.helped) == (1, 0)
    assert recall(ctx, SCOPE) == []

    rows = ctx.records.query(RecordKind.RETIREMENT.value, lesson_id=lesson.id)
    assert [row["reason"] for row in rows] == [RETIRED_REGRESSION]
    assert str(rows[0]["detail"]), "a retirement with no explanation is not an audit trail"

    # A decided key never resurrects: the same failure recurring does not restage it.
    again = stage_from(ctx, seed_evidence(ctx, ["run-c"]))
    assert again.decided and not again.stored
    assert status_of(ctx, lesson.id) is LessonStatus.RETIRED


def test_a_park_does_not_increment_used() -> None:
    """Parks, and runs with no report card at all, leave the counters alone.

    The failure this prevents is quiet and expensive: a flaky weekend of parks,
    aborts and unknown effect states fills ``used`` without ever filling
    ``helped``, the Wilson bound collapses, and the loop auto-retires the lessons
    that were working on evidence that was never about them.

    So the row stays PENDING — permanently. An outcome is history and a neutral
    run's report card never becomes non-neutral, so a use that is never graded is
    the correct, honest record of "this lesson was injected into a tick that
    produced nothing to judge". Rewriting it to ``attributed, helped=False``
    would be exactly the poisoning the rule forbids.
    """
    ctx = build_bare_context()
    lesson = _promoted_lesson(ctx)

    # (a) the tick parked: neutral, so there is nothing to attribute.
    seed_outcome(ctx, "run-parked", outcome_class="neutral")
    assert record_use(ctx, lesson, "run-parked")
    parked = attribute(ctx, "run-parked")

    assert parked.outcome_class == "neutral"
    assert parked.graded == ()
    assert "neutral" in parked.skipped
    still_virgin = read_lesson(ctx, lesson.id)
    assert still_virgin is not None
    assert (still_virgin.used, still_virgin.helped) == (0, 0)
    assert still_virgin.status is LessonStatus.PROMOTED
    use_rows = ctx.records.query(RecordKind.LESSON_USE.value, run_id="run-parked")
    assert [row["state"] for row in use_rows] == [LESSON_USE_PENDING]
    assert use_rows[0]["helped"] is None, "pending must be None, never False"

    # (b) the process died before settling: no report card, so nothing is graded.
    assert record_use(ctx, lesson, "run-vanished")
    vanished = attribute(ctx, "run-vanished")
    assert vanished.outcome_class is None
    assert vanished.graded == ()
    assert "no outcome record" in vanished.skipped
    ungraded = read_lesson(ctx, lesson.id)
    assert ungraded is not None and (ungraded.used, ungraded.helped) == (0, 0)

    # (c) a real result does move them, so the counters are not simply frozen.
    seed_outcome(ctx, "run-graded", outcome_class="favourable")
    assert record_use(ctx, lesson, "run-graded")
    graded = attribute(ctx, "run-graded")
    assert graded.graded == (lesson.id,)
    scored = read_lesson(ctx, lesson.id)
    assert scored is not None
    assert (scored.used, scored.helped) == (1, 1)


# ---------------------------------------------------------------------------
# 9. Decay
# ---------------------------------------------------------------------------


def test_an_unused_lesson_decays_below_the_floor_and_retires() -> None:
    """A lesson nobody can show is helping ages out; an unreadable one is KEPT.

    Decay is what stops the injected block growing forever on the strength of
    injections that produced nothing to judge. Age is measured from the most
    recent GRADED use, so a lesson injected into a fortnight of neutral ticks
    never moves its clock and retires — deliberately, because a lesson nobody can
    demonstrate is helping is exactly what decay is for.

    The other half is the rule the predecessor's salience formula got backwards.
    **A missing or unreadable timestamp keeps the lesson**: treating "I cannot
    read this row's stamp" as "this row is infinitely old" makes corrupt rows the
    first casualties of a cleanup pass, and those are the rows an operator most
    needs to still be there when they go looking.
    """
    #: A second kind of failure in the same scope, with its own pre-authored
    #: remedy, so that this test has two distinct promoted lessons to tell apart.
    other_tag = "upstream_timeout"
    ctx = build_bare_context(
        remedy_table={
            TAG_GATE_CONTRADICTED: REMEDY,
            other_tag: "raise the deadline and retry once before giving up",
        }
    )
    aged = _promoted_lesson(ctx, runs=("stale-a", "stale-b"))

    # A second, undateable lesson in the same scope, its stamps stripped the way
    # a half-written record or a botched migration would leave them.
    undateable = _promoted_lesson(ctx, runs=("odd-a", "odd-b"), failure_tag=other_tag)
    assert undateable.id != aged.id
    assert ctx.records.transition(
        RecordKind.LESSON.value,
        undateable.id,
        expect={"status": LessonStatus.PROMOTED.value},
        set={"promoted_at": "", "created_at": "not a timestamp", "last_used_at": None},
    )

    # Nothing has aged yet, so nothing goes.
    fresh = decay(ctx)
    assert fresh.retired == ()
    assert fresh.undateable == (undateable.id,)
    assert fresh.kept == 2

    # Past the floor: with the default 7/14-day curve and retire_floor 0.2, a
    # lesson ungraded for about twelve and a half days is below the bar.
    days = 13
    assert decay_weight(days) < ctx.retire_floor
    ctx.clock.advance(days * 86400)

    report = decay(ctx)
    assert report.retired == (aged.id,)
    assert report.undateable == (undateable.id,), "a malformed row was evicted first"
    assert report.kept == 1

    gone = read_lesson(ctx, aged.id)
    assert gone is not None and gone.status is LessonStatus.RETIRED
    kept = read_lesson(ctx, undateable.id)
    assert kept is not None and kept.status is LessonStatus.PROMOTED

    rows = ctx.records.query(RecordKind.RETIREMENT.value, lesson_id=aged.id)
    assert [row["reason"] for row in rows] == [RETIRED_DECAYED]
    assert [lesson.id for lesson in recall(ctx, SCOPE)] == [undateable.id]


# ---------------------------------------------------------------------------
# 10. FIX-14 — partition first, compare tokens second
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_a", "tag_a", "scope_b", "tag_b"),
    [
        pytest.param(SCOPE, "timeout", SCOPE, "auth_revoked", id="same-scope-different-tag"),
        pytest.param(SCOPE, "timeout", "other-scope", "timeout", id="same-tag-different-scope"),
    ],
)
def test_clusters_do_not_merge_across_failure_tags(
    scope_a: str, tag_a: str, scope_b: str, tag_b: str
) -> None:
    """Identical words are not enough. Membership is decided by the structured tag.

    Raw-token Jaccard conflates unrelated failures, because ``error``, ``failed``,
    ``in`` and ``line`` appear in all of them, and at a threshold of 0.3 they
    form one enormous trash cluster whose "lesson" is an amalgamation of
    contradictory fixes. Partitioning by ``(scope, failure_tag)`` before a single
    token is compared is what stops that.

    The two signals here have byte-identical text, so their similarity is exactly
    1.0 — the strongest possible case for merging them — and they still do not
    merge. Inside a partition the tokens keep their real job, which is choosing
    which phrasing a human reads on the approvals page.
    """
    shared = "the request failed at line 42 with an error from the upstream service"
    left = LearningSignal(
        id="sig-left", scope=scope_a, failure_tag=tag_a, text=shared, run_id="run-a", cursor=1
    )
    right = LearningSignal(
        id="sig-right", scope=scope_b, failure_tag=tag_b, text=shared, run_id="run-b", cursor=2
    )
    assert jaccard(normalise_tokens(left.text), normalise_tokens(right.text)) == 1.0

    clusters = cluster([left, right])
    assert len(clusters) == 2, "two unrelated failures were merged on their wording"
    assert len({candidate.key for candidate in clusters}) == 2
    assert len({candidate.lesson_id for candidate in clusters}) == 2
    for candidate in clusters:
        tags = {signal.failure_tag for signal in candidate.signals}
        scopes = {signal.scope for signal in candidate.signals}
        assert len(tags) == 1 and len(scopes) == 1
        assert candidate.evidence_consistency == 1.0


def test_similar_signals_inside_one_partition_do_cluster() -> None:
    """The complement: partitioning must not have disabled clustering altogether.

    A guard that refuses everything passes every "did it refuse?" test ever
    written, so the partition rule is only meaningful alongside this one. Two
    reports of the same failure, in the same scope, under the same tag, are one
    candidate with a support of two — and the claim it carries is a real sentence
    drawn from the evidence, never the empty string.
    """
    ctx = build_bare_context()
    signals = [
        seed_signal(ctx, "run-a", text="the published notes are 34 bytes, below the rule"),
        seed_signal(ctx, "run-b", text="the published notes are 41 bytes, below the rule"),
    ]
    seed_outcome(ctx, "run-a", outcome_class="adverse")
    seed_outcome(ctx, "run-b", outcome_class="adverse")

    clusters = cluster(signals)
    assert len(clusters) == 1
    candidate = clusters[0]
    assert candidate.support == 2
    assert candidate.run_ids == ("run-a", "run-b")
    assert candidate.claim, "a cluster was emitted with an empty claim"
    assert candidate.claim in {signal.text for signal in signals}

    staged = stage(ctx, candidate)
    assert staged.created and staged.support == 2
    assert promote(ctx, staged.lesson_id).promoted


def test_a_partition_with_no_text_at_all_is_rejected() -> None:
    """Two empty token sets are identical, so an all-empty batch would cluster confidently.

    ``jaccard(frozenset(), frozenset())`` is 1.0 — identical emptiness is
    identical, and that is the mathematically correct answer. It places the
    obligation on the clusterer, and this is that obligation: a partition whose
    signals carry no text is REJECTED, never emitted with ``claim=""``. Without
    it a batch of textless signals forms one supremely confident cluster around
    nothing, and a lesson with an empty claim is an empty line injected into
    every future prompt in its scope.
    """
    assert jaccard(normalise_tokens(""), normalise_tokens("")) == 1.0
    blank = [
        LearningSignal(
            id="sig-a", scope=SCOPE, failure_tag="timeout", text="", run_id="a", cursor=1
        ),
        LearningSignal(
            id="sig-b", scope=SCOPE, failure_tag="timeout", text="   ", run_id="b", cursor=2
        ),
    ]
    assert cluster(blank) == []


#: Unusual in a test module, and deliberate: the counterfeit corpus addresses the
#: assertions in this file by node id, so the set of things this file claims to
#: prove is part of its interface. A test deleted rather than fixed should show up
#: as a change to this list, next to the mutation that no longer has a witness.
__all__ = [
    "Loop",
    "build_bare_context",
    "build_loop",
    "seed_evidence",
    "seed_outcome",
    "seed_signal",
    "stage_from",
    "test_a_drifted_fingerprint_is_skipped",
    "test_a_lesson_without_template_derived_guidance_is_forced_to_the_approval_floor",
    "test_a_park_does_not_increment_used",
    "test_a_partition_with_no_text_at_all_is_rejected",
    "test_a_regressing_lesson_is_auto_retired",
    "test_a_signal_becomes_an_injected_lesson_and_changes_behaviour",
    "test_a_t2_scoped_lesson_parks_instead_of_promoting",
    "test_a_virgin_candidate_promotes_with_used_zero",
    "test_an_unused_lesson_decays_below_the_floor_and_retires",
    "test_candidate_id_is_stable_as_evidence_accumulates",
    "test_clusters_do_not_merge_across_failure_tags",
    "test_cold_start_from_an_empty_store_reaches_a_promotion",
    "test_neutral_outcomes_never_become_evidence",
    "test_similar_signals_inside_one_partition_do_cluster",
    "test_support_counts_distinct_runs_not_signals",
]
