"""U5 — unified worker abstraction over API and subscription terminal workers.

Consolidates selection signals from:
  - routing/limit_state (accounts, cooldowns, fleet_available)
  - adapters/registry (CLI providers)
  - longhaul/routing.rank_workers (registry scores)
  - swarm/router (tier + effort decisions stay upstream)

Does **not** proxy consumer subscriptions through LiteLLM. Auth state stays
per-worker profile; never shared across profiles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkerEndpoint:
    """One selectable execution endpoint (terminal CLI or API-backed)."""

    worker_id: str
    provider: str  # claude | codex | grok | gemini | kimi | api
    model: str
    mechanism: str  # terminal_cli | api
    account_id: str | None = None
    supports_effort: bool = True
    available: bool = True
    cooldown_until: str | None = None
    inflight: int = 0
    max_inflight: int = 1
    recent_success_rate: float | None = None
    notes: str = ""

    def can_accept(self) -> bool:
        return self.available and self.inflight < self.max_inflight and not self.cooldown_until


@dataclass(frozen=True, slots=True)
class WorkerSelection:
    endpoint: WorkerEndpoint | None
    tier: str
    effort: str
    reason: str
    candidates: tuple[WorkerEndpoint, ...] = ()


def list_terminal_workers(
    *,
    providers: Sequence[str] = ("claude", "codex", "grok"),
    models: Mapping[str, Sequence[str]] | None = None,
) -> list[WorkerEndpoint]:
    """Static catalog of terminal CLI workers (auth isolated per account later)."""
    defaults: dict[str, list[str]] = {
        "claude": ["claude-sonnet-4", "claude-opus-5"],
        "codex": ["gpt-5.6-sol", "gpt-5.6-terra"],
        "grok": ["grok-4.5"],
    }
    models = {k: list(v) for k, v in (models or defaults).items()}
    out: list[WorkerEndpoint] = []
    for provider in providers:
        for model in models.get(provider, []):
            out.append(
                WorkerEndpoint(
                    worker_id=f"{provider}:{model}",
                    provider=provider,
                    model=model,
                    mechanism="terminal_cli",
                    supports_effort=provider in {"codex", "grok", "claude"},
                    # High concurrency for max production throughput (no artificial
                    # low caps — fleet pressure is handled by limit_state overlay).
                    max_inflight=32 if provider == "claude" else 16,
                )
            )
    return out


def annotate_with_limit_state(
    endpoints: Sequence[WorkerEndpoint],
    *,
    store: Any = None,
) -> list[WorkerEndpoint]:
    """Overlay durable limit_state when a store is available; else pass-through."""
    if store is None:
        return list(endpoints)
    try:
        from omniagentos.routing import limit_state as ls

        annotated: list[WorkerEndpoint] = []
        for ep in endpoints:
            # Best-effort: if fleet is saturated, mark unavailable.
            try:
                available = True
                if hasattr(ls, "fleet_available"):
                    # fleet_available(store) may return bool or tuple
                    fa = ls.fleet_available(store)
                    if isinstance(fa, tuple):
                        available = bool(fa[0]) if fa else True
                    else:
                        available = bool(fa)
            except Exception:  # noqa: BLE001
                available = ep.available
            annotated.append(
                WorkerEndpoint(
                    worker_id=ep.worker_id,
                    provider=ep.provider,
                    model=ep.model,
                    mechanism=ep.mechanism,
                    account_id=ep.account_id,
                    supports_effort=ep.supports_effort,
                    available=available and ep.available,
                    cooldown_until=ep.cooldown_until,
                    inflight=ep.inflight,
                    max_inflight=ep.max_inflight,
                    recent_success_rate=ep.recent_success_rate,
                    notes=ep.notes,
                )
            )
        return annotated
    except Exception:  # noqa: BLE001
        return list(endpoints)


def select_worker(
    *,
    tier: str,
    effort: str,
    preferred_providers: Sequence[str] | None = None,
    endpoints: Sequence[WorkerEndpoint] | None = None,
    store: Any = None,
    model_role: str | None = None,
    allowed_providers: Sequence[str] | None = None,
) -> WorkerSelection:
    """Two-step selection: tier/effort already decided; pick an eligible endpoint.

    Preference: preferred_providers order, then available, then lower inflight.

    When ``OMNIAGENTOS_CHAMPION_ROUTING_MODE=enforce`` and a promoted
    model_assignment champion is available for ``model_role``, the champion's
    provider order is consulted (via the lazy lab.runtime accessor). Shadow and
    off modes leave selection on the baseline preference list.

    When ``allowed_providers`` is set and ``OMNIAGENTOS_ALLOWED_PROVIDERS_MODE``
    is ``enforce``, the pool is filtered before selection (D5). Exhaustion
    returns ``reason="provider_not_allowed"`` rather than a banned provider.
    """
    pool = list(endpoints) if endpoints is not None else list_terminal_workers()
    pool = annotate_with_limit_state(pool, store=store)

    # D5: per-run allowlist (off/shadow/enforce). Imported before the try so an
    # ImportError cannot make the except-clause evaluation raise NameError.
    from omniagentos.providers.constraints import ProviderNotAllowed, filter_allowed

    try:
        pool = filter_allowed(pool, allowed_providers, stage="worker_rotation")
    except ProviderNotAllowed:
        return WorkerSelection(
            endpoint=None,
            tier=tier,
            effort=effort,
            reason="provider_not_allowed",
            candidates=tuple(pool) if pool else (),
        )
    except Exception:  # noqa: BLE001 — never break selection on constraint import errors
        pass

    preferred = list(preferred_providers or ("claude", "codex", "grok"))
    try:
        from omniagentos.providers.constraints import allowed_providers_mode

        if allowed_providers and allowed_providers_mode() != "off":
            allow = [str(p).strip().lower() for p in allowed_providers if str(p).strip()]
            preferred = [p for p in preferred if p in allow] + [
                p for p in allow if p not in preferred
            ]
    except Exception:  # noqa: BLE001
        pass

    # D1: champion-informed provider preference (enforce only changes selection).
    try:
        from omniagentos.cbm.champion_routing import preferred_providers_for_role

        preferred = preferred_providers_for_role(model_role, preferred)
    except Exception:  # noqa: BLE001 — baseline selection always available
        pass

    def sort_key(ep: WorkerEndpoint) -> tuple[int, int, str]:
        try:
            pref = preferred.index(ep.provider)
        except ValueError:
            pref = 99
        return (0 if ep.can_accept() else 1, pref, ep.worker_id)

    ordered = sorted(pool, key=sort_key)
    eligible = [e for e in ordered if e.can_accept()]
    if not eligible:
        return WorkerSelection(
            endpoint=None,
            tier=tier,
            effort=effort,
            reason="no_eligible_worker",
            candidates=tuple(ordered),
        )
    pick = eligible[0]
    try:
        from omniagentos.providers.constraints import (
            ProviderNotAllowed,
            assert_provider_allowed,
        )

        assert_provider_allowed(
            pick.provider,
            allowed_providers,
            stage="worker_rotation_pick",
        )
    except ProviderNotAllowed:
        return WorkerSelection(
            endpoint=None,
            tier=tier,
            effort=effort,
            reason="provider_not_allowed",
            candidates=tuple(ordered),
        )
    except Exception:  # noqa: BLE001 — never break selection on constraint import errors
        pass
    return WorkerSelection(
        endpoint=pick,
        tier=tier,
        effort=effort,
        reason=f"selected {pick.worker_id} for tier={tier} effort={effort}",
        candidates=tuple(ordered),
    )
