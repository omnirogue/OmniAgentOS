"""The ONE tier/effort vocabulary surface (T4.4, trimmed).

:class:`~omniagentos.contracts.ModelTier` and
:class:`~omniagentos.contracts.ReasoningEffort` are canonical — they live in
contracts and are re-exported here so callers have a single import for "the
vocabulary and the parser for it". This module adds only what contracts cannot
hold: a *parser* (:func:`normalize_effort`, :func:`normalize_tier`) and the
tier -> default-effort pairing (:func:`default_effort`).

Why a parser at all: reasoning effort was spelled out independently in at least
six places, each with a slightly different accepted set —
``modelintel/router.py:32`` (``EFFORTS``, index-compared),
``swarm/router.py:90`` (``EFFORT_LEVELS``), ``adapters/common.py:219``
(``_REASONING_EFFORTS``, the only one that accepted ``minimal``),
``api/routes/system.py:97``, ``orchestrator/intent.py:30`` and
``orchestrator/contracts.py:35`` (both of which spell the TOP effort ``max``).
Every one of them was a hand-maintained literal that could drift from the others.
This is the replacement.

One value is deliberately NOT canonical and cannot be: ``"max"``, an effort
notch ABOVE ``xhigh`` that ``intake/fable.py``, ``orchestrator/intent.py`` and the
Claude CLI all accept today but ``ReasoningEffort`` has no member for. See
:data:`_EFFORT_ALIASES`.

Deliberately NOT here (cut from T4.4 by the operator): a bidirectional mapping
across all seven tier-shaped vocabularies (swarm simple/standard/complex,
modelintel difficulty, cascade rungs, ...). Those keep working untouched as views
over ModelTier; a mapping layer nothing reads would be dead weight with a
maintenance cost.

Parsing posture: **unknown input returns the caller's default (``None`` by
default), it does not raise and it does not escalate.** These functions parse
*requests* — a metadata key, a CLI flag, a config value — and an unreadable
request is best treated as absent, leaving the floors in
``omniagentos.policy.execution`` to decide. Escalation on unreadable input is the
right posture one layer down, inside the floor join, and it lives there.
"""

from __future__ import annotations

from omniagentos.contracts import (
    MODEL_TIER_RANK,
    REASONING_EFFORT_RANK,
    ModelTier,
    ReasoningEffort,
)

__all__ = [
    "DEFAULT_EFFORT_BY_TIER",
    "MODEL_TIER_RANK",
    "REASONING_EFFORT_RANK",
    "ModelTier",
    "ReasoningEffort",
    "default_effort",
    "normalize_effort",
    "normalize_tier",
]


# The effort a tier is worth when nothing else has an opinion. Intentionally
# identical to the risk ladder in policy/execution.TASK_RISK_FLOOR: a task that
# needs the strong tier needs high effort for the same reason it needed the strong
# tier, and pairing them differently in two places is how they drift.
DEFAULT_EFFORT_BY_TIER: dict[ModelTier, ReasoningEffort] = {
    ModelTier.CHEAP: ReasoningEffort.LOW,
    ModelTier.STANDARD: ReasoningEffort.MEDIUM,
    ModelTier.STRONG: ReasoningEffort.HIGH,
    ModelTier.MAX: ReasoningEffort.XHIGH,
}


def _squash(raw: object) -> str:
    """Fold punctuation/casing so ``"X-High"``, ``"x_high"`` and ``"xhigh"`` agree.

    Only punctuation and case are folded. No synonyms are invented: a spelling
    this codebase does not already use is not silently accepted, because a parser
    that guesses is how a seventh vocabulary gets born.
    """
    return str(raw).strip().lower().replace("-", "").replace("_", "").replace(" ", "")


# Empty by design, and the emptiness is the point.
#
# This table used to hold {"max": XHIGH}: "max" was a LIVE spelling
# (intake/fable.py:46 Effort, orchestrator/intent.py:24 + _VALID_EFFORTS:30) that
# contracts.ReasoningEffort had no member for, so the parser mapped it DOWN one
# notch rather than dropping it to None (dropping would have handed back whatever
# default sat underneath -- weaker still, losing four notches instead of one).
#
# The lead then added ReasoningEffort.MAX (rank 5) precisely because a canonical
# enum missing a value two modules declare IS the drift that produced the
# original two-metadata-keys bug. The alias is now not merely unnecessary but
# WRONG -- it would clamp a genuine max request down to xhigh, a quieter instance
# of the same class of bug. The parametrized round-trip test in
# tests/execution/test_vocab.py caught exactly that the moment MAX landed.
#
# Kept as an empty dict rather than deleted: _EFFORT_BY_SQUASHED splats it, and a
# future genuine synonym (a provider spelling "very_high", say) belongs here.
# NOTE: adapters still map per-provider -- CLI support for "max" was NOT
# independently confirmed, so canonical != universally accepted.
_EFFORT_ALIASES: dict[str, ReasoningEffort] = {}

_EFFORT_BY_SQUASHED: dict[str, ReasoningEffort] = {
    **{_squash(member.value): member for member in ReasoningEffort},
    **{_squash(alias): member for alias, member in _EFFORT_ALIASES.items()},
}

_TIER_BY_SQUASHED: dict[str, ModelTier] = {_squash(member.value): member for member in ModelTier}


def normalize_effort(
    value: object, *, default: ReasoningEffort | None = None
) -> ReasoningEffort | None:
    """Parse any spelling of a reasoning effort. Unknown -> ``default``.

    Accepts a ``ReasoningEffort``, a string in any casing with ``-``/``_``/spaces,
    or ``None``. Returns a real enum member so the value that reaches an adapter's
    argv is one of exactly five strings.

    Note for callers replacing an ``EFFORTS``-style literal: this vocabulary
    includes ``minimal``, which ``modelintel``/``swarm`` historically did not
    accept. Widening an accepted set is a behavior change; make it deliberately.
    """
    if isinstance(value, ReasoningEffort):
        return value
    if value is None:
        return default
    return _EFFORT_BY_SQUASHED.get(_squash(value), default)


def normalize_tier(value: object, *, default: ModelTier | None = None) -> ModelTier | None:
    """Parse any spelling of an execution tier. Unknown -> ``default``."""
    if isinstance(value, ModelTier):
        return value
    if value is None:
        return default
    return _TIER_BY_SQUASHED.get(_squash(value), default)


def default_effort(tier: ModelTier | str | None) -> ReasoningEffort:
    """The effort that pairs with ``tier``. An unreadable tier yields MEDIUM.

    MEDIUM, not MINIMAL: this answers "nobody said, what now" and the global
    default posture for this codebase is ``opus`` at ``medium``. It is not a
    safety floor — :func:`omniagentos.policy.execution.floor_for` is — so it does
    not need to fail closed at XHIGH.
    """
    normalized = normalize_tier(tier)
    if normalized is None:
        return ReasoningEffort.MEDIUM
    return DEFAULT_EFFORT_BY_TIER[normalized]
