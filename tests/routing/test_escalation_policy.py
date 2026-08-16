"""P1-RECOVERY: pure bounded distinct-model escalation policy.

Gates (LANE-BRIEF / ORR-W2-J05 / R6):
- distinct_route_identity — identity is (provider, canonical_model), not display
- retry_not_escalation — typed RETRY does not advance rung_index / generation
- terminal_closed — exhaustion terminals cannot reopen
- same_model_counterfeit — two display names → one canonical model is not a step

Negative mutations these tests must fail under (see rework packet):
- same-model-escalation
- retry-counted-as-escalation
- reviewer-unavailable-passes
- terminal-reopens
- unbounded-rung-loop

Entirely offline: no network, no DB, no filesystem.
"""

from __future__ import annotations

import pytest

from omniagentos.routing.escalation import (
    MAX_LADDER_RUNGS,
    TERMINAL_KINDS,
    DecisionKind,
    EscalationCursor,
    InvalidEscalationLadder,
    RouteIdentity,
    build_ladder,
    decide_escalate,
    decide_retry,
    decide_reviewer_result,
    initial_route,
    is_terminal,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cross_lineage_rungs() -> list[dict[str, object]]:
    return [
        {
            "provider": "openai",
            "display_model": "GPT-5.6-Sol High",
            "canonical_model": "gpt-5.6-sol",
            "name": "sol",
        },
        {
            "provider": "anthropic",
            "display_model": "Claude Opus",
            "canonical_model": "claude-opus",
            "name": "opus",
        },
        {
            "provider": "moonshot",
            "display_model": "Kimi K2",
            "canonical_model": "kimi-k2",
            "name": "kimi",
        },
    ]


def _alias_map() -> dict[str, str]:
    return {
        "GPT-5.6-Sol High": "gpt-5.6-sol",
        "gpt-5.6-sol-display": "gpt-5.6-sol",
        "Claude Opus": "claude-opus",
        "Kimi K2": "kimi-k2",
    }


def _ladder(**kwargs: object):
    return build_ladder(
        _cross_lineage_rungs(),
        aliases=_alias_map(),
        **kwargs,  # type: ignore[arg-type]
    )


def _settled_at(ladder, rung_index: int) -> EscalationCursor:
    visited = tuple(ladder.rungs[i].identity for i in range(rung_index + 1))
    return EscalationCursor(
        policy_hash=ladder.policy_hash,
        rung_index=rung_index,
        generation=rung_index,
        terminal=None,
        visited=visited,
    )


# ---------------------------------------------------------------------------
# Gate: distinct_route_identity + same_model_counterfeit
# ---------------------------------------------------------------------------


class TestDistinctRouteIdentity:
    def test_route_identity_is_provider_plus_canonical_not_display(self) -> None:
        ladder = build_ladder(
            [
                {
                    "provider": "openai",
                    "display_model": "GPT-5.6-Sol High",
                    "canonical_model": "gpt-5.6-sol",
                },
                {
                    "provider": "openai",
                    "display_model": "gpt-5.6-sol-display",
                    "canonical_model": "gpt-5.6-sol",
                },
                {
                    "provider": "anthropic",
                    "display_model": "Claude Opus",
                    "canonical_model": "claude-opus",
                },
            ],
            aliases=_alias_map(),
        )
        start = initial_route(ladder)
        assert start.kind is DecisionKind.INITIAL_ROUTE
        assert start.route is not None
        assert start.route.display_model == "GPT-5.6-Sol High"
        assert start.route.identity == RouteIdentity("openai", "gpt-5.6-sol")

        nxt = decide_escalate(ladder, start.cursor)
        assert nxt.kind is DecisionKind.ESCALATE
        assert nxt.route is not None
        assert nxt.route.canonical_model == "claude-opus"
        assert nxt.route.provider == "anthropic"
        assert any(s.reason == "already_visited_identity" for s in nxt.skips)
        assert RouteIdentity("openai", "gpt-5.6-sol") in nxt.cursor.visited
        assert len([v for v in nxt.cursor.visited if v.canonical_model == "gpt-5.6-sol"]) == 1

    def test_alias_map_collapses_display_names_at_build(self) -> None:
        ladder = build_ladder(
            [
                {"provider": "openai", "display_model": "GPT-5.6-Sol High"},
                {"provider": "openai", "display_model": "gpt-5.6-sol-display"},
            ],
            aliases=_alias_map(),
        )
        assert ladder.rungs[0].canonical_model == "gpt-5.6-sol"
        assert ladder.rungs[1].canonical_model == "gpt-5.6-sol"
        assert ladder.rungs[0].identity == ladder.rungs[1].identity

    def test_provider_diversity_required_even_for_same_canonical_string(self) -> None:
        ladder = build_ladder(
            [
                {"provider": "openai", "display_model": "shared-id"},
                {"provider": "anthropic", "display_model": "shared-id"},
            ]
        )
        start = initial_route(ladder)
        nxt = decide_escalate(ladder, start.cursor)
        assert nxt.kind is DecisionKind.ESCALATE
        assert nxt.route is not None
        assert nxt.route.provider == "anthropic"
        assert nxt.route.canonical_model == "shared-id"


# ---------------------------------------------------------------------------
# Gate: retry_not_escalation
# ---------------------------------------------------------------------------


class TestRetryNotEscalation:
    def test_retry_and_escalate_are_different_typed_outcomes(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        retry = decide_retry(ladder, start.cursor, failure_class="timeout")
        esc = decide_escalate(ladder, start.cursor, failure_class="review_denied")

        assert retry.kind is DecisionKind.RETRY
        assert esc.kind is DecisionKind.ESCALATE
        assert retry.kind is not esc.kind
        assert retry.kind.value == "retry"
        assert esc.kind.value == "escalate"

    def test_ordinary_retry_does_not_advance_rung_or_generation(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        before = start.cursor
        retry = decide_retry(ladder, before, failure_class="429")

        assert retry.kind is DecisionKind.RETRY
        assert retry.route is not None
        assert retry.route.index == before.rung_index
        assert retry.cursor.rung_index == before.rung_index
        assert retry.cursor.generation == before.generation
        assert retry.cursor.visited == before.visited
        assert retry.cursor.terminal is None

    def test_escalation_advances_rung_and_generation(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        esc = decide_escalate(ladder, start.cursor)
        assert esc.kind is DecisionKind.ESCALATE
        assert esc.route is not None
        assert esc.cursor.rung_index == start.cursor.rung_index + 1
        assert esc.cursor.generation == start.cursor.generation + 1


# ---------------------------------------------------------------------------
# Gate: terminal_closed + exhaustion kinds
# ---------------------------------------------------------------------------


class TestTerminalClosed:
    def test_exhaustion_returns_escalation_exhausted(self) -> None:
        ladder = _ladder()
        cursor = _settled_at(ladder, rung_index=len(ladder.rungs) - 1)
        decision = decide_escalate(ladder, cursor)
        assert decision.kind is DecisionKind.ESCALATION_EXHAUSTED
        assert decision.kind.value == "escalation_exhausted"
        assert decision.route is None
        assert is_terminal(decision.cursor)
        assert decision.cursor.terminal is DecisionKind.ESCALATION_EXHAUSTED

    def test_infrastructure_exhausted_when_remaining_rungs_unavailable(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        assert start.route is not None
        available = {start.route.identity}
        decision = decide_escalate(ladder, start.cursor, available=available)
        assert decision.kind is DecisionKind.INFRASTRUCTURE_EXHAUSTED
        assert decision.kind.value == "infrastructure_exhausted"
        assert is_terminal(decision.cursor)
        assert all(s.reason == "infrastructure_unavailable" for s in decision.skips)

    def test_terminal_cannot_reopen_via_escalate(self) -> None:
        ladder = _ladder()
        cursor = _settled_at(ladder, rung_index=len(ladder.rungs) - 1)
        closed = decide_escalate(ladder, cursor)
        assert is_terminal(closed.cursor)

        again = decide_escalate(ladder, closed.cursor)
        assert again.kind is DecisionKind.ESCALATION_EXHAUSTED
        assert again.route is None
        assert is_terminal(again.cursor)
        assert again.cursor.terminal == closed.cursor.terminal
        assert again.cursor.generation == closed.cursor.generation
        assert again.cursor.rung_index == closed.cursor.rung_index

    def test_terminal_cannot_reopen_via_retry(self) -> None:
        ladder = _ladder()
        cursor = _settled_at(ladder, rung_index=len(ladder.rungs) - 1)
        closed = decide_escalate(ladder, cursor)
        retry = decide_retry(ladder, closed.cursor)
        assert retry.kind is DecisionKind.ESCALATION_EXHAUSTED
        assert is_terminal(retry.cursor)
        assert retry.cursor.rung_index == closed.cursor.rung_index
        assert retry.cursor.generation == closed.cursor.generation

    def test_terminal_kinds_are_closed_set(self) -> None:
        assert DecisionKind.ESCALATION_EXHAUSTED in TERMINAL_KINDS
        assert DecisionKind.INFRASTRUCTURE_EXHAUSTED in TERMINAL_KINDS
        assert DecisionKind.RETRY not in TERMINAL_KINDS
        assert DecisionKind.ESCALATE not in TERMINAL_KINDS
        assert DecisionKind.REVIEWER_UNAVAILABLE not in TERMINAL_KINDS
        assert DecisionKind.INITIAL_ROUTE not in TERMINAL_KINDS


# ---------------------------------------------------------------------------
# Gate: reviewer unavailability is never approval
# ---------------------------------------------------------------------------


class TestReviewerUnavailabilityNonVerdict:
    def test_unavailable_false_is_non_verdict_not_approval(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        d = decide_reviewer_result(
            ladder, start.cursor, available=False, verdict="approve"
        )
        assert d.kind is DecisionKind.REVIEWER_UNAVAILABLE
        assert d.kind.value == "reviewer_unavailable"
        assert d.verdict is None
        assert d.is_verdict is False
        assert d.cursor.rung_index == start.cursor.rung_index
        assert d.cursor.generation == start.cursor.generation

    def test_available_true_none_verdict_is_not_approval(self) -> None:
        """Available reviewer with no verdict must not invent approval."""
        ladder = _ladder()
        start = initial_route(ladder)
        d = decide_reviewer_result(ladder, start.cursor, available=True, verdict=None)
        assert d.kind is DecisionKind.REVIEWER_RESULT
        assert d.verdict is None
        assert d.is_verdict is False

    def test_available_true_empty_string_verdict_is_passthrough_not_invented_approve(
        self,
    ) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        d = decide_reviewer_result(ladder, start.cursor, available=True, verdict="")
        assert d.kind is DecisionKind.REVIEWER_RESULT
        assert d.verdict == ""
        # Empty string is not a concrete non-None approval claim.
        assert d.is_verdict is False or d.verdict != "approve"

    def test_available_true_concrete_verdict_passthrough(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        d = decide_reviewer_result(
            ladder, start.cursor, available=True, verdict="reject"
        )
        assert d.kind is DecisionKind.REVIEWER_RESULT
        assert d.verdict == "reject"
        assert d.is_verdict is True
        assert d.cursor.rung_index == start.cursor.rung_index


# ---------------------------------------------------------------------------
# Gate: pure + bounded + fail-closed construction
# ---------------------------------------------------------------------------


class TestPureBoundedPolicy:
    def test_empty_ladder_fail_closed(self) -> None:
        with pytest.raises(InvalidEscalationLadder, match="empty"):
            build_ladder([])

    def test_malformed_rung_fail_closed(self) -> None:
        with pytest.raises(InvalidEscalationLadder):
            build_ladder([{"provider": "", "display_model": "x"}])
        with pytest.raises(InvalidEscalationLadder):
            build_ladder([{"provider": "openai", "display_model": ""}])

    def test_hard_rung_bound_exists(self) -> None:
        assert MAX_LADDER_RUNGS > 0
        too_many = [
            {
                "provider": "p",
                "display_model": f"m{i}",
                "canonical_model": f"c{i}",
            }
            for i in range(MAX_LADDER_RUNGS + 1)
        ]
        with pytest.raises(InvalidEscalationLadder, match="bound"):
            build_ladder(too_many)

    def test_policy_hash_stable_and_mismatch_raises(self) -> None:
        a = _ladder()
        b = build_ladder(
            [{"provider": "x", "display_model": "y", "canonical_model": "z"}]
        )
        start = initial_route(a)
        from omniagentos.routing.escalation import InvalidEscalationCursor

        with pytest.raises(InvalidEscalationCursor):
            decide_escalate(b, start.cursor)

    def test_evaluation_is_pure_no_mutation_of_ladder(self) -> None:
        ladder = _ladder()
        start = initial_route(ladder)
        rungs_before = ladder.rungs
        _ = decide_retry(ladder, start.cursor)
        _ = decide_escalate(ladder, start.cursor)
        assert ladder.rungs is rungs_before
        assert ladder.rungs[0].provider == "openai"


# ---------------------------------------------------------------------------
# Negative-mutation binding tests (requirement-level)
# ---------------------------------------------------------------------------


class TestNegativeMutationRequirements:
    """These assert the REQUIREMENT. A production bug that re-introduces any
    named mutation must turn these red without rewriting the test to match code.
    """

    def test_same_model_escalation_is_rejected(self) -> None:
        """Mutation: alias two display names to one canonical and treat as step."""
        ladder = build_ladder(
            [
                {
                    "provider": "openai",
                    "display_model": "Sol High",
                    "canonical_model": "gpt-5.6-sol",
                },
                {
                    "provider": "openai",
                    "display_model": "Sol Ultra",
                    "canonical_model": "gpt-5.6-sol",
                },
            ]
        )
        start = initial_route(ladder)
        nxt = decide_escalate(ladder, start.cursor)
        assert nxt.kind is DecisionKind.ESCALATION_EXHAUSTED
        assert nxt.route is None
        assert any(s.reason == "already_visited_identity" for s in nxt.skips)

    def test_retry_must_not_be_counted_as_escalation(self) -> None:
        """Mutation: advance rung on ordinary retry."""
        ladder = _ladder()
        cursor = initial_route(ladder).cursor
        for i in range(3):
            d = decide_retry(ladder, cursor, failure_class="timeout")
            assert d.kind is DecisionKind.RETRY, f"retry {i} became {d.kind}"
            assert d.cursor.rung_index == 0
            assert d.cursor.generation == cursor.generation
            cursor = d.cursor
        esc = decide_escalate(ladder, cursor)
        assert esc.kind is DecisionKind.ESCALATE
        assert esc.cursor.rung_index == 1

    def test_reviewer_unavailable_must_not_pass_as_approval(self) -> None:
        """Mutation: convert infrastructure absence to approval."""
        ladder = _ladder()
        cursor = initial_route(ladder).cursor
        d = decide_reviewer_result(
            ladder, cursor, available=False, verdict="approve"
        )
        assert d.kind is DecisionKind.REVIEWER_UNAVAILABLE
        assert d.verdict is None
        assert d.is_verdict is False
        assert d.kind.value != "approve"
        assert "approv" not in d.kind.value

    def test_terminal_must_not_reopen(self) -> None:
        """Mutation: allow another escalation after exhaustion."""
        ladder = _ladder()
        cursor = _settled_at(ladder, rung_index=len(ladder.rungs) - 1)
        closed = decide_escalate(ladder, cursor)
        assert closed.kind is DecisionKind.ESCALATION_EXHAUSTED
        for _ in range(3):
            d = decide_escalate(ladder, closed.cursor)
            assert d.kind is DecisionKind.ESCALATION_EXHAUSTED
            assert d.route is None
            assert is_terminal(d.cursor)

    def test_ladder_evaluation_is_finite(self) -> None:
        """Mutation: remove the finite ladder bound / unbounded loop."""
        ladder = _ladder()
        assert 0 < len(ladder.rungs) <= MAX_LADDER_RUNGS
        cursor = initial_route(ladder).cursor
        steps = 0
        d = None
        while steps < 100:
            steps += 1
            d = decide_escalate(ladder, cursor)
            if is_terminal(d.cursor):
                break
            cursor = d.cursor
        else:
            pytest.fail("escalation walk did not terminate within finite bound")
        assert d is not None
        assert steps <= len(ladder.rungs) + 1
        assert d.kind in (
            DecisionKind.ESCALATION_EXHAUSTED,
            DecisionKind.INFRASTRUCTURE_EXHAUSTED,
        )
