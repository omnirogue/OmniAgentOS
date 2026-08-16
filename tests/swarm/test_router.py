"""SwarmRouter (WP5b) behavior suite — fake rankings/digest/limits, no I/O.

Covers the route chain: risk pin -> claude, provider_pressure
deprioritization, all-cooling -> None (scheduler park signal), account
reservation riding the decision, provider_switched emission, and the learned
start tier plugging the Beta-Binomial learner into first attempts only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from omniagentos.swarm.contracts import ACTION_PROVIDER_SWITCHED
from omniagentos.swarm.router import SwarmRouter
from tests.swarm.scheduler_fakes import RecordingEmitter

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
DAY = 86400.0


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


# codex outranks claude mechanically in every mode (higher coding score).
RANKINGS = {
    "sol-coder": _agent("sol-coder", "codex", "gpt-5.6-sol", coding=0.95, tooluse=0.9),
    "claude-coder": _agent("claude-coder", "claude", "sonnet", coding=0.80, tooluse=0.8),
}


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
    samples: list[dict[str, Any]] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    emitter: RecordingEmitter | None = None,
    **overrides: Any,
) -> tuple[SwarmRouter, FakeRouterLimits, RecordingEmitter]:
    limits = limits or FakeRouterLimits()
    emitter = emitter or RecordingEmitter()
    router = SwarmRouter(
        emitter=emitter,
        limits=limits,
        lineage_providers={},
        rankings_loader=lambda: dict(rankings if rankings is not None else RANKINGS),
        digest_loader=lambda: None,
        samples_loader=lambda: list(samples or []),
        attempts_loader=lambda task_id: list(attempts or []),
        now=lambda: NOW,
        **overrides,
    )
    return router, limits, emitter


def _task(swarm_json: dict[str, Any] | None = None, task_id: str = "task1") -> dict[str, Any]:
    return {
        "id": task_id,
        "swarm_run_id": "swr1",
        "swarm_json": json.dumps(swarm_json or {}),
    }


class TestRiskPin:
    def test_risky_task_routes_to_claude_despite_a_better_codex_score(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.reservations = {
            "claude": _reservation("claude"),
            "codex": _reservation("codex"),
        }
        for risk in ("external", "deploy", "destructive"):
            decision = router.route(_task({"risk_class": risk}), "standard")
            assert decision is not None
            assert decision.provider == "claude", risk

    def test_risk_pin_survives_rankings_without_a_claude_coder(self) -> None:
        """No claude agent in the rankings: the pin falls back to the
        built-in claude candidate rather than leaking to codex."""
        router, limits, _ = make_router(
            rankings={"sol-coder": RANKINGS["sol-coder"]},
        )
        decision = router.route(_task({"risk_class": "deploy"}), "complex")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.model == "opus"  # complex fallback rung

    def test_unrisky_task_takes_the_top_ranked_provider(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.reservations = {
            "claude": _reservation("claude"),
            "codex": _reservation("codex"),
        }
        decision = router.route(_task({"risk_class": "none"}), "standard")
        assert decision is not None
        assert decision.provider == "codex"


class TestPressure:
    def test_pressured_provider_loses_to_a_healthy_one(self) -> None:
        """Pre-429 backpressure: codex outranks claude mechanically, but at
        0.9 pressure it is deprioritized below healthy claude."""
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.pressure = {"codex": 0.9, "claude": 0.0}
        limits.reservations = {
            "claude": _reservation("claude"),
            "codex": _reservation("codex"),
        }
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "claude"

    def test_cooling_provider_is_dropped_entirely(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.cooling = {"codex"}
        limits.reservations = {"claude": _reservation("claude")}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "claude"
        assert "codex" not in limits.reserve_calls

    def test_all_cooling_signals_the_scheduler_park_path(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.cooling = {"claude", "codex"}
        assert router.route(_task(), "standard") is None

    def test_reservation_race_falls_through_to_the_next_provider(self) -> None:
        """Pressure said codex was fine but reserve_account came back empty
        (cooled/saturated since the read): the router moves on."""
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.reservations = {"claude": _reservation("claude")}  # codex: None
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "claude"
        assert limits.reserve_calls == ["codex", "claude"]

    def test_every_reservation_lost_returns_none(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"claude": 1, "codex": 1}
        assert router.route(_task(), "standard") is None

    def test_unreadable_enabled_count_is_not_default_cli_login(self) -> None:
        """enabled_account_count exception must not free-look as '0 accounts → default CLI'.

        Counterfeit: ``except Exception: return 0`` then ``configured = count > 0``
        treats measurement failure as unconfigured and returns a route with
        account_id=None / no reservation — favourable when health could not measure.
        """

        class _RaisingEnabled(FakeRouterLimits):
            def enabled_account_count(self, provider: str) -> int:
                raise RuntimeError(f"ledger unreadable for {provider}")

        limits = _RaisingEnabled()
        # Even with reservations ready, unreadable count must not free-route.
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
        }
        router, _, _ = make_router(limits=limits)
        decision = router.route(_task(), "standard")
        # Park / no route — never a free default-login decision.
        assert decision is None
        assert limits.reserve_calls == []

    def test_unreadable_enabled_count_skips_provider_not_default_login(self) -> None:
        """Only the unreadable provider is skipped; a measured neighbour still routes."""

        class _CodexCountRaises(FakeRouterLimits):
            def enabled_account_count(self, provider: str) -> int:
                if provider == "codex":
                    raise RuntimeError("codex ledger unreadable")
                return super().enabled_account_count(provider)

        limits = _CodexCountRaises()
        limits.enabled = {"claude": 1}  # measured: claude configured
        limits.reservations = {"claude": _reservation("claude")}
        router, _, _ = make_router(limits=limits)
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.account_id == "acct_claude_1"
        assert decision.reservation_id == "rsv_claude_1"
        # codex must not win via free default-login path.
        assert "codex" not in limits.reserve_calls

    def test_unreadable_cooling_is_not_healthy(self) -> None:
        """all_cooling exception must not free-look as 'not cooling' (healthy).

        Counterfeit: ``except Exception: return False`` + bare truthiness lets a
        provider whose cooling state could not be measured stay viable.
        """

        class _CoolingRaises(FakeRouterLimits):
            def all_cooling(self, provider: str) -> bool:
                raise RuntimeError(f"cooling unreadable for {provider}")

        limits = _CoolingRaises()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.reservations = {
            "claude": _reservation("claude"),
            "codex": _reservation("codex"),
        }
        router, _, _ = make_router(limits=limits)
        # Both providers unreadable for cooling → park, not a free healthy route.
        assert router.route(_task(), "standard") is None
        assert limits.reserve_calls == []

    def test_unreadable_pressure_is_not_zero_healthy(self) -> None:
        """provider_pressure exception must not free-look as 0.0 healthy pressure.

        Counterfeit: ``except Exception: return 0.0`` treats unreadable pressure
        as fully healthy and lets the provider win on score.
        """

        class _PressureRaises(FakeRouterLimits):
            def provider_pressure(self, provider: str) -> float:
                if provider == "codex":
                    raise RuntimeError("codex pressure unreadable")
                return super().provider_pressure(provider)

        limits = _PressureRaises()
        limits.enabled = {"claude": 1, "codex": 1}
        limits.pressure = {"claude": 0.0}
        limits.reservations = {
            "claude": _reservation("claude"),
            "codex": _reservation("codex"),
        }
        router, _, _ = make_router(limits=limits)
        decision = router.route(_task(), "standard")
        # codex outranks claude mechanically but unreadable pressure must not
        # free-look as healthy 0.0 — skip codex, route measured claude.
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.account_id == "acct_claude_1"
        assert "codex" not in limits.reserve_calls


class TestAccounts:
    def test_decision_carries_the_reservation(self) -> None:
        router, limits, _ = make_router()
        limits.enabled = {"codex": 1, "claude": 1}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
        }
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.account_id == "acct_codex_1"
        assert decision.reservation_id == "rsv_codex_1"

    def test_unconfigured_provider_routes_with_the_default_login(self) -> None:
        """No accounts registered at all: the default CLI login is the
        account — no reservation, no account_id, never blocked."""
        router, limits, _ = make_router()
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.account_id is None
        assert decision.reservation_id is None
        assert limits.reserve_calls == []

    def test_missing_rankings_never_bricks_the_swarm(self) -> None:
        router, _, _ = make_router(rankings={})
        decision = router.route(_task(), "simple")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.model == "sonnet"


class TestProviderSwitched:
    def test_reroute_to_a_new_provider_emits_the_event(self) -> None:
        router, limits, emitter = make_router(
            attempts=[
                {
                    "id": "swa_0",
                    "seq": 0,
                    "provider": "codex",
                    "ended_at": "2026-07-23T11:00:00Z",
                    "end_reason": "rate_limited",
                }
            ],
        )
        limits.enabled = {"claude": 1, "codex": 1}
        limits.cooling = {"codex"}  # rate-limited terminal cooled codex
        limits.reservations = {"claude": _reservation("claude")}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "claude"
        events = emitter.of(ACTION_PROVIDER_SWITCHED)
        assert events == [
            {
                "task_id": "task1",
                "from_provider": "codex",
                "to_provider": "claude",
                "reason": "reroute",
            }
        ]

    def test_same_provider_reroute_stays_silent(self) -> None:
        router, limits, emitter = make_router(
            attempts=[
                {
                    "id": "swa_0",
                    "seq": 0,
                    "provider": "codex",
                    "ended_at": "2026-07-23T11:00:00Z",
                    "end_reason": "review_denied",
                }
            ],
        )
        limits.enabled = {"codex": 1, "claude": 1}
        limits.reservations = {
            "codex": _reservation("codex"),
            "claude": _reservation("claude"),
        }
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "codex"
        assert emitter.of(ACTION_PROVIDER_SWITCHED) == []

    def test_first_attempt_emits_nothing(self) -> None:
        router, limits, emitter = make_router(attempts=[])
        router.route(_task(), "standard")
        assert emitter.of(ACTION_PROVIDER_SWITCHED) == []


class TestLearnedStartTier:
    def _losing_simple_samples(self) -> list[dict[str, Any]]:
        now_ts = NOW.timestamp()
        losses = [
            {"tier_name": "simple", "win": False, "ts": now_ts - DAY, "complexity": "simple"}
            for _ in range(10)
        ]
        wins = [
            {"tier_name": "standard", "win": True, "ts": now_ts - DAY, "complexity": "simple"}
            for _ in range(10)
        ]
        return losses + wins

    def test_first_attempt_starts_at_the_learned_tier(self) -> None:
        """simple reliably loses / standard reliably wins for this class:
        the learner raises the START rung so the double-work never happens."""
        router, _, _ = make_router(samples=self._losing_simple_samples())
        decision = router.route(_task({"complexity": "simple"}), "simple")
        assert decision is not None
        assert decision.tier == "standard"

    def test_mid_ladder_routes_keep_the_scheduler_tier(self) -> None:
        """current_tier set = escalation/retry territory: the scheduler owns
        the rung and the learner must not fight the ladder."""
        router, _, _ = make_router(samples=self._losing_simple_samples())
        task = _task({"complexity": "simple", "current_tier": "simple"})
        decision = router.route(task, "simple")
        assert decision is not None
        assert decision.tier == "simple"

    def test_no_history_keeps_the_hint(self) -> None:
        router, _, _ = make_router(samples=[])
        decision = router.route(_task({"complexity": "standard"}), "standard")
        assert decision is not None
        assert decision.tier == "standard"

    def test_down_tier_needs_leaf_evidence(self) -> None:
        """A confident cheap-tier history in ANOTHER class (thin leaf) must
        not down-tier this class's hint below min samples."""
        now_ts = NOW.timestamp()
        other_class = [
            {"tier_name": "simple", "win": True, "ts": now_ts - DAY, "complexity": "standard"}
            for _ in range(50)
        ]
        router, _, _ = make_router(samples=other_class)
        decision = router.route(_task({"complexity": "complex"}), "complex")
        assert decision is not None
        assert decision.tier == "complex"


class TestSpeedTierFloor:
    """D10 Mode dial: the run's persisted speed (stamped per card at provision)
    floors/pins the tier — fast pins START tier 'simple', ultra floors
    'complex'; auto/absent change nothing."""

    def test_fast_pins_the_start_tier_to_simple(self) -> None:
        router, _, _ = make_router()
        decision = router.route(_task({"speed": "fast", "complexity": "standard"}), "standard")
        assert decision is not None
        assert decision.tier == "simple"

    def test_fast_pin_beats_the_learner(self) -> None:
        """The learner may not re-raise a Fast Mode start tier: escalation on
        failure stays the scheduler's job."""
        now_ts = NOW.timestamp()
        losing_simple = [
            {"tier_name": "simple", "win": False, "ts": now_ts - DAY, "complexity": "simple"}
            for _ in range(10)
        ] + [
            {"tier_name": "standard", "win": True, "ts": now_ts - DAY, "complexity": "simple"}
            for _ in range(10)
        ]
        router, _, _ = make_router(samples=losing_simple)
        decision = router.route(_task({"speed": "fast", "complexity": "simple"}), "simple")
        assert decision is not None
        assert decision.tier == "simple"

    def test_fast_mid_ladder_keeps_the_scheduler_rung(self) -> None:
        """Escalation territory: the scheduler owns the rung — a fast pin
        applies to START tiers only."""
        router, _, _ = make_router()
        task = _task({"speed": "fast", "current_tier": "standard", "retries": 1})
        decision = router.route(task, "standard")
        assert decision is not None
        assert decision.tier == "standard"

    def test_ultra_floors_complex(self) -> None:
        router, _, _ = make_router()
        decision = router.route(_task({"speed": "ultra", "complexity": "simple"}), "simple")
        assert decision is not None
        assert decision.tier == "complex"

    def test_ultra_floor_applies_mid_ladder_too(self) -> None:
        """A floor can only raise: even on a retry the rung never sits below
        'complex' for an ultra run."""
        router, _, _ = make_router()
        task = _task({"speed": "ultra", "current_tier": "standard", "retries": 1})
        decision = router.route(task, "standard")
        assert decision is not None
        assert decision.tier == "complex"

    def test_auto_and_absent_change_nothing(self) -> None:
        router, _, _ = make_router()
        for swarm_json in ({"speed": "auto"}, {}, {"speed": "warp"}):
            decision = router.route(_task({**swarm_json, "complexity": "standard"}), "standard")
            assert decision is not None
            assert decision.tier == "standard"
