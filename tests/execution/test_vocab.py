"""T4.4: the ONE effort/tier parser.

This replaces six hand-maintained literals. What is pinned here is the parsing
POSTURE, because it differs deliberately from the floors one layer down:
``normalize_*`` treats unreadable input as ABSENT (returns the caller's default),
whereas ``policy.execution.join`` treats it as dangerous and escalates. Parsing a
request and enforcing a floor are different jobs and fail in opposite directions.
"""

from __future__ import annotations

import pytest

from omniagentos.contracts import (
    MODEL_TIER_RANK,
    REASONING_EFFORT_RANK,
    ModelTier,
    ReasoningEffort,
)
from omniagentos.execution.vocab import (
    DEFAULT_EFFORT_BY_TIER,
    default_effort,
    normalize_effort,
    normalize_tier,
)


@pytest.mark.parametrize("member", list(ReasoningEffort))
def test_every_canonical_effort_round_trips(member: ReasoningEffort) -> None:
    assert normalize_effort(member) is member
    assert normalize_effort(member.value) is member
    assert normalize_effort(member.value.upper()) is member
    assert normalize_effort(f"  {member.value}  ") is member


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("xhigh", ReasoningEffort.XHIGH),
        ("x-high", ReasoningEffort.XHIGH),
        ("x_high", ReasoningEffort.XHIGH),
        ("X High", ReasoningEffort.XHIGH),
        ("MINIMAL", ReasoningEffort.MINIMAL),
        ("Medium", ReasoningEffort.MEDIUM),
    ],
)
def test_punctuation_and_casing_are_folded(raw: str, expected: ReasoningEffort) -> None:
    assert normalize_effort(raw) is expected


@pytest.mark.parametrize("raw", ["", "   ", "ultra", "insane", "0", "true", "highest", "lo"])
def test_unknown_effort_returns_the_default_and_never_raises(raw: str) -> None:
    """Unknown means ABSENT for a parser, and no synonyms are invented: a
    spelling this codebase does not already use is not silently accepted."""
    assert normalize_effort(raw) is None
    assert normalize_effort(raw, default=ReasoningEffort.MEDIUM) is ReasoningEffort.MEDIUM


@pytest.mark.parametrize("raw", ["max", "MAX", " Max "])
def test_max_resolves_exactly_and_is_never_clamped(raw: str) -> None:
    """'max' is a LIVE effort above xhigh (intake/fable.py:46,
    orchestrator/intent.py:24 + _VALID_EFFORTS:30) and it now resolves EXACTLY.

    This test previously asserted max -> XHIGH and carried the note "delete when
    contracts gains ReasoningEffort.MAX". The lead added that member (rank 5),
    because a canonical enum missing a value two modules declare is the same
    drift that produced the original two-metadata-keys bug. So the assertion is
    inverted rather than deleted: the down-map must NOT come back, since silently
    clamping a genuine max request to xhigh would be a quieter instance of
    exactly the bug this module exists to prevent.

    Note the separation of concerns: canonical does NOT mean universally
    accepted. Adapters still map per-provider (adapters/common.py
    cli_reasoning_effort), and CLI support for 'max' was never independently
    confirmed."""
    assert normalize_effort(raw) is ReasoningEffort.MAX


def test_max_pin_survives_the_policy_instead_of_being_ignored() -> None:
    """A caller asking for the strongest effort must not end up at the
    action-class floor. Now asserts MAX exactly, not the old XHIGH clamp."""
    from omniagentos.contracts import ActionClass
    from omniagentos.execution.policy import ExecutionSignals, decide_execution

    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, explicit_effort="max")
    )
    assert envelope.effort is ReasoningEffort.MAX
    assert not [reason for reason in envelope.reasons if "unrecognized" in reason]


@pytest.mark.parametrize("raw", [None, 0, 3.5, object(), [], {}])
def test_non_string_effort_input_degrades_instead_of_raising(raw: object) -> None:
    assert normalize_effort(raw) is None


def test_none_effort_returns_the_default_not_a_parse_of_the_word_none() -> None:
    assert normalize_effort(None) is None
    assert normalize_effort(None, default=ReasoningEffort.HIGH) is ReasoningEffort.HIGH
    # ...and the literal string "none" is not a member of the vocabulary either.
    assert normalize_effort("none") is None


def test_effort_vocabulary_is_a_superset_of_every_literal_it_replaces() -> None:
    """modelintel EFFORTS / swarm EFFORT_LEVELS / api _EFFORTS were
    (low, medium, high, xhigh); adapters/common additionally accepted 'minimal'."""
    for legacy in ("low", "medium", "high", "xhigh", "minimal"):
        assert normalize_effort(legacy) is not None


@pytest.mark.parametrize("member", list(ModelTier))
def test_every_canonical_tier_round_trips(member: ModelTier) -> None:
    assert normalize_tier(member) is member
    assert normalize_tier(member.value) is member
    assert normalize_tier(f" {member.value.upper()} ") is member


@pytest.mark.parametrize("raw", ["", "strongest", "cheapest", "opus", "1", None, 7, object()])
def test_unknown_tier_returns_the_default(raw: object) -> None:
    assert normalize_tier(raw) is None
    assert normalize_tier(raw, default=ModelTier.STANDARD) is ModelTier.STANDARD


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (ModelTier.CHEAP, ReasoningEffort.LOW),
        (ModelTier.STANDARD, ReasoningEffort.MEDIUM),
        (ModelTier.STRONG, ReasoningEffort.HIGH),
        (ModelTier.MAX, ReasoningEffort.XHIGH),
        ("strong", ReasoningEffort.HIGH),
    ],
)
def test_default_effort_pairs_with_the_tier(
    tier: ModelTier | str, expected: ReasoningEffort
) -> None:
    assert default_effort(tier) is expected


def test_default_effort_for_an_unreadable_tier_is_medium() -> None:
    """The house default posture ('opus at medium'), not a safety decision — the
    floors in policy.execution are what fail closed."""
    assert default_effort(None) is ReasoningEffort.MEDIUM
    assert default_effort("banana") is ReasoningEffort.MEDIUM


def test_default_effort_table_covers_every_tier_and_is_monotonic() -> None:
    assert set(DEFAULT_EFFORT_BY_TIER) == set(ModelTier)
    ordered = sorted(ModelTier, key=MODEL_TIER_RANK.__getitem__)
    ranks = [REASONING_EFFORT_RANK[DEFAULT_EFFORT_BY_TIER[tier]] for tier in ordered]
    assert ranks == sorted(ranks)


def test_default_effort_pairing_matches_the_task_risk_ladder() -> None:
    """The same pairing is spelled in policy/execution.TASK_RISK_FLOOR. If the two
    ever disagree, one of them is silently wrong; this is the tripwire."""
    from omniagentos.policy.execution import TASK_RISK_FLOOR

    for tier, effort in TASK_RISK_FLOOR.values():
        assert DEFAULT_EFFORT_BY_TIER[tier] is effort
