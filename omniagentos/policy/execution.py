"""Deterministic tier/effort FLOORS (T4.3).

This lives in ``omniagentos/policy/`` ON PURPOSE. It is a *safety floor*, not a
routing preference, so it belongs beside :func:`omniagentos.policy.evaluate_action`
under the same protection and the same fail-closed posture. The decision function
that consumes it (``omniagentos.execution.policy.decide_execution``) may grow lane
heuristics; these tables may not.

Three invariants hold for everything in this module:

1. **It only ever RAISES.** :func:`join` is a per-axis maximum, so composing any
   number of floors can never lower a tier or an effort. The single downgrade in
   the whole system lives in ``execution.policy`` and is guarded by a conjunction
   of five conditions; there is none here.
2. **It fails CLOSED, and it never crashes.** An unrecognized ``task_risk`` value
   yields the HIGH floor, not the low one; an unrecognized ``action_class`` yields
   the IRREVERSIBLE floor. This mirrors :func:`omniagentos.policy.is_hard_stop`,
   which returns ``True`` (deny) for a class it cannot parse. A policy module that
   raises on malformed input would take a run down; one that silently downgrades
   would be worse. Escalating is the only safe third option.
3. **It is pure.** No clock, no I/O, no config, no global state. The same inputs
   give the same floor forever, which is what makes an ExecutionEnvelope's
   ``reasons`` list an audit trail rather than a guess.

A note on ``task_risk``: ``tasks.risk`` defaults to ``'low'`` in
``contracts/schema.sql`` and is written by nobody today, so in practice it
contributes nothing until a planner starts setting it. ``None`` is therefore
treated as that schema default (``'low'``) rather than as "unknown" — but a value
that is *present and unrecognized* must never silently downgrade, hence rule 2.
"""

from __future__ import annotations

from omniagentos.contracts import (
    MODEL_TIER_RANK,
    REASONING_EFFORT_RANK,
    ActionClass,
    ModelTier,
    ReasoningEffort,
)

__all__ = [
    "ACTION_CLASS_FLOOR",
    "IDENTITY_FLOOR",
    "TASK_RISK_FLOOR",
    "UNKNOWN_ACTION_CLASS_FLOOR",
    "UNKNOWN_TASK_RISK_FLOOR",
    "TierEffort",
    "action_class_floor",
    "action_class_rank",
    "canonical_task_risk",
    "floor_for",
    "join",
    "task_risk_floor",
]

# One (tier, effort) point. Ordered per-axis by the rank tables in contracts.
TierEffort = tuple[ModelTier, ReasoningEffort]


# The neutral element of :func:`join`: joining with it changes nothing. This is
# where a decision STARTS, not where it may end up.
IDENTITY_FLOOR: TierEffort = (ModelTier.CHEAP, ReasoningEffort.MINIMAL)


# What the *shape of the act* demands, independent of the task's own risk label.
# Read-only and sandboxed work is cheap because being wrong is free; anything that
# reaches outside the sandbox reversibly gets a real model; consequential and
# irreversible acts get the strong tier because the cost of a mistake is no longer
# bounded by an undo.
ACTION_CLASS_FLOOR: dict[ActionClass, TierEffort] = {
    ActionClass.READ_ONLY: (ModelTier.CHEAP, ReasoningEffort.LOW),
    ActionClass.SANDBOXED_CREATION: (ModelTier.CHEAP, ReasoningEffort.LOW),
    ActionClass.INTERNAL_REVERSIBLE: (ModelTier.STANDARD, ReasoningEffort.MEDIUM),
    ActionClass.EXTERNAL_REVERSIBLE: (ModelTier.STANDARD, ReasoningEffort.MEDIUM),
    ActionClass.CONSEQUENTIAL: (ModelTier.STRONG, ReasoningEffort.HIGH),
    ActionClass.IRREVERSIBLE: (ModelTier.STRONG, ReasoningEffort.HIGH),
}

# What the task's declared risk demands. Keys are the raw ``tasks.risk`` strings,
# not an enum: the column is free-form TEXT and no enum exists in contracts, so
# introducing one here would be a fourth vocabulary rather than a floor.
TASK_RISK_FLOOR: dict[str, TierEffort] = {
    "low": (ModelTier.CHEAP, ReasoningEffort.LOW),
    "medium": (ModelTier.STANDARD, ReasoningEffort.MEDIUM),
    "high": (ModelTier.STRONG, ReasoningEffort.HIGH),
    "critical": (ModelTier.MAX, ReasoningEffort.XHIGH),
}

# Fail-closed defaults. A risk label we cannot read is treated as HIGH — the
# explicit requirement — and an action class we cannot read is treated as the
# worst class, matching ``is_hard_stop``'s "unknown means deny" posture.
UNKNOWN_TASK_RISK_FLOOR: TierEffort = TASK_RISK_FLOOR["high"]
UNKNOWN_ACTION_CLASS_FLOOR: TierEffort = ACTION_CLASS_FLOOR[ActionClass.IRREVERSIBLE]

# Derived from ActionClass's declaration order, which contracts documents as
# trust-significant (lowest -> highest risk). Deriving rather than transcribing is
# deliberate: runner/core.py already hand-writes ``_ACTION_CLASS_RANK`` and a
# second literal copy could drift from it. The test suite pins the derived order
# against the literal so a reorder of the enum is caught, not absorbed.
_ACTION_CLASS_RANK: dict[ActionClass, int] = {
    member: index for index, member in enumerate(ActionClass)
}


def action_class_rank(action_class: ActionClass | str) -> int:
    """Trust rank of ``action_class``; unknown ranks HIGHEST (fail closed).

    Used to answer "is this class at most internal_reversible?" without spelling
    the ordering a second time. An unparseable class outranks every real one, so
    every ``rank(x) <= rank(threshold)`` test it appears in is False.
    """
    try:
        return _ACTION_CLASS_RANK[ActionClass(action_class)]
    except (KeyError, TypeError, ValueError):
        return max(_ACTION_CLASS_RANK.values()) + 1


def _as_tier(value: ModelTier | str) -> ModelTier:
    """Coerce to ModelTier; anything unreadable escalates to MAX (fail closed).

    Only reachable from :func:`join`, whose inputs come from the tables above or
    from already-parsed explicit pins — callers that accept untrusted text should
    parse it with ``execution.vocab.normalize_tier`` first, which DROPS an
    unreadable pin instead of escalating it. The difference is intentional: a pin
    is a request to go up, so an unreadable one simply does not apply; a value
    that has already reached the join is part of the floor, so an unreadable one
    must not quietly lower it.
    """
    try:
        tier = ModelTier(value)
    except (TypeError, ValueError):
        return ModelTier.MAX
    return tier if tier in MODEL_TIER_RANK else ModelTier.MAX


def _as_effort(value: ReasoningEffort | str) -> ReasoningEffort:
    """Coerce to ReasoningEffort; anything unreadable escalates to XHIGH."""
    try:
        effort = ReasoningEffort(value)
    except (TypeError, ValueError):
        return ReasoningEffort.XHIGH
    return effort if effort in REASONING_EFFORT_RANK else ReasoningEffort.XHIGH


def join(a: TierEffort, b: TierEffort) -> TierEffort:
    """Least upper bound of two floors: per-axis maximum. THIS ONLY EVER RAISES.

    The two axes are independent — ``(strong, low)`` joined with ``(cheap, xhigh)``
    is ``(strong, xhigh)``, not either input. That is the point: a strong model at
    a low effort and a cheap model thinking hard are different failures, and a
    floor must cover both.

    Commutative, associative, idempotent, and monotonic, with
    :data:`IDENTITY_FLOOR` as the identity — so the order in which
    ``decide_execution`` folds its floors cannot change the answer, only the order
    of the reasons it records.
    """
    tier_a, effort_a = _as_tier(a[0]), _as_effort(a[1])
    tier_b, effort_b = _as_tier(b[0]), _as_effort(b[1])
    tier = max((tier_a, tier_b), key=MODEL_TIER_RANK.__getitem__)
    effort = max((effort_a, effort_b), key=REASONING_EFFORT_RANK.__getitem__)
    return (tier, effort)


def canonical_task_risk(task_risk: str | None) -> str | None:
    """Lowercase, stripped risk label, or ``None`` when the field is ABSENT.

    Absent (``None`` / blank) is not the same as unrecognized. Absent means the
    column still holds its schema default and carries no information; the caller
    treats it as ``'low'``. Unrecognized means somebody wrote something we cannot
    read, which is a signal in itself — the returned string simply is not a key of
    :data:`TASK_RISK_FLOOR`, and :func:`task_risk_floor` escalates it.
    """
    if task_risk is None:
        return None
    canonical = str(task_risk).strip().lower()
    return canonical or None


def task_risk_floor(task_risk: str | None) -> TierEffort:
    """Floor implied by ``tasks.risk``. Absent -> the 'low' floor; unknown -> HIGH."""
    canonical = canonical_task_risk(task_risk)
    if canonical is None:
        # The schema default ('low') by another name. Contributes nothing once
        # joined with any action-class floor, which is exactly right for a column
        # nothing writes yet.
        return TASK_RISK_FLOOR["low"]
    return TASK_RISK_FLOOR.get(canonical, UNKNOWN_TASK_RISK_FLOOR)


def action_class_floor(action_class: ActionClass | str) -> TierEffort:
    """Floor implied by the action class; unknown -> the IRREVERSIBLE floor."""
    try:
        normalized = ActionClass(action_class)
    except (TypeError, ValueError):
        return UNKNOWN_ACTION_CLASS_FLOOR
    return ACTION_CLASS_FLOOR.get(normalized, UNKNOWN_ACTION_CLASS_FLOOR)


def floor_for(action_class: ActionClass | str, task_risk: str | None = None) -> TierEffort:
    """The safety floor for an act of this class at this declared risk.

    The join means the two inputs cannot cancel: a ``read_only`` step on a
    ``critical`` task still gets ``(max, xhigh)``, and a ``consequential`` step on
    a ``low`` task still gets ``(strong, high)``.
    """
    return join(action_class_floor(action_class), task_risk_floor(task_risk))
