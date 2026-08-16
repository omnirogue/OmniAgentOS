"""T4.4: decide_execution — the deterministic execution policy.

The load-bearing claims under test:

* the fold only ever ratchets UP (floor, then breadth, then explicit pin);
* the ONE downgrade needs every one of its conjuncts, so each is flipped
  individually and must block it on its own;
* an explicit pin is never downgraded, including a pin we could not even parse;
* every mutation is explained — ``reasons`` is never empty and never silently
  absorbs a change;
* it is pure: same signals, same envelope, twice.
"""

from __future__ import annotations

import pytest

from omniagentos.contracts import (
    ActionClass,
    DeclaredScope,
    ModelTier,
    ReasoningEffort,
    SandboxLevel,
    ScopeEnforcement,
)
from omniagentos.execution.policy import (
    BREADTH_FLOOR,
    DOWNGRADE_REASON,
    POLICY_VERSION,
    TURN_BUDGET_BASE,
    TURN_BUDGET_LARGE,
    TURN_BUDGET_WIDE,
    ExecutionSignals,
    ScopeBreadth,
    decide_execution,
    is_broad_create_root,
    measure_breadth,
)


def scope(**kwargs: object) -> DeclaredScope:
    return DeclaredScope(**kwargs)  # type: ignore[arg-type]


def files(count: int, *, prefix: str = "src/pkg/mod") -> list[str]:
    return [f"{prefix}{index}.py" for index in range(count)]


def point(signals: ExecutionSignals) -> tuple[ModelTier | None, ReasoningEffort | None]:
    envelope = decide_execution(signals)
    return (envelope.tier, envelope.effort)


# --------------------------------------------------------------------------
# Breadth scoring
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        ("src", True),
        ("tests", True),
        ("", True),  # the repo root itself: the broadest licence there is
        (".", True),
        ("/", True),
        ("./src", True),  # same top-level dir, spelled differently
        ("src/", True),
        ("src/api", False),
        ("./src/api", False),
        ("a/b/c", False),
        ("/Users/x/project/src", False),
    ],
)
def test_broad_create_root_detection(root: str, expected: bool) -> None:
    assert is_broad_create_root(root) is expected


@pytest.mark.parametrize(
    ("declared", "expected_score", "expected_bucket"),
    [
        (None, 0, ScopeBreadth.SMALL),
        (scope(), 0, ScopeBreadth.SMALL),
        (scope(files_to_modify=["a.py"]), 1, ScopeBreadth.SMALL),
        (scope(files_to_modify=["a.py", "b.py"]), 2, ScopeBreadth.MEDIUM),
        (scope(files_to_modify=files(4)), 4, ScopeBreadth.MEDIUM),
        (scope(files_to_modify=files(5)), 5, ScopeBreadth.LARGE),
        (scope(files_to_modify=files(8)), 8, ScopeBreadth.LARGE),
        (scope(files_to_modify=files(9)), 9, ScopeBreadth.WIDE),
        # A create root counts double...
        (scope(create_roots=["src/api"]), 2, ScopeBreadth.MEDIUM),
        (scope(create_roots=["src/api", "src/web"]), 4, ScopeBreadth.MEDIUM),
        # ...and a BROAD one counts double again on top (2 + 3 = 5).
        (scope(create_roots=["src"]), 5, ScopeBreadth.LARGE),
        (scope(create_roots=["src", "tests"]), 10, ScopeBreadth.WIDE),
        # Mixed: 3 files + 1 nested root (2) + 1 broad root (5) = 10.
        (
            scope(files_to_modify=files(3), create_roots=["src/api", "docs"]),
            10,
            ScopeBreadth.WIDE,
        ),
        # The three file lists are UNIONED, not summed.
        (
            scope(files_to_modify=["a.py"], files_to_create=["a.py"], files_to_delete=["a.py"]),
            1,
            ScopeBreadth.SMALL,
        ),
        (
            scope(files_to_modify=["a.py"], files_to_create=["b.py"], files_to_delete=["c.py"]),
            3,
            ScopeBreadth.MEDIUM,
        ),
    ],
)
def test_breadth_buckets(
    declared: DeclaredScope | None, expected_score: int, expected_bucket: ScopeBreadth
) -> None:
    reading = measure_breadth(declared)
    assert (reading.score, reading.bucket) == (expected_score, expected_bucket)


def test_breadth_dedupes_spellings_of_the_same_path() -> None:
    """One file listed three ways is one file, not three."""
    reading = measure_breadth(
        scope(files_to_modify=["a.py", "./a.py", "a.py/"], create_roots=["src", "./src", "src/"])
    )
    assert reading.file_count == 1
    assert reading.create_root_count == 1
    assert reading.broad_root_count == 1
    assert reading.score == 1 + 2 + 3


def test_breadth_ignores_blank_file_entries() -> None:
    reading = measure_breadth(scope(files_to_modify=["a.py", "", "   "]))
    assert reading.file_count == 1


# --------------------------------------------------------------------------
# The fold: floor -> breadth -> explicit
# --------------------------------------------------------------------------


def test_read_only_with_no_signals_lands_at_the_cheapest_real_floor() -> None:
    """Starts at (cheap, minimal); the action-class floor is the first thing that
    moves it, which is why ``reasons`` can never come back empty."""
    envelope = decide_execution(ExecutionSignals(action_class=ActionClass.READ_ONLY))
    assert (envelope.tier, envelope.effort) == (ModelTier.CHEAP, ReasoningEffort.LOW)
    assert envelope.reasons
    assert envelope.reasons[0].startswith("floor:action_class=read_only")
    assert envelope.reasons[0].endswith("-> cheap/low")


@pytest.mark.parametrize(
    ("action_class", "expected"),
    [
        (ActionClass.READ_ONLY, (ModelTier.CHEAP, ReasoningEffort.LOW)),
        (ActionClass.SANDBOXED_CREATION, (ModelTier.CHEAP, ReasoningEffort.LOW)),
        (ActionClass.INTERNAL_REVERSIBLE, (ModelTier.STANDARD, ReasoningEffort.MEDIUM)),
        (ActionClass.EXTERNAL_REVERSIBLE, (ModelTier.STANDARD, ReasoningEffort.MEDIUM)),
        (ActionClass.CONSEQUENTIAL, (ModelTier.STRONG, ReasoningEffort.HIGH)),
        (ActionClass.IRREVERSIBLE, (ModelTier.STRONG, ReasoningEffort.HIGH)),
    ],
)
def test_action_class_floor_reaches_the_envelope(
    action_class: ActionClass, expected: tuple[ModelTier, ReasoningEffort]
) -> None:
    assert point(ExecutionSignals(action_class=action_class)) == expected


def test_task_risk_can_only_raise_never_lower() -> None:
    assert point(ExecutionSignals(action_class=ActionClass.CONSEQUENTIAL, task_risk="low")) == (
        ModelTier.STRONG,
        ReasoningEffort.HIGH,
    )
    assert point(ExecutionSignals(action_class=ActionClass.READ_ONLY, task_risk="critical")) == (
        ModelTier.MAX,
        ReasoningEffort.XHIGH,
    )


def test_unrecognized_task_risk_escalates_and_says_so() -> None:
    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, task_risk="spicy")
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.STRONG, ReasoningEffort.HIGH)
    assert "task_risk=spicy(unrecognized)" in envelope.reasons[0]


def test_absent_task_risk_is_not_named_in_the_reason() -> None:
    envelope = decide_execution(ExecutionSignals(action_class=ActionClass.READ_ONLY))
    assert "task_risk" not in envelope.reasons[0]


@pytest.mark.parametrize(
    ("bucket", "declared", "expected"),
    [
        (ScopeBreadth.SMALL, scope(files_to_modify=files(1)), None),
        (ScopeBreadth.MEDIUM, scope(files_to_modify=files(3)), None),
        (ScopeBreadth.LARGE, scope(files_to_modify=files(6)), BREADTH_FLOOR[ScopeBreadth.LARGE]),
        (ScopeBreadth.WIDE, scope(files_to_modify=files(12)), BREADTH_FLOOR[ScopeBreadth.WIDE]),
    ],
)
def test_breadth_floor_applies_only_from_large_upward(
    bucket: ScopeBreadth,
    declared: DeclaredScope,
    expected: tuple[ModelTier, ReasoningEffort] | None,
) -> None:
    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, declared=declared)
    )
    assert measure_breadth(declared).bucket is bucket
    breadth_reasons = [reason for reason in envelope.reasons if reason.startswith("breadth:")]
    if expected is None:
        assert breadth_reasons == []
        assert (envelope.tier, envelope.effort) == (ModelTier.CHEAP, ReasoningEffort.LOW)
    else:
        assert len(breadth_reasons) == 1
        assert (envelope.tier, envelope.effort) == expected
        # "reason with the counts" — the numbers that produced the bucket.
        assert "files=" in breadth_reasons[0]
        assert "create_roots=" in breadth_reasons[0]
        assert "broad_roots=" in breadth_reasons[0]
        assert "score=" in breadth_reasons[0]


def test_breadth_floor_is_recorded_only_when_it_actually_moves_the_decision() -> None:
    """A wide scope on a consequential act adds nothing — it is already above the
    breadth floor — so it must not claim credit in the reasons."""
    envelope = decide_execution(
        ExecutionSignals(
            action_class=ActionClass.CONSEQUENTIAL, declared=scope(files_to_modify=files(12))
        )
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.STRONG, ReasoningEffort.HIGH)
    assert not [reason for reason in envelope.reasons if reason.startswith("breadth:")]


# --------------------------------------------------------------------------
# Explicit pins
# --------------------------------------------------------------------------


def test_explicit_pin_raises_and_is_recorded() -> None:
    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, explicit_tier=ModelTier.MAX)
    )
    assert envelope.tier is ModelTier.MAX
    assert any(reason.startswith("explicit:tier=max") for reason in envelope.reasons)


def test_explicit_pin_accepts_strings_in_any_spelling() -> None:
    envelope = decide_execution(
        ExecutionSignals(
            action_class=ActionClass.READ_ONLY, explicit_tier=" Max ", explicit_effort="X-High"
        )
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.MAX, ReasoningEffort.XHIGH)
    assert any("explicit:tier=max,effort=xhigh" in reason for reason in envelope.reasons)


def test_pinning_one_axis_does_not_move_the_other() -> None:
    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, explicit_effort=ReasoningEffort.XHIGH)
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.CHEAP, ReasoningEffort.XHIGH)


def test_explicit_pin_below_the_floor_cannot_lower_it() -> None:
    """A pin is a request to go up. It has no authority to go down."""
    envelope = decide_execution(
        ExecutionSignals(
            action_class=ActionClass.IRREVERSIBLE,
            explicit_tier=ModelTier.CHEAP,
            explicit_effort=ReasoningEffort.MINIMAL,
        )
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.STRONG, ReasoningEffort.HIGH)
    assert any("no change" in reason for reason in envelope.reasons)


def test_unparseable_pin_is_dropped_and_reported_not_escalated() -> None:
    envelope = decide_execution(
        ExecutionSignals(
            action_class=ActionClass.READ_ONLY, explicit_tier="strongest", explicit_effort="ultra"
        )
    )
    assert (envelope.tier, envelope.effort) == (ModelTier.CHEAP, ReasoningEffort.LOW)
    assert any("unrecognized (ignored)" in reason for reason in envelope.reasons)


# --------------------------------------------------------------------------
# THE ONE DOWNGRADE — every conjunct tested for necessity
# --------------------------------------------------------------------------

DOWNGRADEABLE = ExecutionSignals(
    action_class=ActionClass.INTERNAL_REVERSIBLE,
    task_risk=None,
    declared=scope(
        files_to_modify=["src/api/routes.py"], confident=True, verify_command="pytest -q"
    ),
)
UNDOWNGRADED = (ModelTier.STANDARD, ReasoningEffort.MEDIUM)
DOWNGRADED = (ModelTier.CHEAP, ReasoningEffort.LOW)


def downgraded(envelope_reasons: list[str]) -> bool:
    return any(DOWNGRADE_REASON in reason for reason in envelope_reasons)


def test_the_baseline_actually_downgrades() -> None:
    """If this stops holding, every necessity test below passes vacuously."""
    envelope = decide_execution(DOWNGRADEABLE)
    assert (envelope.tier, envelope.effort) == DOWNGRADED
    assert downgraded(envelope.reasons)


@pytest.mark.parametrize(
    ("conjunct", "signals"),
    [
        # bucket == small
        (
            "breadth",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py", "b.py"], confident=True, verify_command="pytest -q"
                ),
            ),
        ),
        # action_class <= internal_reversible
        (
            "action_class",
            ExecutionSignals(
                action_class=ActionClass.EXTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
            ),
        ),
        # task_risk in (None, 'low')
        (
            "task_risk-medium",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                task_risk="medium",
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
            ),
        ),
        # ...and an unrecognized risk fails closed here too.
        (
            "task_risk-unknown",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                task_risk="spicy",
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
            ),
        ),
        # declared is not None
        (
            "declared-missing",
            ExecutionSignals(action_class=ActionClass.INTERNAL_REVERSIBLE, declared=None),
        ),
        # declared.confident
        (
            "not-confident",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py"], confident=False, verify_command="pytest -q"
                ),
            ),
        ),
        # declared.verify_command
        (
            "no-verify-command",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(files_to_modify=["a.py"], confident=True, verify_command=None),
            ),
        ),
        (
            "blank-verify-command",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(files_to_modify=["a.py"], confident=True, verify_command="   "),
            ),
        ),
        # explicit_tier is None
        (
            "explicit-tier-pinned",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
                explicit_tier=ModelTier.STANDARD,
            ),
        ),
        # explicit_effort is None
        (
            "explicit-effort-pinned",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
                explicit_effort=ReasoningEffort.MEDIUM,
            ),
        ),
        # An UNPARSEABLE pin still counts as "somebody asked", because the guard is
        # the two is-None checks on the raw signals, not a special case.
        (
            "explicit-garbage-pinned",
            ExecutionSignals(
                action_class=ActionClass.INTERNAL_REVERSIBLE,
                declared=scope(
                    files_to_modify=["a.py"], confident=True, verify_command="pytest -q"
                ),
                explicit_tier="strongest",
            ),
        ),
    ],
)
def test_every_downgrade_conjunct_is_necessary(conjunct: str, signals: ExecutionSignals) -> None:
    """Flip exactly one conjunct off the downgradeable baseline; the downgrade
    must not fire. Each case is a separate reason the cheap path is unsafe."""
    envelope = decide_execution(signals)
    assert not downgraded(envelope.reasons), conjunct
    assert (envelope.tier, envelope.effort) != DOWNGRADED, conjunct


def test_downgrade_survives_an_explicit_low_task_risk() -> None:
    """'low' and absent are equivalent here — the schema default is 'low'."""
    signals = ExecutionSignals(
        action_class=ActionClass.INTERNAL_REVERSIBLE,
        task_risk="LOW",
        declared=scope(files_to_modify=["a.py"], confident=True, verify_command="pytest -q"),
    )
    envelope = decide_execution(signals)
    assert (envelope.tier, envelope.effort) == DOWNGRADED
    assert downgraded(envelope.reasons)


def test_downgrade_is_not_recorded_when_it_would_change_nothing() -> None:
    """A read-only job is already at (cheap, low); there is nothing to downgrade,
    so no reason is invented."""
    signals = ExecutionSignals(
        action_class=ActionClass.READ_ONLY,
        declared=scope(files_to_modify=["a.py"], confident=True, verify_command="pytest -q"),
    )
    envelope = decide_execution(signals)
    assert (envelope.tier, envelope.effort) == DOWNGRADED
    assert not downgraded(envelope.reasons)


def test_downgrade_never_goes_below_cheap_low() -> None:
    """It clamps to the (cheap, low) floor, not to the (cheap, minimal) start."""
    envelope = decide_execution(DOWNGRADEABLE)
    assert envelope.effort is not ReasoningEffort.MINIMAL


# --------------------------------------------------------------------------
# Turn budget
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (None, TURN_BUDGET_BASE),
        (scope(), TURN_BUDGET_BASE),
        (scope(files_to_modify=files(5)), TURN_BUDGET_BASE),
        # A LARGE-breadth job with few named files still gets the base budget:
        # the budget keys off declared FILES, not off the breadth score.
        (scope(files_to_modify=files(5), create_roots=["src/api"]), TURN_BUDGET_BASE),
        (scope(files_to_modify=files(6)), TURN_BUDGET_LARGE),
        (scope(files_to_modify=files(8)), TURN_BUDGET_LARGE),
        # 9 files scores 9, which is already WIDE — so the ">= 10 files" rule is
        # subsumed by the wide bucket for file-only scopes, and both land on 36.
        (scope(files_to_modify=files(9)), TURN_BUDGET_WIDE),
        (scope(files_to_modify=files(10)), TURN_BUDGET_WIDE),
        (scope(files_to_modify=files(40)), TURN_BUDGET_WIDE),
        # Wide by create-root breadth alone, with only three named files.
        (
            scope(files_to_modify=files(3), create_roots=["src", "tests"]),
            TURN_BUDGET_WIDE,
        ),
    ],
)
def test_turn_budget(declared: DeclaredScope | None, expected: int) -> None:
    envelope = decide_execution(
        ExecutionSignals(action_class=ActionClass.READ_ONLY, declared=declared)
    )
    assert envelope.max_tool_turns == expected


def test_turn_budget_reason_is_always_appended() -> None:
    for declared in (None, scope(), scope(files_to_modify=files(7))):
        envelope = decide_execution(
            ExecutionSignals(action_class=ActionClass.READ_ONLY, declared=declared)
        )
        turn_reasons = [reason for reason in envelope.reasons if reason.startswith("turns:")]
        assert len(turn_reasons) == 1
        assert "declared_files=" in turn_reasons[0]
        assert "breadth=" in turn_reasons[0]


# --------------------------------------------------------------------------
# Envelope hygiene: reasons, purity, and the fields this policy must not touch
# --------------------------------------------------------------------------

SAMPLE_SIGNALS = [
    ExecutionSignals(action_class=ActionClass.READ_ONLY),
    ExecutionSignals(action_class=ActionClass.IRREVERSIBLE, task_risk="critical"),
    ExecutionSignals(action_class=ActionClass.READ_ONLY, task_risk="nonsense"),
    ExecutionSignals(
        action_class=ActionClass.SANDBOXED_CREATION, declared=scope(files_to_modify=files(20))
    ),
    ExecutionSignals(action_class=ActionClass.CONSEQUENTIAL, explicit_tier=ModelTier.MAX),
    DOWNGRADEABLE,
    ExecutionSignals(action_class="banana"),
]


@pytest.mark.parametrize("signals", SAMPLE_SIGNALS)
def test_reasons_are_never_empty_and_always_explain_the_result(
    signals: ExecutionSignals,
) -> None:
    envelope = decide_execution(signals)
    assert envelope.reasons
    assert envelope.reasons[0].startswith("floor:")
    assert envelope.reasons[-1].startswith("turns:")
    assert envelope.policy_version == POLICY_VERSION
    # The decided point appears verbatim in the last reason that moved it.
    rendered = f"{envelope.tier.value}/{envelope.effort.value}"  # type: ignore[union-attr]
    moves = [reason for reason in envelope.reasons if "->" in reason]
    assert moves and moves[-1].endswith(rendered)


@pytest.mark.parametrize("signals", SAMPLE_SIGNALS)
def test_decide_execution_is_deterministic(signals: ExecutionSignals) -> None:
    assert decide_execution(signals).model_dump() == decide_execution(signals).model_dump()


@pytest.mark.parametrize("signals", SAMPLE_SIGNALS)
def test_decide_execution_does_not_mutate_its_input(signals: ExecutionSignals) -> None:
    before = signals.declared.model_dump() if signals.declared is not None else None
    decide_execution(signals)
    after = signals.declared.model_dump() if signals.declared is not None else None
    assert before == after


def test_provisioning_fields_are_left_at_their_contract_defaults() -> None:
    """sandbox_level / extra_dirs / scope_enforcement are owned elsewhere. This
    policy decides tier, effort and turns, and must not silently pre-empt them."""
    envelope = decide_execution(ExecutionSignals(action_class=ActionClass.IRREVERSIBLE))
    assert envelope.sandbox_level is SandboxLevel.READ_ONLY
    assert envelope.extra_dirs == []
    assert envelope.scope_enforcement is ScopeEnforcement.OFF


def test_unknown_action_class_fails_closed_through_the_whole_policy() -> None:
    envelope = decide_execution(ExecutionSignals(action_class="banana"))
    assert (envelope.tier, envelope.effort) == (ModelTier.STRONG, ReasoningEffort.HIGH)


def test_speed_hint_and_lane_cannot_change_the_decision() -> None:
    """Structural, not incidental: the policy is monotonic, so a request to go
    faster has nothing it is permitted to lower."""
    base = ExecutionSignals(action_class=ActionClass.CONSEQUENTIAL, task_risk="high")
    hinted = ExecutionSignals(
        action_class=ActionClass.CONSEQUENTIAL, task_risk="high", speed_hint=True, lane="superfast"
    )
    assert decide_execution(base).model_dump() == decide_execution(hinted).model_dump()


def test_signals_are_frozen() -> None:
    """The policy reads its inputs; it never rewrites them mid-decision."""
    signals = ExecutionSignals(action_class=ActionClass.READ_ONLY)
    with pytest.raises((AttributeError, TypeError)):
        signals.action_class = ActionClass.IRREVERSIBLE  # type: ignore[misc]
