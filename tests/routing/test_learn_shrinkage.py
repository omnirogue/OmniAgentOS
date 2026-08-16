"""Beta-Binomial hierarchical shrinkage learner (WP5b, plan A7 idiom).

Pure math over synthetic counts — no I/O, no cascade run. The Wilson-bound
path (``test_learn.py``) is untouched: these helpers are ADDITIVE.
"""

from __future__ import annotations

import pytest

from omniagentos.routing.cascade import CascadeTier
from omniagentos.routing.learn import (
    beta_binomial_rate,
    decay_weight,
    decayed_tier_counts,
    hierarchical_tier_rates,
    recommend_start_tier_shrunk,
)

LADDER = (
    CascadeTier(name="simple", adapter="t", cost_rank=1.0),
    CascadeTier(name="standard", adapter="t", cost_rank=2.0),
    CascadeTier(name="complex", adapter="t", cost_rank=3.0),
)
TIERS = tuple(t.name for t in LADDER)

DAY = 86400.0


class TestBetaBinomialRate:
    def test_no_samples_is_exactly_the_parent_rate(self) -> None:
        assert beta_binomial_rate(0, 0, parent_rate=0.7, k=5.0) == pytest.approx(0.7)

    def test_low_n_shrinks_toward_the_parent(self) -> None:
        """2 lucky wins in 2 tries must NOT read as a 100% win rate: with
        k=5 pseudo-samples of a 0.2 parent the estimate sits between."""
        rate = beta_binomial_rate(2, 2, parent_rate=0.2, k=5.0)
        assert rate == pytest.approx((2 + 5 * 0.2) / (2 + 5))
        # Closer to the parent than to the raw 1.0 observed rate.
        assert abs(rate - 0.2) < abs(rate - 1.0)

    def test_high_n_converges_to_the_observed_rate(self) -> None:
        rate = beta_binomial_rate(900, 1000, parent_rate=0.2, k=5.0)
        assert rate == pytest.approx(0.9, abs=0.01)

    def test_k_zero_degrades_to_the_raw_rate(self) -> None:
        assert beta_binomial_rate(3, 4, parent_rate=0.9, k=0.0) == pytest.approx(0.75)
        assert beta_binomial_rate(0, 0, parent_rate=0.9, k=0.0) == pytest.approx(0.9)


class TestDecay:
    def test_full_weight_inside_seven_days(self) -> None:
        assert decay_weight(0.0) == 1.0
        assert decay_weight(3.0) == 1.0
        assert decay_weight(7.0) == 1.0

    def test_linear_decay_between_seven_and_fourteen_days(self) -> None:
        assert decay_weight(10.5) == pytest.approx(0.5)
        assert decay_weight(12.25) == pytest.approx(0.25)

    def test_zero_weight_past_fourteen_days(self) -> None:
        assert decay_weight(14.0) == 0.0
        assert decay_weight(30.0) == 0.0

    def test_negative_age_clamps_to_full_weight(self) -> None:
        assert decay_weight(-1.0) == 1.0

    def test_decayed_counts_weight_samples_by_age(self) -> None:
        now = 1_000_000_000.0
        samples = [
            {"tier_name": "simple", "win": True, "ts": now - 1 * DAY},  # weight 1.0
            {"tier_name": "simple", "win": False, "ts": now - 10.5 * DAY},  # 0.5
            {"tier_name": "simple", "win": True, "ts": now - 30 * DAY},  # 0.0 (dropped)
            {"tier_name": "unknown-tier", "win": True, "ts": now},  # skipped
            {"tier_name": "simple", "win": True},  # no ts: skipped
        ]
        counts = decayed_tier_counts(samples, TIERS, now_ts=now)
        wins, n = counts["simple"]
        assert wins == pytest.approx(1.0)
        assert n == pytest.approx(1.5)
        assert counts["standard"] == (0.0, 0.0)


class TestHierarchy:
    def test_empty_leaf_inherits_the_parent_estimate(self) -> None:
        """Shrinkage toward the parent at zero leaf samples: the leaf level
        has nothing, the parent has 8/10, so the leaf's estimate is the
        parent's shrunk rate — sane from sample #1 instead of starving."""
        leaf = {name: (0.0, 0.0) for name in TIERS}
        parent = {"simple": (8.0, 10.0), "standard": (0.0, 0.0), "complex": (0.0, 0.0)}
        rates = hierarchical_tier_rates([leaf, parent], TIERS, k=5.0, global_prior=0.5)
        parent_rate = (8 + 5 * 0.5) / (10 + 5)
        assert rates["simple"] == pytest.approx(parent_rate)
        # A tier with no data anywhere ends at the global prior.
        assert rates["complex"] == pytest.approx(0.5)

    def test_strong_leaf_evidence_overrides_a_weak_parent(self) -> None:
        leaf = {"simple": (95.0, 100.0), "standard": (0.0, 0.0), "complex": (0.0, 0.0)}
        parent = {"simple": (1.0, 10.0), "standard": (0.0, 0.0), "complex": (0.0, 0.0)}
        rates = hierarchical_tier_rates([leaf, parent], TIERS, k=5.0, global_prior=0.5)
        assert rates["simple"] > 0.85  # leaf dominates despite the 10% parent


class TestRecommendation:
    def test_no_data_falls_back_to_cheapest_by_expected_chained_cost(self) -> None:
        empty = {name: (0.0, 0.0) for name in TIERS}
        assert recommend_start_tier_shrunk([empty, empty], LADDER) == 0

    def test_losing_cheap_tier_recommends_the_confident_next_rung(self) -> None:
        leaf = {
            "simple": (0.0, 10.0),  # cheap tier reliably loses
            "standard": (10.0, 10.0),  # next rung reliably wins
            "complex": (0.0, 0.0),
        }
        assert recommend_start_tier_shrunk([leaf, dict(leaf)], LADDER) == 1

    def test_decayed_out_history_stops_steering(self) -> None:
        """The same losing-simple history, but 30 days old: decay zeroes it
        and the recommendation returns to the cheap-first default."""
        now = 1_000_000_000.0
        stale = [{"tier_name": "simple", "win": False, "ts": now - 30 * DAY} for _ in range(10)] + [
            {"tier_name": "standard", "win": True, "ts": now - 30 * DAY} for _ in range(10)
        ]
        counts = decayed_tier_counts(stale, TIERS, now_ts=now)
        assert recommend_start_tier_shrunk([counts, dict(counts)], LADDER) == 0

    def test_empty_ladder_returns_zero(self) -> None:
        assert recommend_start_tier_shrunk([{}], ()) == 0
