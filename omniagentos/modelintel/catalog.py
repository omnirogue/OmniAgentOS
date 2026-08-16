"""Model catalog aggregator for the dashboard's model picker.

Reads the cascade ladder (``configs/cascade.yaml``) and the provider-account
health table (same ``claude_accounts`` store that ``GET /api/swarm/providers``
consumes — see :func:`omniagentos.accounts.service.list_accounts`) and fuses
them into a flat list the chat composer can render directly.

Every entry carries ``{id, label, provider, tier, available, lineage}`` —
``available`` is ``False`` when the provider has zero healthy accounts (no
enabled, non-rate-limited, non-error row), so a model whose only lane is down
is shown greyed-out rather than selectable.

The first entry is ALWAYS the auto-router synthetic:
``{"id": "auto", "label": "Auto — router decides", "provider": "router",
"tier": null, "available": true, "lineage": null}``.

Graceful degradation: a missing, empty, or malformed ``cascade.yaml`` still
returns the auto entry plus whatever could be parsed. Provider availability is
fail-closed: missing health data marks concrete providers unavailable, while a
health-query failure is raised so the caller can distinguish an outage from a
genuinely empty account store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import default_db_path, utc_now_iso

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter-to-provider mapping — mirrors the key→module table in
# ``omniagentos.adapters.registry._ADAPTERS`` but captures only the provider
# name (the part before the dash in each key). Kept as a local literal so this
# module never imports the adapter runtime modules at startup.
# ---------------------------------------------------------------------------

_ADAPTER_PROVIDER_MAP: dict[str, str] = {
    "cli-claude": "claude",
    "cli-codex": "codex",
    "cli-grok": "grok",
    "cli-gemini": "gemini",
    "cli-kimi": "kimi",
    "cli-qwen": "qwen",
}

# The first-class auto-router entry — ALWAYS the first item in the catalog.
AUTO_ENTRY: dict[str, Any] = {
    "id": "auto",
    "label": "Auto — router decides",
    "provider": "router",
    "tier": None,
    "available": True,
    "lineage": None,
}


# ---------------------------------------------------------------------------
# Cascade ladder loading
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def default_cascade_path() -> Path:
    """Canonical cascade.yaml path, anchored to the repo root."""
    return _repo_root() / "configs" / "cascade.yaml"


def load_cascade_ladder(path: Path | None = None) -> list[dict[str, Any]]:
    """Parse the ``ladder`` key from ``configs/cascade.yaml``.

    Returns an empty list on any failure (missing file, YAML parse error,
    wrong shape) — the caller's contract is "best-effort ladder, never
    raises". Each entry is a raw dict from the YAML loader; validation and
    type coercion happen in :func:`build_model_catalog`.
    """
    target = path or default_cascade_path()
    if not target.is_file():
        LOG.debug("cascade.yaml not found at %s — returning empty ladder", target)
        return []
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        LOG.warning("cascade.yaml parse failed: %s", exc)
        return []
    if not isinstance(raw, dict):
        LOG.warning("cascade.yaml root is not a mapping — returning empty ladder")
        return []
    ladder = raw.get("ladder")
    if not isinstance(ladder, list):
        LOG.warning("cascade.yaml has no 'ladder' list — returning empty ladder")
        return []
    return [entry for entry in ladder if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Provider health
# ---------------------------------------------------------------------------


def _account_is_healthy(account: dict[str, Any]) -> bool:
    """An account counts as healthy when it is enabled, not errored, not rate
    limited, not under an active pause, and not under an active cooldown.

    Matches the spirit of ``accounts.service.AVAILABLE_PREDICATE`` (the single
    rotation-authority filter) without re-querying the DB — the accounts are
    already fetched by :func:`omniagentos.accounts.service.list_accounts` and
    carry their ``enabled``, ``status``, ``paused``, and ``cooldown_until``
    fields in Python. Weekly quota and ``max_inflight`` are NOT read here —
    those live in ``AccountPool`` limit_state (a different data source and
    contract), not on the accounts row."""
    if not account.get("enabled"):
        return False
    status = str(account.get("status") or "unknown")
    if status in ("error", "rate_limited"):
        return False
    if account.get("paused"):
        return False
    cooldown = account.get("cooldown_until")
    if cooldown and str(cooldown) > utc_now_iso():
        return False
    return True


def build_provider_health(
    db_path: str | None = None,
) -> dict[str, bool]:
    """Provider → available flag from the accounts table.

    A provider with at least one healthy account is ``available``; a provider
    where every account is disabled/errored/rate-limited is ``False``.
    Providers absent from this mapping have unknown health and callers must
    treat them as unavailable.

    Reads the same ``claude_accounts`` store ``GET /api/swarm/providers``
    reads; never imports runtime-heavy modules. Import and query failures are
    deliberately allowed to propagate so an unavailable health store cannot
    masquerade as an empty, healthy result.
    """
    from omniagentos.accounts.service import list_accounts

    effective_path = db_path or default_db_path()
    accounts = list_accounts(effective_path)

    by_provider: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        provider = str(account.get("provider") or "claude")
        by_provider.setdefault(provider, []).append(account)

    health: dict[str, bool] = {}
    for provider, provider_accounts in by_provider.items():
        health[provider] = any(_account_is_healthy(a) for a in provider_accounts)
    return health


# ---------------------------------------------------------------------------
# Catalog builder
# ---------------------------------------------------------------------------


def _provider_from_adapter(adapter: str) -> str:
    """Map a cascade adapter key (``cli-gemini``) to its provider name."""
    return _ADAPTER_PROVIDER_MAP.get(adapter, adapter)


def _extract_lineage(model_name: str) -> str | None:
    """Derive a lineage/family label from a model identifier.

    ``gemini-3.1`` → ``gemini``; ``grok-4.5`` → ``grok``;
    ``gpt-5.6-sol`` → ``gpt``. Returns ``None`` if the name is empty."""
    if not model_name:
        return None
    parts = model_name.split("-")
    return parts[0] if parts else None


def _extract_tier(entry: dict[str, Any], index: int) -> int | None:
    """Parse the tier number from a ladder entry's ``name`` field.

    ``tier0-gemini-coder`` → ``0``; ``tier3-sol`` → ``3``. Falls back to the
    positional index when the name doesn't follow the convention (or is absent).
    """
    name = str(entry.get("name") or "")
    if name.startswith("tier") and len(name) > 4 and name[4].isdigit():
        try:
            end = 5
            while end < len(name) and name[end].isdigit():
                end += 1
            return int(name[4:end])
        except (ValueError, IndexError):
            pass
    return index


def build_model_catalog(
    cascade_path: Path | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Aggregate the full model catalog for the dashboard picker.

    Combines the cascade ladder with provider-account health. Each cascade
    entry becomes one row; providers with no healthy accounts are marked
    ``available: false``. The auto-router entry is always first.

    Callers may override ``cascade_path`` or ``db_path`` for testing; both
    default to the canonical production locations.
    """
    ladder = load_cascade_ladder(cascade_path)
    provider_health = build_provider_health(db_path)

    models: list[dict[str, Any]] = [dict(AUTO_ENTRY)]

    for index, entry in enumerate(ladder):
        adapter = str(entry.get("adapter") or "")
        model_name = str(entry.get("model") or "")
        effort = str(entry.get("effort") or "")

        provider = _provider_from_adapter(adapter)

        # Absence is unknown health, not evidence of availability. Concrete
        # models therefore fail closed unless a healthy account proves the
        # provider selectable.
        available = provider_health.get(provider, False)

        lineage = _extract_lineage(model_name)

        effort_suffix = f" ({effort})" if effort else ""
        label = f"{model_name}{effort_suffix}" if model_name else (adapter or f"tier-{index}")

        models.append(
            {
                "id": str(entry.get("name") or f"model-{index}"),
                "label": label,
                "provider": provider,
                "tier": _extract_tier(entry, index),
                "available": available,
                "lineage": lineage,
            }
        )

    return {
        "models": models,
        "updated_at": utc_now_iso(),
    }
