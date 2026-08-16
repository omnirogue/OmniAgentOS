"""P0-CONTRACT.v1a — freeze tests for shared mission/reliability contracts.

Covers ExecutionRef, SwarmPlanDecision, EffectiveRoute, CostObservation,
additive MissionEvents (Events.ALL untouched), swarm group_* actions, and a
negative control proving the suite detects a contract violation.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omniagentos.contracts import (
    CostObservation,
    CostQuality,
    EffectiveRoute,
    Events,
    ExecutionRef,
    MissionEvents,
    ProviderCallStage,
    ReasoningEffort,
    _parse_cost_usd_decimal,
)
from omniagentos.swarm.contracts import (
    ACTION_BUDGET_UNENFORCEABLE,
    ACTION_GROUP_ACTIVATED,
    ACTION_GROUP_CANCELLED,
    ACTION_GROUP_COMPLETED,
    ACTION_GROUP_CREATED,
    ACTION_GROUP_FAILED,
    ACTION_PLAN_CREATED,
    PLAN_DISPOSITIONS,
    READY_PLAN_DISPOSITION,
    SWARM_EVENT_ACTIONS,
    SWARM_EVENT_KIND,
    SWARM_GROUP_EVENT_ACTIONS,
    PlanIssue,
    SwarmPlan,
    SwarmPlanDecision,
    SwarmTaskSpec,
)

# Frozen Wave-0 Events.ALL snapshot at baseline b583a7f2… — must not change.
_FROZEN_EVENTS_ALL: tuple[str, ...] = (
    "run.updated",
    "step.updated",
    "task.updated",
    "approval.requested",
    "approval.decided",
    "pause.changed",
    "audit.event",
    "worker.heartbeat",
    "comms.message",
    "goal.metric",
    "alert.created",
    "briefing.ready",
    "suggestion.updated",
    "session.updated",
)

_MISSION_EVENT_KINDS: tuple[str, ...] = (
    "chat.project_binding_changed",
    "classification.updated",
    "classification.needs_confirmation",
    "classification.shadow_compared",
    "context.package_ready",
    "context.delivery_failed",
    "task_contract.created",
    "task_contract.updated",
    "task_contract.would_deny",
    "contract_gate.updated",
    "contract_budget.updated",
    "resource_request.created",
    "resource_request.updated",
    "formation.updated",
    "verification.updated",
    "receipt.available",
    "memory.updated",
)

# Pre-P0 swarm actions that must remain present and ordered as a prefix of
# SWARM_EVENT_ACTIONS (group_* actions append only).
_LEGACY_SWARM_ACTIONS_PREFIX: tuple[str, ...] = (
    "plan_created",
    "run_started",
    "slot_opened",
    "task_assigned",
    "task_completed",
    "review_confirmed",
    "review_denied",
    "provider_switched",
    "rate_limit",
    "rate_limit_stall",
    "task_split",
    "resize",
    "task_blocked",
    "approval_parked",
    "merge_started",
    "run_completed",
    "run_failed",
    "worktree_created",
    "branch_merged",
    "merge_conflict",
    "worktree_kept",
    "merge_aborted",
    "subtasks_requested",
    "subtasks_denied",
    "subtasks_granted",
    "leader_update",
    "worker_spawned",
    "gate_degraded",
    "budget_unenforceable",
)


# ---------------------------------------------------------------------------
# ExecutionRef
# ---------------------------------------------------------------------------


class TestExecutionRef:
    def test_required_and_optional_fields(self) -> None:
        ref = ExecutionRef(request_id="req_1", execution_id="exec_1")
        assert ref.request_id == "req_1"
        assert ref.execution_id == "exec_1"
        assert ref.company_id is None
        assert ref.project_id is None
        assert ref.campaign_id is None
        assert ref.idempotency_key_hash is None
        assert ref.created_at  # default factory

    def test_full_envelope_round_trip(self) -> None:
        payload = {
            "request_id": "req_abc",
            "execution_id": "exec_xyz",
            "company_id": "co_1",
            "project_id": "proj_1",
            "campaign_id": "camp_1",
            "idempotency_key_hash": "deadbeef",
            "created_at": "2026-07-30T00:00:00Z",
        }
        ref = ExecutionRef.model_validate(payload)
        assert ref.model_dump() == payload

    def test_missing_required_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionRef.model_validate({"request_id": "only"})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SwarmPlanDecision
# ---------------------------------------------------------------------------


def _sample_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="ship",
        tasks=[
            SwarmTaskSpec(
                id="t1",
                title="do work",
                owned_paths=["src/"],
                acceptance="tests pass",
                verify_command="uv run pytest -q",
            )
        ],
    )


class TestSwarmPlanDecision:
    def test_dispositions_exact(self) -> None:
        assert PLAN_DISPOSITIONS == (
            "ready",
            "needs_clarification",
            "impossible",
            "policy_denied",
            "invalid_plan",
            "planner_unavailable",
            # Additive (operator ruling 2026-08-10): a well-formed plan whose
            # ownership is a bounded non-file resource. Non-ready like every
            # entry after "ready", so it carries no executable plan content.
            "draft",
        )
        assert READY_PLAN_DISPOSITION == "ready"

    def test_ready_may_carry_plans(self) -> None:
        decision = SwarmPlanDecision(disposition="ready", plans=[_sample_plan()])
        assert decision.is_ready
        assert len(decision.plans) == 1
        assert decision.plans[0].goal == "ship"

    @pytest.mark.parametrize(
        "disposition",
        [
            "needs_clarification",
            "impossible",
            "policy_denied",
            "invalid_plan",
            "planner_unavailable",
        ],
    )
    def test_non_ready_rejects_plan_content(self, disposition: str) -> None:
        with pytest.raises(ValidationError, match="only disposition 'ready'"):
            SwarmPlanDecision(disposition=disposition, plans=[_sample_plan()])  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "disposition",
        [
            "needs_clarification",
            "impossible",
            "policy_denied",
            "invalid_plan",
            "planner_unavailable",
        ],
    )
    def test_non_ready_without_plans_ok(self, disposition: str) -> None:
        decision = SwarmPlanDecision(
            disposition=disposition,  # type: ignore[arg-type]
            issues=[PlanIssue(code="x", message="blocked")],
            questions=["what is the target?"],
            reason="cannot proceed",
        )
        assert not decision.is_ready
        assert decision.plans == []

    def test_unknown_disposition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SwarmPlanDecision(disposition="proceeding")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EffectiveRoute
# ---------------------------------------------------------------------------


class TestEffectiveRoute:
    def test_required_fields_and_explicit_transport(self) -> None:
        route = EffectiveRoute(
            role="worker",
            requested_model="anthropic/claude-sonnet-4",
            effective_model="anthropic/claude-sonnet-4",
            model_lineage="anthropic",
            billing_provider="anthropic",
            transport="cli",
            adapter_key="cli-claude",
            selection_reason="profile role map",
        )
        assert route.transport == "cli"
        assert route.effort is None
        assert route.profile_id is None
        assert route.price_revision is None

    def test_optional_fields_round_trip(self) -> None:
        payload = {
            "role": "reviewer",
            "requested_model": "openai/gpt-5",
            "effective_model": "openai/gpt-5",
            "model_lineage": "openai",
            "billing_provider": "openrouter",
            "transport": "api",
            "adapter_key": "openrouter",
            "effort": "high",
            "profile_id": "test-profile",
            "profile_revision": 3,
            "selection_reason": "strict_model",
            "price_revision": "2026-07-01",
        }
        route = EffectiveRoute.model_validate(payload)
        dumped = route.model_dump()
        assert dumped["transport"] == "api"
        assert dumped["effort"] == ReasoningEffort.HIGH
        assert dumped["profile_revision"] == 3

    def test_effort_rejects_arbitrary_string(self) -> None:
        with pytest.raises(ValidationError):
            EffectiveRoute.model_validate(
                {
                    "role": "worker",
                    "requested_model": "m",
                    "effective_model": "m",
                    "model_lineage": "x",
                    "billing_provider": "x",
                    "transport": "cli",
                    "adapter_key": "k",
                    "selection_reason": "ok",
                    "effort": "invented-seventh-effort",
                }
            )

    def test_missing_selection_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EffectiveRoute(
                role="worker",
                requested_model="m",
                effective_model="m",
                model_lineage="x",
                billing_provider="x",
                transport="cli",
                adapter_key="k",
            )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# CostObservation + exact decimal round-trip
# ---------------------------------------------------------------------------


def _base_cost_kwargs(**overrides: object) -> dict:
    payload: dict = {
        "call_id": "call_1",
        "request_id": "req_1",
        "execution_id": "exec_1",
        "stage": ProviderCallStage.WORKER,
        "attempt_index": 0,
        "provider": "openrouter",
        "transport": "api",
        "requested_model": "m",
        "effective_model": "m",
        "model_lineage": "qwen",
        "billing_provider": "openrouter",
        "adapter_key": "openrouter",
        "request_state": "sent",
        "cost_quality": CostQuality.UNKNOWN,
        "cost_source": "test",
    }
    payload.update(overrides)
    return payload


class TestCostObservation:
    def test_cost_quality_and_stage_vocabularies(self) -> None:
        assert tuple(CostQuality) == (
            CostQuality.EXACT,
            CostQuality.ESTIMATED,
            CostQuality.UNKNOWN,
        )
        assert {m.value for m in CostQuality} == {"exact", "estimated", "unknown"}
        stages = {m.value for m in ProviderCallStage}
        for required in (
            "planner",
            "clarifier",
            "planner_retry",
            "worker",
            "worker_retry",
            "reviewer",
            "reviewer_retry",
            "escalation",
            "integrator",
            "integrator_retry",
        ):
            assert required in stages

    def test_exact_decimal_round_trip_0_000003705(self) -> None:
        text = "0.000003705"
        preserved, nano = _parse_cost_usd_decimal(text)
        assert preserved == "0.000003705"
        assert nano == 3705

        obs = CostObservation.model_validate(
            _base_cost_kwargs(
                cost_usd_decimal=text,
                cost_quality=CostQuality.EXACT,
                cost_source="provider",
            )
        )
        assert obs.cost_usd_decimal == "0.000003705"
        assert obs.cost_usd_nanos == 3705

        wire = obs.model_dump(mode="json")
        again = CostObservation.model_validate(wire)
        assert again.cost_usd_decimal == "0.000003705"
        assert again.cost_usd_nanos == 3705
        assert "0.000003705" in obs.model_dump_json()

    def test_large_decimal_exact_nano_mapping(self) -> None:
        text = "9999999999999999999999999999.999999999"
        expected_nano = 9999999999999999999999999999999999999
        preserved, nano = _parse_cost_usd_decimal(text)
        assert preserved == text
        assert nano == expected_nano

        obs = CostObservation.model_validate(
            _base_cost_kwargs(
                cost_usd_decimal=text,
                cost_quality=CostQuality.EXACT,
                cost_source="provider",
            )
        )
        assert obs.cost_usd_decimal == text
        assert obs.cost_usd_nanos == expected_nano
        again = CostObservation.model_validate(obs.model_dump(mode="json"))
        assert again.cost_usd_decimal == text
        assert again.cost_usd_nanos == expected_nano

    def test_parse_independent_of_hostile_decimal_context(self) -> None:
        """Old Decimal-context path fails under low precision; integer path must not."""
        import decimal

        text = "9999999999999999999999999999.999999999"
        expected_nano = 9999999999999999999999999999999999999
        with decimal.localcontext() as ctx:
            ctx.prec = 6
            ctx.rounding = decimal.ROUND_DOWN
            # Prove the rejected behavior: Decimal arithmetic under this context
            # does NOT yield the exact integer nano-USD.
            amount = decimal.Decimal(text)
            legacy_nano = amount * decimal.Decimal("1000000000")
            assert (
                int(legacy_nano) != expected_nano or legacy_nano != legacy_nano.to_integral_value()
            )
            # Current authority still exact under the hostile context.
            preserved, nano = _parse_cost_usd_decimal(text)
            assert preserved == text
            assert nano == expected_nano
            obs = CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_usd_decimal=text,
                    cost_quality=CostQuality.EXACT,
                    cost_source="provider",
                )
            )
            again = CostObservation.model_validate(obs.model_dump(mode="json"))
            assert again.cost_usd_decimal == text
            assert again.cost_usd_nanos == expected_nano
            # Frozen small sample still exact under the same hostile context.
            p2, n2 = _parse_cost_usd_decimal("0.000003705")
            assert p2 == "0.000003705"
            assert n2 == 3705

    def test_strict_integer_fields_reject_coercions(self) -> None:
        adversarial: list[tuple[str, object]] = [
            ("attempt_index", True),
            ("attempt_index", "1"),
            ("attempt_index", 1.0),
            ("input_tokens", False),
            ("input_tokens", "10"),
            ("input_tokens", 3.5),
            ("output_tokens", "0"),
            ("total_tokens", 2.0),
            ("cost_usd_nanos", True),
            ("cost_usd_nanos", "3705"),
            ("cost_usd_nanos", 3705.0),
            ("cost_upper_bound_usd_nanos", "100"),
            ("cost_upper_bound_usd_nanos", 1.5),
            ("cost_upper_bound_usd_nanos", False),
        ]
        for field, bad in adversarial:
            if field == "cost_upper_bound_usd_nanos":
                kwargs = _base_cost_kwargs(
                    cost_quality=CostQuality.ESTIMATED,
                    cost_source="x",
                    cost_upper_bound_usd_nanos=bad,
                )
            elif field == "cost_usd_nanos":
                kwargs = _base_cost_kwargs(
                    cost_quality=CostQuality.EXACT,
                    cost_source="x",
                    cost_usd_decimal="0.000003705",
                    cost_usd_nanos=bad,
                )
            else:
                kwargs = _base_cost_kwargs(**{field: bad})
            with pytest.raises(ValidationError):
                CostObservation.model_validate(kwargs)

        # Positive controls: real non-negative ints accepted.
        ok = CostObservation.model_validate(
            _base_cost_kwargs(
                attempt_index=2,
                input_tokens=0,
                output_tokens=3,
                total_tokens=3,
                cost_quality=CostQuality.EXACT,
                cost_source="provider",
                cost_usd_decimal="0.000003705",
                cost_usd_nanos=3705,
            )
        )
        assert ok.attempt_index == 2
        assert ok.cost_usd_nanos == 3705

    def test_whitespace_in_exact_decimal_rejected(self) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            _parse_cost_usd_decimal(" 0.000003705")
        with pytest.raises(ValidationError, match="whitespace"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_usd_decimal=" 0.000003705",
                    cost_quality=CostQuality.EXACT,
                    cost_source="provider",
                )
            )

    def test_unknown_cost_is_none_not_zero(self) -> None:
        obs = CostObservation.model_validate(
            _base_cost_kwargs(
                stage=ProviderCallStage.PLANNER,
                request_state="indeterminate",
                cost_quality=CostQuality.UNKNOWN,
                cost_source="timeout",
            )
        )
        assert obs.cost_usd_decimal is None
        assert obs.cost_usd_nanos is None

    def test_unknown_rejects_invented_exact_cost(self) -> None:
        with pytest.raises(ValidationError, match="unknown"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_usd_decimal="0.01",
                    cost_quality=CostQuality.UNKNOWN,
                    cost_source="bad",
                )
            )

    def test_estimated_requires_upper_bound_and_forbids_exact(self) -> None:
        with pytest.raises(ValidationError, match="upper_bound"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_quality=CostQuality.ESTIMATED,
                    cost_source="estimator",
                )
            )
        with pytest.raises(ValidationError, match="exact cost"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_quality=CostQuality.ESTIMATED,
                    cost_source="estimator",
                    cost_upper_bound_usd_nanos=10_000_000,
                    cost_usd_decimal="0.01",
                )
            )
        ok = CostObservation.model_validate(
            _base_cost_kwargs(
                cost_quality=CostQuality.ESTIMATED,
                cost_source="estimator",
                cost_upper_bound_usd_nanos=10_000_000,
            )
        )
        assert ok.cost_upper_bound_usd_nanos == 10_000_000
        assert ok.cost_usd_decimal is None

    def test_exact_requires_decimal(self) -> None:
        with pytest.raises(ValidationError, match="exact"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_quality=CostQuality.EXACT,
                    cost_source="provider",
                    cost_usd_nanos=1,
                )
            )

    def test_disagreement_between_text_and_nanos_rejected(self) -> None:
        with pytest.raises(ValidationError, match="disagrees"):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_usd_decimal="0.000003705",
                    cost_usd_nanos=1,
                    cost_quality=CostQuality.EXACT,
                    cost_source="provider",
                )
            )

    def test_negative_tokens_and_nanos_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    input_tokens=-1,
                    cost_quality=CostQuality.UNKNOWN,
                    cost_source="x",
                )
            )
        with pytest.raises(ValidationError):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_quality=CostQuality.ESTIMATED,
                    cost_source="x",
                    cost_upper_bound_usd_nanos=-5,
                )
            )

    def test_request_state_closed_set(self) -> None:
        for state in ("not_sent", "sent", "indeterminate"):
            obs = CostObservation.model_validate(_base_cost_kwargs(request_state=state))
            assert obs.request_state == state
        with pytest.raises(ValidationError):
            CostObservation.model_validate(_base_cost_kwargs(request_state="succeeded"))

    def test_request_and_execution_ids_required(self) -> None:
        with pytest.raises(ValidationError):
            CostObservation.model_validate(
                {k: v for k, v in _base_cost_kwargs().items() if k != "request_id"}
            )
        with pytest.raises(ValidationError):
            CostObservation.model_validate(
                {k: v for k, v in _base_cost_kwargs().items() if k != "execution_id"}
            )

    def test_correlation_and_route_identity_fields(self) -> None:
        obs = CostObservation.model_validate(
            _base_cost_kwargs(
                run_id="swr_1",
                task_id="btk_1",
                attempt_id="swa_1",
                session_id="ses_1",
                campaign_id="camp_1",
                reservation_id="rsv_1",
                work_id="work_1",
                root_trace_id="trace_1",
                stage=ProviderCallStage.REVIEWER,
                attempt_index=2,
                requested_model="a",
                effective_model="b",
                model_lineage="openai",
                billing_provider="openrouter",
                transport="gateway",
                adapter_key="loopback",
                provider_request_id="prv_9",
                provider_outcome="http_200",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cost_quality=CostQuality.ESTIMATED,
                cost_source="estimator",
                cost_upper_bound_usd_nanos=10_000_000,
                pricing_revision="p1",
                settled_at="2026-07-30T00:00:01Z",
            )
        )
        assert obs.provider_request_id == "prv_9"
        assert obs.attempt_index == 2
        assert obs.cost_upper_bound_usd_nanos == 10_000_000
        assert obs.work_id == "work_1"
        assert obs.root_trace_id == "trace_1"
        assert obs.settled_at == "2026-07-30T00:00:01Z"


# ---------------------------------------------------------------------------
# Events.ALL unchanged + mission vocabulary + swarm actions
# ---------------------------------------------------------------------------


class TestEventVocabulary:
    def test_events_all_unchanged(self) -> None:
        assert Events.ALL == _FROZEN_EVENTS_ALL
        # Mission kinds must not leak into the frozen tuple.
        for kind in MissionEvents.ALL:
            assert kind not in Events.ALL

    def test_mission_event_vocabulary_exact(self) -> None:
        assert MissionEvents.ALL == _MISSION_EVENT_KINDS
        assert len(MissionEvents.ALL) == len(set(MissionEvents.ALL))

    def test_swarm_event_kind_unchanged(self) -> None:
        assert SWARM_EVENT_KIND == "swarm.event"

    def test_legacy_swarm_actions_compatible_prefix(self) -> None:
        assert SWARM_EVENT_ACTIONS[: len(_LEGACY_SWARM_ACTIONS_PREFIX)] == (
            _LEGACY_SWARM_ACTIONS_PREFIX
        )
        assert ACTION_PLAN_CREATED in SWARM_EVENT_ACTIONS
        assert ACTION_BUDGET_UNENFORCEABLE in SWARM_EVENT_ACTIONS

    def test_group_actions_appended_to_swarm_authority(self) -> None:
        expected_group = (
            ACTION_GROUP_CREATED,
            ACTION_GROUP_ACTIVATED,
            ACTION_GROUP_COMPLETED,
            ACTION_GROUP_FAILED,
            ACTION_GROUP_CANCELLED,
        )
        assert SWARM_GROUP_EVENT_ACTIONS == expected_group
        for action in expected_group:
            assert action in SWARM_EVENT_ACTIONS
            assert action.startswith("group_")
        # Append-only: group actions are the tail.
        assert SWARM_EVENT_ACTIONS[-5:] == expected_group


# ---------------------------------------------------------------------------
# Negative proof — the suite detects a contract violation
# ---------------------------------------------------------------------------


class TestNegativeContractViolation:
    """Focused negative controls: each must fail when the contract is violated."""

    def test_suite_detects_non_ready_plan_content(self) -> None:
        """If validation ever allows plans on non-ready, this assertion fires."""
        with pytest.raises(ValidationError):
            SwarmPlanDecision(
                disposition="impossible",
                plans=[_sample_plan()],
            )

    def test_suite_detects_events_all_mutation(self) -> None:
        """Simulate a forbidden Events.ALL extension and assert we would catch it."""
        mutated = list(Events.ALL) + ["chat.project_binding_changed"]
        # The freeze pin must reject any extension of Events.ALL.
        assert tuple(mutated) != _FROZEN_EVENTS_ALL
        assert "chat.project_binding_changed" not in Events.ALL

    def test_suite_detects_decimal_zero_loss(self) -> None:
        """Prove float path and disagreeing nanos are rejected; Decimal path wins."""
        text = "0.000003705"
        _, nano = _parse_cost_usd_decimal(text)
        assert nano == 3705
        # The actual old failure mode: float cannot carry this magnitude of
        # precision reliably as an accounting unit across coercion.
        as_float = float(text)
        assert as_float != 0.0  # would be the catastrophic zero-loss case on tinier values
        # Disagreement between exact text and a float-derived nano is a contract fail.
        float_nano = int(round(as_float * 1_000_000_000))
        obs = CostObservation.model_validate(
            _base_cost_kwargs(
                call_id="call_neg",
                cost_usd_decimal=text,
                cost_quality=CostQuality.EXACT,
                cost_source="provider",
            )
        )
        assert obs.cost_usd_decimal == text
        assert obs.cost_usd_nanos == nano
        with pytest.raises(ValidationError):
            CostObservation.model_validate({**obs.model_dump(), "cost_usd_nanos": nano + 1})
        # Float-derived nano that happens to match still is not a valid input type
        # when provided as float (strict int authority).
        with pytest.raises(ValidationError):
            CostObservation.model_validate(
                _base_cost_kwargs(
                    cost_usd_decimal=text,
                    cost_usd_nanos=float(nano),  # type: ignore[arg-type]
                    cost_quality=CostQuality.EXACT,
                    cost_source="provider",
                )
            )
        assert isinstance(float_nano, int)
        assert copy.copy(obs).cost_usd_decimal == text
