"""Tests for nested-timeout monotonicity (W3-killconfirm fix #2).

Validates that the timeout ladder is strictly increasing so the innermost
layer (provider session idle timeout) always fails first with the most
specific error, before the coordinator wall deadline fires.

The timeout ladder has three layers:
  1. Gate spec timeout (600s, for verify_command execution)
  2. Provider session idle timeout (from tier budget, but reduced by IDLE_TIMEOUT_FRACTION)
  3. Coordinator wall deadline (from tier budget)

Each inner timeout MUST be strictly less than its parent.
"""

from __future__ import annotations

from omniagentos.swarm.scheduler import (
    DEFAULT_TIMEOUT_MINUTES,
    IDLE_TIMEOUT_FRACTION,
    TIER_LADDER,
)


def test_idle_timeout_fraction_defined():
    """IDLE_TIMEOUT_FRACTION constant must be defined and in valid range."""
    assert hasattr(IDLE_TIMEOUT_FRACTION, "__float__") or isinstance(
        IDLE_TIMEOUT_FRACTION, (int, float)
    ), "IDLE_TIMEOUT_FRACTION must be a number"
    fraction = float(IDLE_TIMEOUT_FRACTION)
    assert 0 < fraction < 1.0, "IDLE_TIMEOUT_FRACTION must be between 0 and 1"


def test_idle_timeout_strictly_less_than_wall_deadline():
    """Provider session idle timeout must be strictly less than wall deadline.

    For each tier, the idle_minutes passed to SpawnRequest should be
    strictly less than the tier budget. This ensures that if a provider
    session goes idle, it will timeout and signal before the coordinator's
    wall deadline fires.

    Without this, a stalled session that goes idle can report the generic
    "tier timeout" error instead of the specific "provider session went idle"
    error.
    """
    for tier in TIER_LADDER:
        tier_seconds = DEFAULT_TIMEOUT_MINUTES[tier] * 60.0
        idle_minutes = (tier_seconds / 60.0) * float(IDLE_TIMEOUT_FRACTION)
        idle_seconds = idle_minutes * 60.0

        # The idle timeout MUST be strictly less than the tier deadline
        assert (
            idle_seconds < tier_seconds
        ), f"Tier {tier}: idle_timeout ({idle_seconds}s) must be < wall_deadline ({tier_seconds}s)"


def test_timeout_ladder_monotonicity():
    """Tier timeouts must be strictly increasing.

    simple < standard < complex, so escalation always increases the budget
    and a task is never starved by an inconsistent escalation step.
    """
    timeouts = [DEFAULT_TIMEOUT_MINUTES[tier] for tier in TIER_LADDER]

    for i in range(len(timeouts) - 1):
        current_tier = TIER_LADDER[i]
        next_tier = TIER_LADDER[i + 1]
        current_timeout = timeouts[i]
        next_timeout = timeouts[i + 1]

        assert (
            current_timeout < next_timeout
        ), f"Tier ladder not monotonic: {current_tier} ({current_timeout}min) >= {next_tier} ({next_timeout}min)"


def test_gate_spec_timeout_vs_tier_idle_timeout():
    """Gate spec timeout (600s) should be reasonable relative to smallest tier timeout.

    The gate spec timeout (600s = 10min) is used for verify_command execution
    and must not exceed the smallest tier's idle timeout, otherwise a stalled
    verification could cause the session idle timeout to fire before the gate
    times out.

    Current defaults: simple tier = 15min, gate spec = 10min (ok)
    """
    gate_spec_timeout_s = 600
    smallest_tier_seconds = DEFAULT_TIMEOUT_MINUTES[TIER_LADDER[0]] * 60.0
    smallest_idle_seconds = smallest_tier_seconds * float(IDLE_TIMEOUT_FRACTION)

    assert (
        gate_spec_timeout_s < smallest_idle_seconds
    ), f"Gate spec timeout ({gate_spec_timeout_s}s) >= smallest idle timeout ({smallest_idle_seconds}s)"


def test_idle_timeout_computation():
    """Idle timeout should be computed correctly as a fraction of tier timeout.

    idle_minutes = (tier_seconds / 60.0) * IDLE_TIMEOUT_FRACTION
    """
    tier_seconds = 30 * 60  # 30 minutes (standard tier)
    idle_minutes = (tier_seconds / 60.0) * float(IDLE_TIMEOUT_FRACTION)

    # The idle_minutes should be less than the tier budget
    tier_minutes = tier_seconds / 60.0
    assert idle_minutes < tier_minutes, "Idle timeout must be less than tier budget"

    # Sanity check: idle should be between 1-29 minutes for 30min tier
    assert 1.0 < idle_minutes < tier_minutes, "Idle timeout in expected range"
