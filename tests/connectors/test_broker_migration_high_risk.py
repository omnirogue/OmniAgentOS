"""Regression coverage for Tier 1 credential preflights.

Each collector must ask the broker before deciding a money source is configured;
the raw process environment is intentionally not part of these tests.
"""

from __future__ import annotations

from typing import Any

from omniagentos.banking import collect as banking_collect
from omniagentos.goals import collect as goals_collect
from omniagentos.revenue import collect as revenue_collect


def test_banking_configuration_probe_uses_broker(monkeypatch: Any) -> None:
    calls: list[tuple[str, str]] = []

    def resolve(capability: Any, env_name: str) -> str:
        calls.append((capability.id, env_name))
        return "brokered"

    monkeypatch.setattr(banking_collect.broker, "resolve_one_for", resolve)
    assert banking_collect._configured("teller.read", "TELLER_ACCESS_TOKEN") is True
    assert calls == [("teller.read", "TELLER_ACCESS_TOKEN")]


def test_goals_collection_preflight_uses_broker(monkeypatch: Any) -> None:
    calls: list[tuple[str, str]] = []

    def resolve(capability: Any, env_name: str) -> str:
        calls.append((capability.id, env_name))
        return "brokered"

    monkeypatch.setattr(goals_collect.broker, "resolve_one_for", resolve)
    assert (
        goals_collect._credential("stripe_acmeuni.read", "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY")
        == "brokered"
    )
    assert calls == [("stripe_acmeuni.read", "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY")]


def test_revenue_collection_preflight_uses_broker(monkeypatch: Any) -> None:
    calls: list[tuple[str, str]] = []

    def resolve(capability: Any, env_name: str) -> str:
        calls.append((capability.id, env_name))
        return "brokered"

    monkeypatch.setattr(revenue_collect.broker, "resolve_one_for", resolve)
    assert revenue_collect._credential("fanbasis.read", "FANBASIS_API_KEY") == "brokered"
    assert calls == [("fanbasis.read", "FANBASIS_API_KEY")]
