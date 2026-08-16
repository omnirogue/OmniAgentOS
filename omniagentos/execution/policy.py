"""The deterministic execution policy (T4.4): signals in, ExecutionEnvelope out.

``decide_execution`` is the ONE place that answers "which tier, which effort, how
many tool turns" for a unit of work. It is pure — no clock, no I/O, no config
lookup, no global state — so the same signals always produce the same envelope,
and the ``reasons`` list it returns is a real audit trail rather than a
reconstruction.

**Shape of the decision.** Start at the cheapest possible point,
``(cheap, minimal)``, then :func:`~omniagentos.policy.execution.join` (per-axis
maximum) with each floor in turn:

  1. the safety floor for the action class and the task risk (T4.3),
  2. the breadth floor implied by how much the job declared it will touch,
  3. any explicit pin the caller supplied.

Because every step is a join, the fold can only ratchet UP, and its order cannot
change the answer — only the order of the recorded reasons. There is exactly ONE
downgrade in the entire policy, applied after all of the joins, and it fires only
when five independent conditions all hold (see :data:`DOWNGRADE_REASON`).

**What it deliberately does not do.** It does not read the brief text. There is no
regex over ``auth|security|migration|payment|...`` here, and there must never be
one: this codebase already has three deterministic classifiers that answer that
question properly — ``policy/shell.py:classify_shell``,
``sessions/policy_map.py:classify_tool`` and ``runner/core.py``'s
``_effective_action_class`` — and an explicit invariant that they share ONE
implementation so the gates can never disagree. A fourth classifier here, working
off different evidence, would be able to disagree with all three. The
``action_class`` signal *is* their answer; this module consumes it.

**What it deliberately leaves alone.** ``sandbox_level``, ``extra_dirs`` and
``scope_enforcement`` on the returned envelope keep their contract defaults.
Those are provisioning and rollout decisions owned elsewhere; a caller layers them
on afterwards. Tier, effort, turns and reasons are this module's business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from omniagentos.contracts import (
    ActionClass,
    DeclaredScope,
    ExecutionEnvelope,
    ModelTier,
    ReasoningEffort,
)
from omniagentos.execution.vocab import normalize_effort, normalize_tier
from omniagentos.policy.execution import (
    IDENTITY_FLOOR,
    TASK_RISK_FLOOR,
    TierEffort,
    action_class_rank,
    canonical_task_risk,
    floor_for,
    join,
)

__all__ = [
    "BREADTH_FLOOR",
    "DOWNGRADE_FLOOR",
    "DOWNGRADE_REASON",
    "POLICY_VERSION",
    "TURN_BUDGET_BASE",
    "TURN_BUDGET_LARGE",
    "TURN_BUDGET_WIDE",
    "BreadthReading",
    "ExecutionSignals",
    "ScopeBreadth",
    "decide_execution",
    "is_broad_create_root",
    "measure_breadth",
]


POLICY_VERSION = "execution-policy/1"


class ScopeBreadth(StrEnum):
    """How much surface a job declared. Ordering is significant (narrow -> wide)."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    WIDE = "wide"


# Breadth is scored, not counted, because the three kinds of declaration are not
# equally informative. A named file is a promise. A create ROOT is an open-ended
# licence to add files under a directory, so it counts double. A BROAD create root
# — a repo-top-level directory, or a bare name with no separator at all — is a
# licence over a whole area of the tree, so it counts double again on top.
_WEIGHT_CREATE_ROOT = 2
_WEIGHT_BROAD_CREATE_ROOT = 3

_BREADTH_THRESHOLDS: tuple[tuple[int, ScopeBreadth], ...] = (
    (1, ScopeBreadth.SMALL),
    (4, ScopeBreadth.MEDIUM),
    (8, ScopeBreadth.LARGE),
)

# Breadth raises the floor only once the job is genuinely large. A wide job gets
# HIGH effort but stays at the STANDARD tier: breadth is a statement about how
# much work there is, not about how dangerous it is — danger is the action class's
# job, and it is joined in separately. Sending every wide-but-trivial refactor to
# the strong tier would make breadth a second, weaker risk signal.
BREADTH_FLOOR: dict[ScopeBreadth, TierEffort] = {
    ScopeBreadth.LARGE: (ModelTier.STANDARD, ReasoningEffort.MEDIUM),
    ScopeBreadth.WIDE: (ModelTier.STANDARD, ReasoningEffort.HIGH),
}

# The ONE downgrade. Not "cheap when it looks easy" — cheap when the job is small,
# structurally safe, self-verifying, and nobody asked for anything stronger.
DOWNGRADE_FLOOR: TierEffort = (ModelTier.CHEAP, ReasoningEffort.LOW)
DOWNGRADE_REASON = "downgrade:small+low-risk+verified+confident-scope"

# Turn budgets. Advisory mirror of BudgetSpec.max_turns; a bigger declared scope
# needs more tool calls to touch every file it promised, and a job that runs out
# of turns mid-edit leaves a worse mess than one that never started.
TURN_BUDGET_BASE = 12
TURN_BUDGET_LARGE = 26
TURN_BUDGET_WIDE = 36
_TURNS_LARGE_FILES = 6
_TURNS_WIDE_FILES = 10


@dataclass(frozen=True, slots=True)
class ExecutionSignals:
    """Everything the policy is allowed to look at.

    A frozen dataclass rather than a pydantic model on purpose: pydantic would
    coerce-or-raise on ``explicit_tier``/``explicit_effort``, and this policy must
    neither raise on a typo nor treat one as authoritative. Parsing happens
    explicitly inside :func:`decide_execution`, where an unreadable pin can be
    recorded in ``reasons`` instead of vanishing.

    ``speed_hint`` and ``lane`` are accepted and carried but cannot change the
    decision, and that is structural rather than an oversight: the policy is
    monotonic, so a request to go faster has nothing it is *allowed* to lower. A
    lane picks among models AT the decided tier; it does not get to pick the tier.
    """

    action_class: ActionClass | str
    task_risk: str | None = None
    declared: DeclaredScope | None = None
    explicit_tier: ModelTier | str | None = None
    explicit_effort: ReasoningEffort | str | None = None
    speed_hint: bool = False
    lane: str | None = None


@dataclass(frozen=True, slots=True)
class BreadthReading:
    """A breadth measurement and the counts that produced it (for the reasons)."""

    bucket: ScopeBreadth
    score: int
    file_count: int
    create_root_count: int
    broad_root_count: int

    def counts(self) -> str:
        return (
            f"files={self.file_count}, create_roots={self.create_root_count}, "
            f"broad_roots={self.broad_root_count}, score={self.score}"
        )


def _norm_path(raw: str) -> str:
    """Light, tolerant normalization for counting only.

    Deliberately NOT ``scope.paths.normalize_rel``: that one raises on absolute,
    ``~`` and ``..``-escaping paths, which is correct when it guards a write and
    wrong here, where an odd declaration must still produce a number. This only
    folds the spellings that would double-count one file (``"a.py"`` vs
    ``"./a.py"`` vs ``"a.py/"``).
    """
    value = str(raw).strip()
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def is_broad_create_root(root: str) -> bool:
    """True when ``root`` licenses creation across a whole area of the tree.

    Broad means a repo-top-level directory, or a value with no path separator at
    all. The repo root itself (``""``, ``"."``, ``"/"``) is the broadest of all.
    """
    value = _norm_path(root)
    if value in {"", ".", "/"}:
        return True
    return "/" not in value.strip("/")


def measure_breadth(declared: DeclaredScope | None) -> BreadthReading:
    """Score a declared scope into a :class:`ScopeBreadth` bucket.

    ``declared is None`` means "nothing was declared", which carries no breadth
    information and therefore scores 0 (SMALL). It does NOT unlock the downgrade —
    that requires a declaration to exist and to be verified and confident.
    """
    if declared is None:
        return BreadthReading(ScopeBreadth.SMALL, 0, 0, 0, 0)

    files: set[str] = set()
    for group in (declared.files_to_modify, declared.files_to_create, declared.files_to_delete):
        for entry in group:
            normalized = _norm_path(entry)
            if normalized:
                files.add(normalized)

    # Roots are deduplicated but never dropped: an EMPTY root is not noise, it is
    # a licence over the repo root, and is_broad_create_root treats it as such.
    roots: list[str] = []
    seen_roots: set[str] = set()
    for entry in declared.create_roots:
        normalized = _norm_path(entry)
        if normalized in seen_roots:
            continue
        seen_roots.add(normalized)
        roots.append(normalized)
    broad_roots = [root for root in roots if is_broad_create_root(root)]

    score = (
        len(files) + _WEIGHT_CREATE_ROOT * len(roots) + _WEIGHT_BROAD_CREATE_ROOT * len(broad_roots)
    )

    bucket = ScopeBreadth.WIDE
    for threshold, candidate in _BREADTH_THRESHOLDS:
        if score <= threshold:
            bucket = candidate
            break

    return BreadthReading(bucket, score, len(files), len(roots), len(broad_roots))


def _turn_budget(reading: BreadthReading) -> int:
    """Advisory tool-turn budget for a job of this breadth.

    Keyed on declared FILE COUNT, not on the breadth score: turns are consumed by
    touching files, and a create root is a licence rather than a workload. Note
    that ``file_count >= _TURNS_WIDE_FILES`` is already implied by the WIDE bucket
    (score >= file_count), so it is belt-and-braces — kept because the rule is
    stated in terms of files and should stay readable as such if the bucket
    thresholds ever move.
    """
    if reading.bucket is ScopeBreadth.WIDE or reading.file_count >= _TURNS_WIDE_FILES:
        return TURN_BUDGET_WIDE
    if reading.file_count >= _TURNS_LARGE_FILES:
        return TURN_BUDGET_LARGE
    return TURN_BUDGET_BASE


def _render(point: TierEffort) -> str:
    return f"{point[0].value}/{point[1].value}"


@dataclass
class _Fold:
    """The running decision plus its reasons. Only :meth:`raise_to` may move it."""

    point: TierEffort = IDENTITY_FLOOR
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, floor: TierEffort, label: str) -> bool:
        """Join with ``floor``; record exactly one reason IF it actually moved."""
        joined = join(self.point, floor)
        if joined == self.point:
            return False
        self.point = joined
        self.reasons.append(f"{label} -> {_render(joined)}")
        return True

    def note(self, text: str) -> None:
        """Record something that did NOT move the decision but explains itself.

        An ignored pin is the case that matters: an operator who asked for a tier
        and did not get it is owed the sentence saying why.
        """
        self.reasons.append(text)


def _floor_label(action_class: ActionClass | str, task_risk: str | None) -> str:
    """The reason text for step 1, naming whichever inputs actually spoke."""
    class_text = action_class.value if isinstance(action_class, ActionClass) else str(action_class)
    label = f"floor:action_class={class_text}"
    canonical = canonical_task_risk(task_risk)
    if canonical is None:
        # Absent — the schema default nobody writes. Naming it would be noise.
        return label
    if canonical not in TASK_RISK_FLOOR:
        # Fail-closed escalation must be visible in the audit trail, not inferred
        # from a surprisingly high tier.
        return f"{label},task_risk={canonical}(unrecognized)"
    return f"{label},task_risk={canonical}"


def decide_execution(signals: ExecutionSignals) -> ExecutionEnvelope:
    """Decide tier, effort and turn budget for one unit of work.

    Deterministic and side-effect free. Every mutation of the running decision
    appends exactly one human-readable reason, so ``envelope.reasons`` fully
    explains ``envelope.tier`` and ``envelope.effort``.
    """
    fold = _Fold()

    # 1. Safety floor (T4.3). Always moves the decision: the lowest floor in the
    #    table is (cheap, low) and the fold starts at (cheap, minimal), so
    #    `reasons` is never empty.
    fold.raise_to(
        floor_for(signals.action_class, signals.task_risk),
        _floor_label(signals.action_class, signals.task_risk),
    )

    # 2. Breadth floor. Small and medium jobs contribute nothing.
    reading = measure_breadth(signals.declared)
    breadth_floor = BREADTH_FLOOR.get(reading.bucket)
    if breadth_floor is not None:
        fold.raise_to(breadth_floor, f"breadth:{reading.bucket.value} ({reading.counts()})")

    # 3. Explicit pin. Parsed leniently: an unreadable pin is reported and dropped
    #    rather than escalated, because a pin is a request to go UP and the floors
    #    above already guarantee the bottom. Note it still blocks the downgrade
    #    below — the guard there is "nobody asked for anything", not "nobody asked
    #    for something we could parse".
    pinned_tier = normalize_tier(signals.explicit_tier)
    pinned_effort = normalize_effort(signals.explicit_effort)
    if signals.explicit_tier is not None and pinned_tier is None:
        fold.note(f"explicit:tier={signals.explicit_tier!r} unrecognized (ignored)")
    if signals.explicit_effort is not None and pinned_effort is None:
        fold.note(f"explicit:effort={signals.explicit_effort!r} unrecognized (ignored)")
    if pinned_tier is not None or pinned_effort is not None:
        parts = []
        if pinned_tier is not None:
            parts.append(f"tier={pinned_tier.value}")
        if pinned_effort is not None:
            parts.append(f"effort={pinned_effort.value}")
        label = "explicit:" + ",".join(parts)
        # An unset axis contributes the identity, so pinning effort alone cannot
        # move the tier and vice versa.
        pin: TierEffort = (
            pinned_tier if pinned_tier is not None else IDENTITY_FLOOR[0],
            pinned_effort if pinned_effort is not None else IDENTITY_FLOOR[1],
        )
        if not fold.raise_to(pin, label):
            fold.note(f"{label} already satisfied by {_render(fold.point)} (no change)")

    # THE ONE DOWNGRADE. Applied last, over the joined result, and only when every
    # conjunct holds. Each one is load-bearing:
    #   bucket small          — a big job is not cheap even when it is safe;
    #   class <= internal_..  — nothing leaves the machine irreversibly;
    #   risk absent or low    — an unrecognized risk fails closed and blocks here;
    #   declared + confident  — the planner asserts the scope is COMPLETE, so a
    #                           surprise outside it will be caught, not absorbed;
    #   verify_command        — there is an automatic way to find out it was wrong;
    #   no explicit pin       — an explicit pin is NEVER downgraded. The guard is
    #                           the two is-None checks on the RAW signals, not a
    #                           special case further down, so an unparseable pin
    #                           blocks it too.
    declared = signals.declared
    risk = canonical_task_risk(signals.task_risk)
    if (
        reading.bucket is ScopeBreadth.SMALL
        and action_class_rank(signals.action_class)
        <= action_class_rank(ActionClass.INTERNAL_REVERSIBLE)
        and risk in (None, "low")
        and declared is not None
        and declared.confident
        and bool(str(declared.verify_command or "").strip())
        and signals.explicit_tier is None
        and signals.explicit_effort is None
    ):
        # Separate from the conjuncts above on purpose: those decide whether the
        # downgrade is PERMITTED, this decides whether it does anything. A
        # read-only job is already at (cheap, low), and claiming credit for a
        # move that did not happen would make the audit trail lie.
        if fold.point != DOWNGRADE_FLOOR:
            fold.point = DOWNGRADE_FLOOR
            fold.note(f"{DOWNGRADE_REASON} -> {_render(DOWNGRADE_FLOOR)}")

    turns = _turn_budget(reading)
    fold.note(
        f"turns:{turns} (declared_files={reading.file_count}, breadth={reading.bucket.value})"
    )

    tier, effort = fold.point
    return ExecutionEnvelope(
        tier=tier,
        effort=effort,
        max_tool_turns=turns,
        reasons=fold.reasons,
        policy_version=POLICY_VERSION,
    )
