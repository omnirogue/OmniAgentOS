"""Dispatch-layer helpers for per-run provider allowlists (D5).

Keeps the swarm/dispatch entrypoints free of ad-hoc allowlist parsing: params
may carry ``allowed_providers`` as a list of strings; this module normalises
that into a form the shared predicate and planner fallback chain accept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omniagentos.providers.constraints import (
    ProviderNotAllowed,
    assert_provider_allowed,
    filter_allowed,
    normalize_provider,
)


def allowed_providers_from_params(params: Mapping[str, Any] | None) -> list[str] | None:
    """Extract ``allowed_providers`` from a swarm/dispatch params mapping.

    Returns ``None`` when absent or empty (no constraint). Returns a list of
    canonical provider names otherwise.
    """
    if not params:
        return None
    raw = params.get("allowed_providers")
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    elif isinstance(raw, Sequence):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    else:
        return None
    if not parts:
        return None
    return [normalize_provider(p) for p in parts]


def filter_dispatch_candidates(
    candidates: Sequence[Any],
    params: Mapping[str, Any] | None,
    *,
    stage: str = "swarm_dispatch",
) -> list[Any]:
    """Apply the shared allowlist predicate at a dispatch rotation site."""
    return filter_allowed(
        candidates,
        allowed_providers_from_params(params),
        stage=stage,
    )


def assert_dispatch_provider(
    provider: str | None,
    params: Mapping[str, Any] | None,
    *,
    stage: str = "swarm_dispatch_spawn",
) -> None:
    """Fail closed (or shadow-log) when a chosen provider violates the pin."""
    assert_provider_allowed(
        provider,
        allowed_providers_from_params(params),
        stage=stage,
    )


__all__ = [
    "ProviderNotAllowed",
    "allowed_providers_from_params",
    "assert_dispatch_provider",
    "filter_dispatch_candidates",
]
