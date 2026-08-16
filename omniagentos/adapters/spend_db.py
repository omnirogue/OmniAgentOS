"""Canonical identity for the production paid-provider spend ledger."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omniagentos.runtime_paths import resolve_sim_context_or_none

SPEND_DB_ENV = "OMNIAGENTOS_SPEND_DB"
PRODUCTION_SPEND_DB = Path(
    "/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3"
)


class SpendDbResolutionError(RuntimeError):
    """Paid spend cannot be accounted against the production ledger safely."""


def resolve_spend_db_path(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the one spend ledger, refusing every active simulation context.

    ``explicit`` exists only for dependency injection in tests/tools. Runtime
    consumers call this without it, making ``OMNIAGENTOS_SPEND_DB`` the sole
    override and the fixed production ledger the fallback.
    """

    values = os.environ if environ is None else environ
    try:
        simulation = resolve_sim_context_or_none(values)
    except Exception as exc:
        raise SpendDbResolutionError(
            f"simulation context is incoherent; refusing paid spend DB resolution: {exc}"
        ) from exc
    if simulation is not None:
        raise SpendDbResolutionError(
            f"simulation campaign {simulation.campaign!r} is active; paid calls may not use "
            "a simulation/campaign ledger"
        )

    if explicit is not None:
        return Path(explicit)
    configured = values.get(SPEND_DB_ENV)
    if configured and configured.strip():
        return Path(configured.strip())
    return PRODUCTION_SPEND_DB


__all__ = [
    "PRODUCTION_SPEND_DB",
    "SPEND_DB_ENV",
    "SpendDbResolutionError",
    "resolve_spend_db_path",
]
