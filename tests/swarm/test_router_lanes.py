"""SwarmRouter lanes (R1.1/R1.2/R1.5) — lane floors, category pins, overflow.

Covers the three routing policies and the review-driven fixes:
- Lane floors (R1.1): quality floors restricting candidates by tier/risk, with
  model-key normalization so a claude fallback satisfies a claude floor, and
  typo'd/absent floor entries dropped instead of stranding the task in a
  permanent requeue.
- Category pins (R1.2): discipline-to-model pins that RESTRICT the candidate set
  (a pinned lower-ranked model wins; a pinned cooling model requeues, never
  substitutes).
- Pressure overflow (R1.5): extend complex candidates when the floor is under
  pressure — never for risky classes, with the overflow provider DERIVED from
  the model's lineage.
- Tier-scoped effort overrides (finding #4): xhigh on complex, tier default
  elsewhere.

Every router here is HERMETIC: the fixture injects lane_floors / category_pins /
overflow / effort maps / model lineages, so no test reads the real
configs/swarm.yaml. Mechanical scores are dominated by ``codingScore`` (all
agents share latency/tooluse/cost), so the ranking order in this fixture is
codex(0.95) > claude-opus(0.90) > grok(0.85) > claude-sonnet(0.75) > kimi(0.70).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from omniagentos.swarm.router import SwarmRouter
from tests.swarm.scheduler_fakes import RecordingEmitter

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _agent(
    agent_id: str,
    provider: str,
    model: str,
    *,
    coding: float = 0.8,
    tooluse: float = 0.8,
    cost: float = 0.5,
    tier: str = "architectural",
    effort: str = "xhigh",
    role: str = "coder",
    available: bool = True,
    latency: int = 1000,
) -> dict[str, Any]:
    return {
        "id": agent_id,
        "provider": provider,
        "model": model,
        "role": role,
        "capabilityTier": tier,
        "maxReasoning": effort,
        "available": available,
        "warmLatencyMs": latency,
        "codingScore": coding,
        "toolUseScore": tooluse,
        "costScore": cost,
    }


# Distinct provider per model so decisions are unambiguous. codingScore sets the
# rank order (ultrabuild wQ=0.40 dominates the identical latency/tooluse/cost):
# codex > claude-opus > grok > claude-sonnet > kimi.
RANKINGS_MULTI = {
    "sol-coder": _agent("sol-coder", "codex", "gpt-5.6-sol", coding=0.95),
    "opus-coder": _agent("opus-coder", "claude", "claude-opus-5", coding=0.90),
    "grok-coder": _agent("grok-coder", "grok", "grok-4.5", coding=0.85),
    "sonnet-coder": _agent("sonnet-coder", "claude", "claude-sonnet-5", coding=0.75),
    "kimi-coder": _agent("kimi-coder", "kimi", "kimi-k3", coding=0.70),
}

# Injected model -> lineage index (finding #3): overflow candidates derive their
# executing provider from this, exactly like ranked candidates use the digest
# lineage. lineage_providers is {} in the fixture so provider == lineage.
MODEL_LINEAGES = {
    "gpt-5.6-sol": "codex",
    "claude-opus-5": "claude",
    "claude-sonnet-5": "claude",
    "grok-4.5": "grok",
    "kimi-k3": "kimi",
    "gemini-3.1-pro": "gemini",
    "gemini-3.6-flash": "gemini",
    "gemini-3.5-flash-lite": "gemini",
}

# Tier-default effort map, injected so no test reads the real swarm.yaml.
EFFORT_BY_TIER = {"simple": "low", "standard": "medium", "complex": "high"}


class FakeReservation(SimpleNamespace):
    pass


def _reservation(provider: str, n: int = 1) -> FakeReservation:
    return FakeReservation(
        id=f"rsv_{provider}_{n}",
        provider=provider,
        account=SimpleNamespace(account_id=f"acct_{provider}_{n}"),
        expires_at="2026-07-23T12:02:00Z",
    )


class FakeRouterLimits:
    def __init__(self) -> None:
        self.enabled: dict[str, int] = {}
        self.cooling: set[str] = set()
        self.pressure: dict[str, float] = {}
        self.reservations: dict[str, Any] = {}
        self.reserve_calls: list[str] = []

    def enabled_account_count(self, provider: str) -> int:
        return self.enabled.get(provider, 0)

    def all_cooling(self, provider: str) -> bool:
        return provider in self.cooling

    def provider_pressure(self, provider: str) -> float:
        return self.pressure.get(provider, 0.0)

    def reserve_account(self, provider: str) -> Any | None:
        self.reserve_calls.append(provider)
        return self.reservations.get(provider)


def make_router(
    *,
    limits: FakeRouterLimits | None = None,
    rankings: dict[str, dict[str, Any]] | None = None,
    lane_floors: dict[str, list[str]] | None = None,
    category_pins: dict[str, str] | None = None,
    semantic_pins: bool | None = None,
    category_classifier: Any | None = None,
    overflow: tuple[dict[str, list[str]], float] | None = None,
    extra_candidates: list[dict[str, Any]] | None = None,
    effort_by_tier: dict[str, str] | None = None,
    effort_overrides: dict[str, str] | None = None,
    effort_overrides_by_tier: dict[str, dict[str, str]] | None = None,
    model_lineages: dict[str, str] | None = None,
    **overrides: Any,
) -> tuple[SwarmRouter, FakeRouterLimits, RecordingEmitter]:
    """A fully hermetic router: EVERY config-derived map is injected, so the
    real configs/swarm.yaml is never read (config stays None but is never
    consulted because no `*_map(config)` default branch is reached)."""
    limits = limits or FakeRouterLimits()
    emitter = RecordingEmitter()
    router = SwarmRouter(
        emitter=emitter,
        limits=limits,
        lineage_providers={},
        rankings_loader=lambda: dict(rankings if rankings is not None else RANKINGS_MULTI),
        digest_loader=lambda: None,
        samples_loader=lambda: [],
        attempts_loader=lambda task_id: [],
        now=lambda: NOW,
        lane_floors=lane_floors or {},
        category_pins=category_pins or {},
        # semantic_pins is injected (default False) so semantic_pins_enabled(config)
        # is never reached — the real configs/swarm.yaml is never read.
        semantic_pins=semantic_pins if semantic_pins is not None else False,
        category_classifier=category_classifier,
        overflow=overflow or ({}, 1.0),
        extra_candidates=extra_candidates if extra_candidates is not None else [],
        effort_by_tier=effort_by_tier if effort_by_tier is not None else dict(EFFORT_BY_TIER),
        effort_overrides=effort_overrides if effort_overrides is not None else {},
        effort_overrides_by_tier=(
            effort_overrides_by_tier if effort_overrides_by_tier is not None else {}
        ),
        model_lineages=model_lineages if model_lineages is not None else dict(MODEL_LINEAGES),
        **overrides,
    )
    return router, limits, emitter


def _task(
    swarm_json: dict[str, Any] | None = None,
    task_id: str = "task1",
    discipline: str | None = None,
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    task = {
        "id": task_id,
        "swarm_run_id": "swr1",
        "swarm_json": json.dumps(swarm_json or {}),
        "title": title,
        "description": description,
    }
    if discipline is not None:
        task["discipline"] = discipline
    return task


def _all_enabled(limits: FakeRouterLimits, *providers: str) -> None:
    for provider in providers:
        limits.enabled[provider] = 1
        limits.reservations[provider] = _reservation(provider)


# ============================================================================
# LANE FLOORS (R1.1)
# ============================================================================


class TestLaneFloorsComplexTier:
    def test_complex_tier_restricted_to_floor_list(self) -> None:
        """complex restricts candidates to the floor even though the same models
        that rank highest happen to be in it; grok (higher than sonnet/kimi) is
        excluded because it is not on the floor."""
        lane_floors = {"complex": ["claude-opus-5"]}  # exclude the #1 (sol)
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "complex")
        assert decision is not None
        # sol (codex) ranks highest but is NOT on the floor → claude-opus wins.
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_complex_tier_no_floor_keeps_ranking(self) -> None:
        """Absent floor → ranking decides (codex/gpt-5.6-sol is #1)."""
        router, limits, _ = make_router(lane_floors={})
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "codex"

    def test_complex_all_floor_cooling_signals_requeue(self) -> None:
        """All floor providers cooling → None (requeue), never downgrade to a
        healthy non-floor model."""
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(lane_floors=lane_floors)
        limits.enabled = {"codex": 1, "claude": 1, "grok": 1}
        limits.cooling = {"codex", "claude"}  # both floor providers cooling
        limits.reservations = {"grok": _reservation("grok")}  # grok healthy
        decision = router.route(_task(), "complex")
        assert decision is None

    def test_standard_tier_unaffected_by_complex_floor(self) -> None:
        """standard risk_class none is unaffected by the complex floor."""
        lane_floors = {"complex": ["claude-opus-5"]}
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude", "grok")
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "codex"  # ranking, no floor at standard

    def test_typod_complex_floor_falls_back_to_no_restriction(self) -> None:
        """A complex floor whose every entry is a typo (matches no candidate)
        is treated as no restriction rather than a silent permanent requeue."""
        lane_floors = {"complex": ["gpt-5.6-solll", "claude-opuss-4.8"]}
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "codex"  # ranking wins; floor dropped


class TestHighRiskFloor:
    def test_high_risk_floor_beats_complex_floor(self) -> None:
        """high_risk takes precedence over the complex floor when both apply."""
        lane_floors = {
            "complex": ["gpt-5.6-sol", "claude-opus-5"],
            "high_risk": ["claude-opus-5"],
        }
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude")
        decision = router.route(_task({"risk_class": "destructive"}), "complex")
        assert decision is not None
        # complex floor would allow sol (codex); high_risk floor restricts to opus.
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_high_risk_empty_rankings_routes_to_claude_fallback(self) -> None:
        """No claude coder in the rankings → the risk pin's built-in claude
        fallback ('opus') satisfies the 'claude-opus-5' floor via
        normalization, so the route lands on claude (NOT None)."""
        lane_floors = {"high_risk": ["claude-opus-5"]}
        router, limits, _ = make_router(
            rankings={},  # no rankings at all → built-in fallback candidate
            lane_floors=lane_floors,
        )
        limits.enabled = {"claude": 1}
        limits.reservations = {"claude": _reservation("claude")}
        decision = router.route(_task({"risk_class": "deploy"}), "complex")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.model == "opus"  # the fallback key, normalized for floor

    def test_high_risk_typod_floor_restricts_to_claude_lineage(self) -> None:
        """A high_risk floor whose entries are all typos falls back to
        claude-lineage candidates (risky work must never downgrade to non-claude
        and must never stall purely on a typo)."""
        lane_floors = {"high_risk": ["claude-opuss-4.8"]}  # typo
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude")
        decision = router.route(_task({"risk_class": "destructive"}), "standard")
        assert decision is not None
        # Only claude candidates survive the risk pin; opus outranks sonnet.
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_high_risk_all_floor_cooling_signals_requeue(self) -> None:
        lane_floors = {"high_risk": ["claude-opus-5"]}
        router, limits, _ = make_router(lane_floors=lane_floors)
        limits.enabled = {"claude": 1}
        limits.cooling = {"claude"}
        decision = router.route(_task({"risk_class": "deploy"}), "standard")
        assert decision is None


# ============================================================================
# CATEGORY PINS (R1.2)
# ============================================================================


class TestCategoryPins:
    def test_pin_restricts_and_lowest_ranked_model_wins(self) -> None:
        """A pin RESTRICTS the candidate set: pinning the LOWEST-ranked healthy
        model (kimi-k3) makes it win over every higher-ranked model."""
        category_pins = {"web_research": "kimi-k3"}
        router, limits, _ = make_router(category_pins=category_pins)
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task({"risk_class": "none"}, discipline="web_research"), "standard"
        )
        assert decision is not None
        assert decision.provider == "kimi"
        assert decision.model == "kimi-k3"

    def test_pinned_but_cooling_signals_requeue(self) -> None:
        """Pinned model cooling → requeue, NOT substitution with the next
        candidate (the whole point of finding #1's restrict semantics)."""
        category_pins = {"adversarial_review": "grok-4.5"}
        router, limits, _ = make_router(category_pins=category_pins)
        limits.enabled = {"grok": 1, "codex": 1}
        limits.cooling = {"grok"}
        limits.reservations = {"codex": _reservation("codex")}
        decision = router.route(
            _task({"risk_class": "none"}, discipline="adversarial_review"), "standard"
        )
        assert decision is None

    def test_no_matching_pin_uses_ranking(self) -> None:
        category_pins = {"adversarial_review": "grok-4.5"}
        router, limits, _ = make_router(category_pins=category_pins)
        _all_enabled(limits, "codex", "grok")
        decision = router.route(
            _task({"risk_class": "none"}, discipline="unknown_discipline"), "standard"
        )
        assert decision is not None
        assert decision.provider == "codex"

    def test_pin_does_not_override_high_risk_floor(self) -> None:
        """A pin to a non-floor model must NOT override the high_risk floor."""
        lane_floors = {"high_risk": ["claude-opus-5"]}
        category_pins = {"web_research": "grok-4.5"}
        router, limits, _ = make_router(lane_floors=lane_floors, category_pins=category_pins)
        _all_enabled(limits, "grok", "codex", "claude")
        decision = router.route(
            _task({"risk_class": "destructive"}, discipline="web_research"),
            "complex",
        )
        assert decision is not None
        # grok pin is ignored; the high_risk floor decides.
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_cooling_explicit_pin_exempt_from_overflow_requeues(self) -> None:
        """F1 (BLOCKER): overflow runs AFTER the pin restriction, so a cooling
        EXPLICIT pin must be EXEMPT from overflow — otherwise a substitute
        (kimi-k3) gets appended and routes instead of requeueing. Pin semantics
        are absolute: pinned+unavailable = requeue, never substitute. Exercised at
        COMPLEX tier where overflow can actually fire."""
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        category_pins = {"science_engineering": "gpt-5.6-sol"}
        router, limits, _ = make_router(
            lane_floors=lane_floors,
            category_pins=category_pins,
            overflow=({"complex": ["kimi-k3"]}, 0.75),
        )
        limits.enabled = {"codex": 1, "kimi": 1}
        limits.cooling = {"codex"}  # the pinned model's provider is cooling
        limits.pressure = {"codex": 0.99, "kimi": 0.0}  # floor pressured → overflow armed
        limits.reservations = {"kimi": _reservation("kimi")}
        decision = router.route(_task(discipline="science_engineering"), "complex")
        # Without the exemption this routed kimi-k3; with it, the cooling pin
        # requeues.
        assert decision is None


# ============================================================================
# SEMANTIC CATEGORY PINS (PKG-SEMANTIC-PINS)
# ============================================================================


def _classifier(category: str | None, score: float = 0.9):
    """A hermetic classifier fake: never imports the real semantic_router. A
    ``None`` category models a below-threshold / no-route verdict (classify_
    category already applies the threshold, so the router sees None)."""
    return lambda _text: None if category is None else (category, score)


class TestSemanticCategoryPins:
    def test_semantic_pin_applies_when_field_absent_and_flag_on(self) -> None:
        """No explicit discipline + flag on + a research/adversarial-shaped text:
        the semantic verdict names a pin category → RESTRICT to that model (even
        though grok is not the highest-ranked candidate)."""
        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_classifier("adversarial_review"),
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(description="tear this design apart and find the flaws"), "standard"
        )
        assert decision is not None
        assert decision.provider == "grok"
        assert decision.model == "grok-4.5"

    def test_explicit_field_wins_over_contradicting_semantic(self) -> None:
        """An explicit discipline pin wins outright; the semantic verdict (a
        DIFFERENT category, mapped to a different model) is never consulted."""
        router, limits, _ = make_router(
            category_pins={"web_research": "kimi-k3", "adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_classifier("adversarial_review", 0.99),
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(
                discipline="web_research",
                description="tear this design apart",  # semantically adversarial
            ),
            "standard",
        )
        assert decision is not None
        # Explicit web_research → kimi-k3; the contradicting semantic verdict loses.
        assert decision.provider == "kimi"
        assert decision.model == "kimi-k3"

    def test_semantic_pin_respects_cooling_requeue(self) -> None:
        """A semantic pin takes the SAME restrict path: a pinned-but-cooling model
        requeues (None), never substitutes with a healthy non-pinned candidate."""
        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_classifier("adversarial_review"),
        )
        limits.enabled = {"grok": 1, "codex": 1}
        limits.cooling = {"grok"}
        limits.reservations = {"codex": _reservation("codex")}
        decision = router.route(_task(description="red-team this proposal"), "standard")
        assert decision is None

    def test_semantic_pin_does_not_override_high_risk_floor(self) -> None:
        """A semantic pin to a non-floor model must NOT override the high_risk
        floor (floors run first; the pin restricts to a model absent from the
        floored set → candidates unchanged)."""
        router, limits, _ = make_router(
            lane_floors={"high_risk": ["claude-opus-5"]},
            category_pins={"web_research": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_classifier("web_research"),
        )
        _all_enabled(limits, "grok", "codex", "claude")
        decision = router.route(
            _task(
                {"risk_class": "destructive"},
                description="research what changed this month",
            ),
            "complex",
        )
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_populated_unmapped_discipline_suppresses_semantic(self) -> None:
        """F2 (MAJOR): a NON-EMPTY explicit discipline that maps to no pin (e.g.
        "backend") must suppress the semantic classifier entirely — prose must
        never override an explicit field. The classifier is never called and the
        plain ranking decides."""
        calls: list[str] = []

        def _spy(text: str) -> tuple[str, float]:
            calls.append(text)
            return ("adversarial_review", 0.99)

        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_spy,
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(discipline="backend", description="tear this design apart"),
            "standard",
        )
        assert decision is not None
        assert decision.provider == "codex"  # ranking; no pin
        assert calls == []  # explicit field suppressed classification

    def test_cooling_semantic_pin_exempt_from_overflow_requeues(self) -> None:
        """F1 (BLOCKER), semantic path: a cooling SEMANTIC pin must be exempt from
        overflow exactly like an explicit one — requeue, never substitute. COMPLEX
        tier so overflow can fire."""
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        category_pins = {"science_engineering": "gpt-5.6-sol"}
        router, limits, _ = make_router(
            lane_floors=lane_floors,
            category_pins=category_pins,
            semantic_pins=True,
            category_classifier=_classifier("science_engineering"),
            overflow=({"complex": ["kimi-k3"]}, 0.75),
        )
        limits.enabled = {"codex": 1, "kimi": 1}
        limits.cooling = {"codex"}
        limits.pressure = {"codex": 0.99, "kimi": 0.0}
        limits.reservations = {"kimi": _reservation("kimi")}
        decision = router.route(
            _task(description="work through the mathematics of this algorithm"),
            "complex",
        )
        assert decision is None

    def test_semantic_category_not_in_pins_map_no_pin(self) -> None:
        """A confident verdict for a category with NO configured pin applies no
        pin — the plain ranking (codex) decides."""
        router, limits, _ = make_router(
            category_pins={"web_research": "grok-4.5"},  # science_engineering absent
            semantic_pins=True,
            category_classifier=_classifier("science_engineering"),
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(description="work through the mathematics of this algorithm"),
            "standard",
        )
        assert decision is not None
        assert decision.provider == "codex"
        assert decision.model != "grok-4.5"

    def test_flag_off_is_byte_identical_and_classifier_never_called(self) -> None:
        """semantic_pins off → the classifier is NEVER consulted and the route is
        byte-identical to the pre-semantic ranking (codex)."""
        calls: list[str] = []

        def _spy(text: str) -> tuple[str, float]:
            calls.append(text)
            return ("adversarial_review", 0.99)

        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=False,
            category_classifier=_spy,
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(description="tear this design apart"), "standard")
        assert decision is not None
        assert decision.provider == "codex"  # ranking, no pin
        assert calls == []  # flag off short-circuits before classifying

    def test_classifier_none_is_byte_identical(self) -> None:
        """A None verdict (no route / below threshold) leaves candidates
        untouched — regression-identical to the explicit-only ranking."""
        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_classifier(None),
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(description="something ambiguous and unclassifiable"), "standard"
        )
        assert decision is not None
        assert decision.provider == "codex"

    def test_classifier_exception_is_byte_identical(self) -> None:
        """A classifier that raises must never break the route — candidates are
        untouched, the ranking decides."""

        def _boom(_text: str) -> tuple[str, float]:
            raise RuntimeError("classifier exploded mid-route")

        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_boom,
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(description="tear this design apart"), "standard")
        assert decision is not None
        assert decision.provider == "codex"

    def test_empty_text_never_classifies(self) -> None:
        """A task with no title/description text does not classify (nothing to
        embed) — the classifier is never called and ranking decides."""
        calls: list[str] = []

        def _spy(text: str) -> tuple[str, float]:
            calls.append(text)
            return ("adversarial_review", 0.99)

        router, limits, _ = make_router(
            category_pins={"adversarial_review": "grok-4.5"},
            semantic_pins=True,
            category_classifier=_spy,
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "standard")  # no title/description
        assert decision is not None
        assert decision.provider == "codex"
        assert calls == []

    def test_semantic_pin_lowest_ranked_model_wins(self) -> None:
        """Proof the semantic pin truly RESTRICTS: pinning the lowest-ranked
        healthy model (kimi) makes it win over every higher-ranked candidate."""
        router, limits, _ = make_router(
            category_pins={"web_research": "kimi-k3"},
            semantic_pins=True,
            category_classifier=_classifier("web_research"),
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(
            _task(title="look up the latest news about the release"), "standard"
        )
        assert decision is not None
        assert decision.provider == "kimi"
        assert decision.model == "kimi-k3"


# ============================================================================
# PRESSURE OVERFLOW (R1.5)
# ============================================================================


class TestPressureOverflow:
    def test_overflow_fires_when_all_floor_pressured(self) -> None:
        """complex + risk none + every floor provider ≥ threshold → overflow
        model becomes eligible and (being unpressured) wins."""
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(
            lane_floors=lane_floors, overflow=({"complex": ["kimi-k3"]}, 0.75)
        )
        limits.enabled = {"codex": 1, "claude": 1, "kimi": 1}
        limits.pressure = {"codex": 0.80, "claude": 0.75, "kimi": 0.0}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
            "kimi": _reservation("kimi"),
        }
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "kimi"
        assert decision.model == "kimi-k3"

    def test_overflow_treats_unreadable_floor_pressure_as_pressured(self) -> None:
        """Unreadable floor pressure must not free-look as healthy / under threshold.

        Overflow fires only when EVERY floor provider is at/above threshold.
        A provider whose pressure ledger cannot be read is not "measured low" —
        it must count as pressured so complex overflow still extends.

        Counterfeit: ``if pressure is None: return False`` (or ``pressure or 0.0``)
        free-looks unreadable as below threshold, suppresses overflow, and keeps
        the run on an unmeasured floor ledger.
        """

        class _FloorPressureRaises(FakeRouterLimits):
            def provider_pressure(self, provider: str) -> float:
                # One floor provider (codex) is unreadable; the other floor
                # (claude) is measured at/above threshold. Overflow eligibility
                # requires *all* floor providers pressured — unreadable must
                # participate as pressured, not as free healthy low.
                if provider == "codex":
                    raise RuntimeError("codex pressure unreadable")
                return super().provider_pressure(provider)

        limits = _FloorPressureRaises()
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(
            limits=limits,
            lane_floors=lane_floors,
            overflow=({"complex": ["kimi-k3"]}, 0.75),
        )
        limits.enabled = {"codex": 1, "claude": 1, "kimi": 1}
        limits.pressure = {"claude": 0.80, "kimi": 0.0}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
            "kimi": _reservation("kimi"),
        }
        decision = router.route(_task(), "complex")
        assert decision is not None
        # Overflow must fire (unreadable floor counts as pressured) and the
        # healthy overflow model wins. Free-looking unreadable as under
        # threshold would suppress overflow and leave only the measured floor.
        assert decision.provider == "kimi"
        assert decision.model == "kimi-k3"

    def test_overflow_provider_derived_from_lineage(self) -> None:
        """The overflow candidate's provider is DERIVED from the model's lineage
        (finding #3), not the hardcoded 'kimi'. A gemini overflow entry routes
        to the gemini provider."""
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(
            lane_floors=lane_floors, overflow=({"complex": ["gemini-3.1-pro"]}, 0.75)
        )
        limits.enabled = {"codex": 1, "claude": 1, "gemini": 1}
        limits.pressure = {"codex": 0.80, "claude": 0.80, "gemini": 0.0}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
            "gemini": _reservation("gemini"),
        }
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "gemini"
        assert decision.model == "gemini-3.1-pro"

    def test_overflow_not_eligible_below_threshold(self) -> None:
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(
            lane_floors=lane_floors, overflow=({"complex": ["kimi-k3"]}, 0.75)
        )
        limits.enabled = {"codex": 1, "claude": 1, "kimi": 1}
        # Both floor providers below the 0.75 threshold (equal, so the higher
        # base score — sol — stays on top after the identical pressure penalty).
        limits.pressure = {"codex": 0.60, "claude": 0.60, "kimi": 0.0}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
            "kimi": _reservation("kimi"),
        }
        decision = router.route(_task(), "complex")
        assert decision is not None
        # Pressure below threshold → no overflow injected → the floor decides,
        # and no kimi-k3 candidate exists.
        assert decision.provider == "codex"
        assert decision.model != "kimi-k3"

    def test_overflow_never_fires_for_risky_class_at_complex(self) -> None:
        """A destructive+complex task must NOT get an overflow model appended —
        even though the tier is complex and the floor is pressured — because the
        risk pin restricts to claude and a non-claude overflow is a poison route
        (finding #2). The old test used STANDARD tier (false coverage)."""
        lane_floors = {
            "complex": ["gpt-5.6-sol", "claude-opus-5"],
            "high_risk": ["claude-opus-5"],
        }
        router, limits, _ = make_router(
            lane_floors=lane_floors, overflow=({"complex": ["kimi-k3"]}, 0.75)
        )
        limits.enabled = {"claude": 1, "kimi": 1}
        # claude (the only floor provider after the risk pin) is fully pressured;
        # kimi is healthy — but overflow must still not fire for a risky class.
        limits.pressure = {"claude": 0.99, "kimi": 0.0}
        limits.reservations = {
            "claude": _reservation("claude"),
            "kimi": _reservation("kimi"),
        }
        decision = router.route(_task({"risk_class": "destructive"}), "complex")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.model == "claude-opus-5"

    def test_overflow_never_fires_at_standard_tier(self) -> None:
        lane_floors = {"complex": ["gpt-5.6-sol", "claude-opus-5"]}
        router, limits, _ = make_router(
            lane_floors=lane_floors, overflow=({"complex": ["kimi-k3"]}, 0.75)
        )
        # Overflow is complex-only: at simple/standard the complex floor does not
        # apply and no overflow is injected, so the plain ranking (sol) decides.
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        for tier in ("simple", "standard"):
            decision = router.route(_task(), tier)
            assert decision is not None
            assert decision.provider == "codex", tier
            assert decision.model != "kimi-k3", tier


# ============================================================================
# TIER-SCOPED EFFORT OVERRIDES (finding #4)
# ============================================================================


class TestTierScopedEffortOverrides:
    def test_complex_gets_xhigh_standard_gets_tier_default(self) -> None:
        """effort_overrides_by_tier bumps gpt-5.6-sol to xhigh ONLY at complex;
        a standard route to the same model keeps the tier default (medium)."""
        router, limits, _ = make_router(
            effort_overrides={},  # flat map empty → no any-tier bump
            effort_overrides_by_tier={"complex": {"gpt-5.6-sol": "xhigh"}},
        )
        limits.enabled = {"codex": 1}
        limits.reservations = {"codex": _reservation("codex")}

        complex_decision = router.route(_task({"complexity": "complex"}), "complex")
        assert complex_decision is not None
        assert complex_decision.provider == "codex"
        assert complex_decision.effort == "xhigh"

        standard_decision = router.route(_task({"complexity": "standard"}), "standard")
        assert standard_decision is not None
        assert standard_decision.provider == "codex"
        assert standard_decision.effort == "medium"  # tier default, NOT xhigh

    def test_flat_override_still_beats_tier_at_any_tier(self) -> None:
        """The pre-existing flat effort_overrides semantics are untouched: a flat
        entry applies at STANDARD too."""
        router, limits, _ = make_router(
            effort_overrides={"gpt-5.6-sol": "high"},
            effort_overrides_by_tier={},
        )
        limits.enabled = {"codex": 1}
        limits.reservations = {"codex": _reservation("codex")}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.effort == "high"


# ============================================================================
# EXTRA CANDIDATES (PKG-GEMINI-LANE) — registry-only fast-lane models
# ============================================================================

# gemini-3.6-flash: SIMPLE-tier fast-lane primary (0.90), STANDARD diversity
# option (0.40); ceiling standard. It has no fusion-agent ranking, so it is only
# routable via the synthesized extra_candidates path.
GEMINI_FLASH = {
    "model": "gemini-3.6-flash",
    "tier_ceiling": "standard",
    "score_by_tier": {"simple": 0.90, "standard": 0.40},
}
# gemini-3.5-flash-lite: simple-tier bulk alternative, flat score 0.75.
FLASH_LITE = {"model": "gemini-3.5-flash-lite", "tier_ceiling": "simple", "score": 0.75}


class TestExtraCandidates:
    def test_extra_wins_as_primary_at_simple(self) -> None:
        """At simple the fast-lane primary (0.90) outranks every rankings-derived
        candidate (best is sol ≈ 0.79 at ultrafast weights) and its provider is
        DERIVED from the model's lineage → gemini."""
        router, limits, _ = make_router(extra_candidates=[GEMINI_FLASH])
        _all_enabled(limits, "codex", "claude", "grok", "kimi", "gemini")
        decision = router.route(_task(), "simple")
        assert decision is not None
        assert decision.provider == "gemini"
        assert decision.model == "gemini-3.6-flash"

    def test_extra_does_not_outrank_workhorse_at_standard(self) -> None:
        """At standard the extra scores 0.40 and does NOT outrank the ranked
        workhorse (sol ≈ 0.67); the rankings lead stays in control."""
        router, limits, _ = make_router(extra_candidates=[GEMINI_FLASH])
        _all_enabled(limits, "codex", "claude", "grok", "kimi", "gemini")
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "codex"
        assert decision.model != "gemini-3.6-flash"

    def test_extra_is_a_standard_diversity_option_when_ranked_cooling(self) -> None:
        """Proof the extra IS in the candidate set at standard: when every
        rankings-derived provider is cooling, the healthy extra is picked."""
        router, limits, _ = make_router(extra_candidates=[GEMINI_FLASH])
        limits.enabled = {"codex": 1, "claude": 1, "grok": 1, "kimi": 1, "gemini": 1}
        limits.cooling = {"codex", "claude", "grok", "kimi"}
        limits.reservations = {"gemini": _reservation("gemini")}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "gemini"
        assert decision.model == "gemini-3.6-flash"

    def test_extra_absent_at_complex(self) -> None:
        """tier_ceiling never reaches complex: the extra is not synthesized, so
        the complex ranking (sol) decides."""
        router, limits, _ = make_router(extra_candidates=[GEMINI_FLASH])
        _all_enabled(limits, "codex", "claude", "grok", "kimi", "gemini")
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "codex"
        assert decision.model != "gemini-3.6-flash"

    def test_extra_stripped_for_risky_classes(self) -> None:
        """Extras are non-claude → the risk pin strips them for every DENIED
        class (verified through the normal pipeline, not special-cased)."""
        for risk in ("external", "deploy", "destructive"):
            router, limits, _ = make_router(extra_candidates=[GEMINI_FLASH])
            limits.enabled = {"claude": 1, "gemini": 1}
            limits.reservations = {
                "claude": _reservation("claude"),
                "gemini": _reservation("gemini"),
            }
            decision = router.route(_task({"risk_class": risk}), "standard")
            assert decision is not None, risk
            assert decision.provider == "claude", risk
            assert decision.model != "gemini-3.6-flash", risk

    def test_flat_score_and_ceiling_simple(self) -> None:
        """flash-lite uses the flat score (0.75) and a simple ceiling: it is
        routable at simple (picked here because the ranked lane is cooling) but
        NOT synthesized at standard (ceiling exceeded → None when nothing else
        is available)."""
        router, limits, _ = make_router(extra_candidates=[FLASH_LITE])
        limits.enabled = {"codex": 1, "claude": 1, "grok": 1, "kimi": 1, "gemini": 1}
        limits.cooling = {"codex", "claude", "grok", "kimi"}
        limits.reservations = {"gemini": _reservation("gemini")}
        simple_decision = router.route(_task(), "simple")
        assert simple_decision is not None
        assert simple_decision.provider == "gemini"
        assert simple_decision.model == "gemini-3.5-flash-lite"

        # At standard the ranked lane is cooling and flash-lite is above its
        # ceiling → no candidate is routable → requeue.
        router2, limits2, _ = make_router(extra_candidates=[FLASH_LITE])
        limits2.enabled = {"codex": 1, "claude": 1, "grok": 1, "kimi": 1, "gemini": 1}
        limits2.cooling = {"codex", "claude", "grok", "kimi"}
        limits2.reservations = {"gemini": _reservation("gemini")}
        assert router2.route(_task(), "standard") is None

    def test_empty_rankings_yields_fallback_and_extras(self) -> None:
        """With no rankings file at all the built-in claude fallback still leads
        the list AND the opted-in extras append: a healthy extra takes the fast
        lane, a cooling extra falls back to claude (the swarm is never bricked)."""
        extra = [GEMINI_FLASH]
        router, limits, _ = make_router(rankings={}, extra_candidates=extra)
        limits.enabled = {"gemini": 1, "claude": 1}
        limits.reservations = {
            "gemini": _reservation("gemini"),
            "claude": _reservation("claude"),
        }
        healthy = router.route(_task(), "simple")
        assert healthy is not None
        assert healthy.provider == "gemini"  # extra appended on the empty path

        router2, limits2, _ = make_router(rankings={}, extra_candidates=extra)
        limits2.enabled = {"gemini": 1, "claude": 1}
        limits2.cooling = {"gemini"}
        limits2.reservations = {"claude": _reservation("claude")}
        cooled = router2.route(_task(), "simple")
        assert cooled is not None
        assert cooled.provider == "claude"  # fallback-claude still present + leads
        assert cooled.model == "sonnet"

    def test_provider_derived_not_hardcoded(self) -> None:
        """The synthesized provider comes from the injected lineage index, not a
        literal: re-point gemini-3.6-flash's lineage and the route follows it."""
        router, limits, _ = make_router(
            extra_candidates=[GEMINI_FLASH],
            model_lineages={**MODEL_LINEAGES, "gemini-3.6-flash": "grok"},
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi", "gemini")
        decision = router.route(_task(), "simple")
        assert decision is not None
        assert decision.provider == "grok"  # lineage remap → provider follows
        assert decision.model == "gemini-3.6-flash"

    def test_malformed_unknown_model_warn_skipped(self) -> None:
        """An entry naming a model with no resolvable lineage is skipped at
        synthesis, never raised; routing proceeds on the rankings."""
        router, limits, _ = make_router(
            extra_candidates=[{"model": "no-such-model", "tier_ceiling": "simple", "score": 0.99}]
        )
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "simple")
        assert decision is not None
        assert decision.provider == "codex"  # ranking (sol) wins; extra dropped
        assert decision.model != "no-such-model"

    def test_malformed_bad_tier_ceiling_warn_skipped(self) -> None:
        """tier_ceiling 'complex' / junk / missing model are dropped by the
        parser (never raised); no synthesized candidate appears."""
        bad = [
            {"model": "gemini-3.6-flash", "tier_ceiling": "complex", "score": 0.99},
            {"model": "gemini-3.6-flash", "tier_ceiling": "banana"},
            {"tier_ceiling": "simple", "score": 0.99},  # missing model
        ]
        router, limits, _ = make_router(extra_candidates=bad)
        _all_enabled(limits, "codex", "claude", "grok", "kimi", "gemini")
        decision = router.route(_task(), "simple")
        assert decision is not None
        assert decision.provider == "codex"
        assert decision.model != "gemini-3.6-flash"

    def test_absent_extra_config_is_byte_identical(self) -> None:
        """No extra_candidates → pure ranking at every tier (backward compat)."""
        router, limits, _ = make_router(extra_candidates=[])
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        for tier in ("simple", "standard", "complex"):
            decision = router.route(_task(), tier)
            assert decision is not None
            assert decision.provider == "codex", tier


# ============================================================================
# BACKWARD COMPATIBILITY (absent config == pre-6241542 behavior)
# ============================================================================


class TestBackwardCompatibility:
    def test_empty_lane_floors_pure_ranking(self) -> None:
        router, limits, _ = make_router(lane_floors={})
        _all_enabled(limits, "codex", "claude", "grok")
        for tier in ("simple", "standard", "complex"):
            decision = router.route(_task(), tier)
            assert decision is not None
            assert decision.provider == "codex", tier  # #1 mechanically

    def test_empty_category_pins_pure_ranking(self) -> None:
        router, limits, _ = make_router(category_pins={})
        _all_enabled(limits, "codex", "claude")
        decision = router.route(
            _task({"risk_class": "none"}, discipline="some_discipline"), "standard"
        )
        assert decision is not None
        assert decision.provider == "codex"

    def test_overflow_disabled_by_default(self) -> None:
        # No floor + overflow disabled → the plain ranking decides at complex,
        # byte-identical to pre-6241542 behavior.
        router, limits, _ = make_router(overflow=({}, 1.0))
        _all_enabled(limits, "codex", "claude", "grok", "kimi")
        decision = router.route(_task(), "complex")
        assert decision is not None
        assert decision.provider == "codex"

    def test_simple_standard_unaffected_by_any_floor(self) -> None:
        lane_floors = {"complex": ["claude-opus-5"]}
        router, limits, _ = make_router(lane_floors=lane_floors)
        _all_enabled(limits, "codex", "claude", "grok")
        for tier in ("simple", "standard"):
            decision = router.route(_task({"risk_class": "none"}), tier)
            assert decision is not None
            assert decision.provider == "codex", tier
