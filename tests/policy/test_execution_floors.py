"""T4.3: the deterministic tier/effort floors.

Three things are being pinned here, and only one of them is the table:

1. the floor matrix itself (action class x task risk), as literals rather than a
   re-derivation, so a change to the tables shows up as a failing expectation;
2. that ``join`` is a genuine per-axis least-upper-bound — commutative,
   associative, idempotent, and incapable of lowering either axis in any argument
   order. Everything above it in T4.4 leans on "the policy only ratchets up", and
   that property is only true if this one is;
3. the fail-closed direction: an unrecognized risk label escalates, an absent one
   does not.
"""

from __future__ import annotations

import itertools

import pytest

from omniagentos.contracts import (
    MODEL_TIER_RANK,
    REASONING_EFFORT_RANK,
    ActionClass,
    ModelTier,
    ReasoningEffort,
)
from omniagentos.policy.execution import (
    ACTION_CLASS_FLOOR,
    IDENTITY_FLOOR,
    TASK_RISK_FLOOR,
    UNKNOWN_ACTION_CLASS_FLOOR,
    UNKNOWN_TASK_RISK_FLOOR,
    action_class_floor,
    action_class_rank,
    canonical_task_risk,
    floor_for,
    join,
    task_risk_floor,
)

CHEAP_LOW = (ModelTier.CHEAP, ReasoningEffort.LOW)
STANDARD_MEDIUM = (ModelTier.STANDARD, ReasoningEffort.MEDIUM)
STRONG_HIGH = (ModelTier.STRONG, ReasoningEffort.HIGH)
MAX_XHIGH = (ModelTier.MAX, ReasoningEffort.XHIGH)

ALL_POINTS = [(tier, effort) for tier in ModelTier for effort in ReasoningEffort]


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------

# Expected floor for every (action class, task risk) pair, written out rather
# than computed. "bogus" stands for any value a planner might write that this
# table does not know.
FLOOR_MATRIX: dict[ActionClass, dict[str | None, tuple[ModelTier, ReasoningEffort]]] = {
    ActionClass.READ_ONLY: {
        None: CHEAP_LOW,
        "low": CHEAP_LOW,
        "medium": STANDARD_MEDIUM,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
    ActionClass.SANDBOXED_CREATION: {
        None: CHEAP_LOW,
        "low": CHEAP_LOW,
        "medium": STANDARD_MEDIUM,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
    ActionClass.INTERNAL_REVERSIBLE: {
        None: STANDARD_MEDIUM,
        "low": STANDARD_MEDIUM,
        "medium": STANDARD_MEDIUM,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
    ActionClass.EXTERNAL_REVERSIBLE: {
        None: STANDARD_MEDIUM,
        "low": STANDARD_MEDIUM,
        "medium": STANDARD_MEDIUM,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
    ActionClass.CONSEQUENTIAL: {
        None: STRONG_HIGH,
        "low": STRONG_HIGH,
        "medium": STRONG_HIGH,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
    ActionClass.IRREVERSIBLE: {
        None: STRONG_HIGH,
        "low": STRONG_HIGH,
        "medium": STRONG_HIGH,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
        "bogus": STRONG_HIGH,
    },
}


@pytest.mark.parametrize(
    ("action_class", "task_risk", "expected"),
    [
        (action_class, risk, expected)
        for action_class, row in FLOOR_MATRIX.items()
        for risk, expected in row.items()
    ],
)
def test_floor_matrix(
    action_class: ActionClass,
    task_risk: str | None,
    expected: tuple[ModelTier, ReasoningEffort],
) -> None:
    assert floor_for(action_class, task_risk) == expected


def test_every_action_class_has_a_floor() -> None:
    """A class with no entry would silently take the fail-closed path forever."""
    assert set(ACTION_CLASS_FLOOR) == set(ActionClass)


def test_action_class_floor_table_is_the_documented_ladder() -> None:
    assert ACTION_CLASS_FLOOR[ActionClass.READ_ONLY] == CHEAP_LOW
    assert ACTION_CLASS_FLOOR[ActionClass.SANDBOXED_CREATION] == CHEAP_LOW
    assert ACTION_CLASS_FLOOR[ActionClass.INTERNAL_REVERSIBLE] == STANDARD_MEDIUM
    assert ACTION_CLASS_FLOOR[ActionClass.EXTERNAL_REVERSIBLE] == STANDARD_MEDIUM
    assert ACTION_CLASS_FLOOR[ActionClass.CONSEQUENTIAL] == STRONG_HIGH
    assert ACTION_CLASS_FLOOR[ActionClass.IRREVERSIBLE] == STRONG_HIGH


def test_task_risk_floor_table_is_the_documented_ladder() -> None:
    assert TASK_RISK_FLOOR == {
        "low": CHEAP_LOW,
        "medium": STANDARD_MEDIUM,
        "high": STRONG_HIGH,
        "critical": MAX_XHIGH,
    }


def test_floors_are_monotonic_in_action_class() -> None:
    """A riskier class never gets a cheaper floor than a safer one."""
    ordered = list(ActionClass)
    for lower, higher in itertools.pairwise(ordered):
        low_tier, low_effort = ACTION_CLASS_FLOOR[lower]
        high_tier, high_effort = ACTION_CLASS_FLOOR[higher]
        assert MODEL_TIER_RANK[high_tier] >= MODEL_TIER_RANK[low_tier]
        assert REASONING_EFFORT_RANK[high_effort] >= REASONING_EFFORT_RANK[low_effort]


def test_floors_are_monotonic_in_task_risk() -> None:
    for lower, higher in itertools.pairwise(["low", "medium", "high", "critical"]):
        low_tier, low_effort = TASK_RISK_FLOOR[lower]
        high_tier, high_effort = TASK_RISK_FLOOR[higher]
        assert MODEL_TIER_RANK[high_tier] >= MODEL_TIER_RANK[low_tier]
        assert REASONING_EFFORT_RANK[high_effort] >= REASONING_EFFORT_RANK[low_effort]


# --------------------------------------------------------------------------
# Fail-closed: unknown escalates, absent does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize("risk", ["bogus", "sev1", "LOW-ISH", "1", "none", "unknown", "true"])
def test_unknown_task_risk_takes_the_high_floor_not_the_low_one(risk: str) -> None:
    """The whole point: a value nobody can read must never DOWNGRADE the job."""
    assert task_risk_floor(risk) == UNKNOWN_TASK_RISK_FLOOR == STRONG_HIGH
    assert floor_for(ActionClass.READ_ONLY, risk) == STRONG_HIGH


@pytest.mark.parametrize("risk", [None, "", "   ", "\t\n"])
def test_absent_task_risk_is_the_schema_default_not_unknown(risk: str | None) -> None:
    """``tasks.risk`` defaults to 'low' and nobody writes it; absent must stay free.

    If blank were treated as unrecognized, every task in the database today would
    be floored at (strong, high) the moment this policy is wired in.
    """
    assert canonical_task_risk(risk) is None
    assert task_risk_floor(risk) == TASK_RISK_FLOOR["low"] == CHEAP_LOW
    assert floor_for(ActionClass.READ_ONLY, risk) == CHEAP_LOW


@pytest.mark.parametrize("risk", ["HIGH", " High ", "Critical", "\tmedium\n"])
def test_task_risk_is_case_and_whitespace_insensitive(risk: str) -> None:
    assert task_risk_floor(risk) == TASK_RISK_FLOOR[risk.strip().lower()]


@pytest.mark.parametrize("bad", ["banana", "", "read only", "READ_ONLY_ISH", "42"])
def test_unknown_action_class_takes_the_irreversible_floor(bad: str) -> None:
    assert action_class_floor(bad) == UNKNOWN_ACTION_CLASS_FLOOR == STRONG_HIGH
    assert floor_for(bad, None) == STRONG_HIGH
    # ...and it still composes upward with a risk label.
    assert floor_for(bad, "critical") == MAX_XHIGH


def test_action_class_string_values_are_accepted() -> None:
    """Call sites read this off a dict/DB row, where it is a bare string."""
    for member in ActionClass:
        assert action_class_floor(member.value) == ACTION_CLASS_FLOOR[member]


def test_floor_for_never_raises_on_garbage() -> None:
    """A policy module that raises takes the run down. It must fail closed instead."""
    for bad_class in [None, 0, object(), [], {"a": 1}]:
        for bad_risk in [None, "?", "", "\x00"]:
            tier, effort = floor_for(bad_class, bad_risk)  # type: ignore[arg-type]
            assert MODEL_TIER_RANK[tier] >= MODEL_TIER_RANK[ModelTier.STRONG]
            assert REASONING_EFFORT_RANK[effort] >= REASONING_EFFORT_RANK[ReasoningEffort.HIGH]


# --------------------------------------------------------------------------
# join(): the property the whole policy stands on
# --------------------------------------------------------------------------


def test_join_never_lowers_either_axis_in_any_argument_order() -> None:
    for a, b in itertools.product(ALL_POINTS, repeat=2):
        for left, right in ((a, b), (b, a)):
            tier, effort = join(left, right)
            assert MODEL_TIER_RANK[tier] >= MODEL_TIER_RANK[left[0]]
            assert MODEL_TIER_RANK[tier] >= MODEL_TIER_RANK[right[0]]
            assert REASONING_EFFORT_RANK[effort] >= REASONING_EFFORT_RANK[left[1]]
            assert REASONING_EFFORT_RANK[effort] >= REASONING_EFFORT_RANK[right[1]]


def test_join_is_commutative() -> None:
    for a, b in itertools.product(ALL_POINTS, repeat=2):
        assert join(a, b) == join(b, a)


def test_join_is_idempotent_and_has_an_identity() -> None:
    for point in ALL_POINTS:
        assert join(point, point) == point
        assert join(point, IDENTITY_FLOOR) == point
        assert join(IDENTITY_FLOOR, point) == point


def test_join_is_associative_so_fold_order_cannot_change_the_answer() -> None:
    """decide_execution folds three floors in a fixed order; this is why that
    order only affects the reasons, never the decision."""
    sample = [
        (ModelTier.CHEAP, ReasoningEffort.MINIMAL),
        (ModelTier.STANDARD, ReasoningEffort.LOW),
        (ModelTier.STRONG, ReasoningEffort.MEDIUM),
        (ModelTier.MAX, ReasoningEffort.XHIGH),
        (ModelTier.CHEAP, ReasoningEffort.HIGH),
    ]
    for a, b, c in itertools.product(sample, repeat=3):
        assert join(join(a, b), c) == join(a, join(b, c))


def test_join_maximizes_the_two_axes_independently() -> None:
    """The reason the floor is a PAIR: a strong model at low effort and a cheap
    model at high effort are different failures, and the floor covers both."""
    assert join(
        (ModelTier.STRONG, ReasoningEffort.LOW), (ModelTier.CHEAP, ReasoningEffort.XHIGH)
    ) == (ModelTier.STRONG, ReasoningEffort.XHIGH)


def test_join_escalates_unreadable_input_rather_than_lowering() -> None:
    """Defense in depth: an unreadable value inside a floor is a bug, and the safe
    direction for a bug in a safety floor is up."""
    assert join(("nonsense", "nonsense"), IDENTITY_FLOOR) == (  # type: ignore[arg-type]
        ModelTier.MAX,
        ReasoningEffort.XHIGH,
    )


# --------------------------------------------------------------------------
# Action-class rank: derived, not transcribed
# --------------------------------------------------------------------------


def test_action_class_rank_matches_the_runner_literal() -> None:
    """``runner/core.py`` hand-writes this ladder. Ours is derived from the enum's
    declaration order, so this asserts the two agree WITHOUT importing the runner
    (which would drag half the process graph into a policy unit test)."""
    assert [action_class_rank(member) for member in ActionClass] == [0, 1, 2, 3, 4, 5]
    assert action_class_rank(ActionClass.IRREVERSIBLE) > action_class_rank(
        ActionClass.CONSEQUENTIAL
    )
    assert action_class_rank(ActionClass.CONSEQUENTIAL) > action_class_rank(
        ActionClass.INTERNAL_REVERSIBLE
    )


def test_unknown_action_class_outranks_every_real_one() -> None:
    """So every ``rank(x) <= rank(threshold)`` guard fails closed for garbage."""
    unknown = action_class_rank("banana")
    assert all(unknown > action_class_rank(member) for member in ActionClass)
