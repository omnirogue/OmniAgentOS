"""Price MEASURED tokens into an ESTIMATE that can never be mistaken for a measurement.

The problem this exists to solve
--------------------------------
``codex exec --json`` reports exact token counts and no cost field. The honest
record of that is ``sessions.cost_usd IS NULL`` — unpriced, unknown (see
:mod:`omniagentos.swarm.usage_capture`). But an unpriced session also accrued
**nothing** toward its run's dollar cap: every gate read ``known_cost_usd = 0``,
so a codex-only swarm ran against a ceiling it could never consume.

Earlier attempts closed that hole by writing a computed number into ``cost_usd``.
That destroyed the invariant the rest of the system depends on::

    cost_usd IS NULL  = unpriced / unknown
    cost_usd = 0.0    = the provider reported this run was genuinely free

Those two must stay distinguishable, so nothing this module returns is ever
written to ``cost_usd``. It produces an estimate persisted in its OWN column
(``sessions.cost_estimate_usd``, migration 118) beside its provenance
(``sessions.cost_estimate_source``). Cap accrual reads the estimate; every
truth-telling surface keeps reading the honest NULL.

Why the discriminator is derived, not stored
--------------------------------------------
``cost_quality`` is a pure function of the two stored numbers::

    cost_usd IS NOT NULL          -> "exact"      (including a genuine 0.0)
    cost_estimate_usd IS NOT NULL -> "estimated"
    neither                       -> "unknown"

A stored discriminator can drift out of step with the values it describes —
exactly the defect class this item is about — so it is computed at every read.
:func:`cost_quality` is that single definition, and it uses the same vocabulary
as ``provider_call_usage.cost_quality`` so one word means one thing across the
whole ledger.

What is and is not invented
---------------------------
The only price source is the model-intelligence registry
(``var/modelintel/registry.json``), whose per-model ``prompt_usd_per_m`` /
``completion_usd_per_m`` come from a live provider fetch and carry their own
``as_of`` stamp. If the registry is missing, unreadable, has no entry for the
model, or publishes no rate for it, this returns ``None`` — unpriceable stays
unpriced. There is no default rate, no fallback table, and no "close enough"
model substitution: inventing a rate is the failure mode this module exists to
avoid, not a convenience it may fall back on.

Cached / cache-write input tokens are billed at the full prompt rate because the
registry publishes no cache rate. That makes the result a deliberate UPPER
BOUND, which is the safe direction for a spend cap.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

#: The three states one row's dollar figure can be in.
QUALITY_EXACT = "exact"
QUALITY_ESTIMATED = "estimated"
QUALITY_UNKNOWN = "unknown"


def cost_quality(cost_usd: float | None, cost_estimate_usd: float | None = None) -> str:
    """Classify one row's dollar figure. Derived — it cannot contradict the data.

    An exact price wins even when an estimate is also stored: the estimate is
    then stale bookkeeping, not a competing claim.
    """
    if cost_usd is not None:
        return QUALITY_EXACT
    if cost_estimate_usd is not None:
        return QUALITY_ESTIMATED
    return QUALITY_UNKNOWN


@dataclass(frozen=True)
class TokenCostEstimate:
    """An upper-bound dollar estimate plus the evidence it was derived from."""

    cost_usd: float
    #: Human-readable provenance, e.g. ``modelintel:gpt-5.6-sol@2026-08-04T11:15:05Z``.
    #: Persisted so an estimate is never an anonymous number.
    source: str
    model_key: str


def _token_count(value: Any) -> int | None:
    # bool is an int subclass; a stray True must never become 1 token.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rate = float(value)
    return rate if math.isfinite(rate) and rate >= 0 else None


_CACHE_LOCK = threading.Lock()
#: path -> (stat signature, parsed payload). The registry is rewritten daily by
#: the modelintel updater, so the signature (mtime_ns, size) invalidates the
#: cache on rewrite without re-reading the file on the hot path.
_REGISTRY_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_ALIAS_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _load_registry(path: Path) -> dict[str, Any] | None:
    signature = _signature(path)
    if signature is None:
        return None
    key = str(path)
    with _CACHE_LOCK:
        cached = _REGISTRY_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    with _CACHE_LOCK:
        _REGISTRY_CACHE[key] = (signature, payload)
    return payload


def _alias_index(config_file: Path) -> dict[str, str]:
    """normalized alias -> canonical registry key, or ``{}`` when unavailable.

    Imported lazily: this module is reached from :mod:`omniagentos.sessions.dal`,
    a lower layer that must not pay a YAML/pydantic import at module load.
    """
    signature = _signature(config_file)
    if signature is None:
        return {}
    key = str(config_file)
    with _CACHE_LOCK:
        cached = _ALIAS_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    try:
        from omniagentos.modelintel.config import build_alias_index, load_config

        index = build_alias_index(load_config(config_file))
    except Exception:  # noqa: BLE001 -- a broken config must not fail a spend read
        LOG.debug("model alias index unavailable at %s", config_file, exc_info=True)
        return {}
    with _CACHE_LOCK:
        _ALIAS_CACHE[key] = (signature, index)
    return index


def _normalized(name: str) -> str:
    try:
        from omniagentos.modelintel.config import normalize_model_name

        return normalize_model_name(name)
    except Exception:  # noqa: BLE001 -- degrade to a plain lowercase match
        return "-".join(name.split("(", 1)[0].strip().lower().split())


def _registry_entry(
    payload: dict[str, Any], model: str, aliases: dict[str, str]
) -> dict[str, Any] | None:
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    normalized = _normalized(model)
    canonical = _normalized(aliases.get(normalized, normalized))
    wanted = {normalized, canonical}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        entry_key = entry.get("key")
        if isinstance(entry_key, str) and _normalized(entry_key) in wanted:
            return entry
    return None


def estimate_token_cost(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    cache_write_input_tokens: int | None = None,
    registry_file: Path | str | None = None,
    config_file: Path | str | None = None,
) -> TokenCostEstimate | None:
    """Upper-bound dollars for measured tokens, or ``None`` when unpriceable.

    ``None`` is the honest answer for a missing model name, missing token counts,
    a missing/unreadable registry, an unlisted model, or a listed model with no
    published rate. Callers persist ``None`` as SQL NULL and the row stays
    ``cost_quality='unknown'`` — never a manufactured zero.

    Never raises: pricing telemetry must not fail the run that produced it.
    """
    prompt_tokens = _token_count(input_tokens)
    completion_tokens = _token_count(output_tokens)
    if prompt_tokens is None and completion_tokens is None:
        return None
    prompt_tokens = prompt_tokens or 0
    completion_tokens = completion_tokens or 0
    cache_tokens = _token_count(cache_write_input_tokens) or 0
    if not isinstance(model, str) or not model.strip():
        return None

    try:
        from omniagentos.modelintel.config import config_path, registry_path

        registry = Path(registry_file) if registry_file is not None else registry_path()
        config = Path(config_file) if config_file is not None else config_path()
    except Exception:  # noqa: BLE001 -- no registry locator, no estimate
        LOG.debug("model registry locator unavailable", exc_info=True)
        return None

    payload = _load_registry(registry)
    if payload is None:
        return None
    entry = _registry_entry(payload, model, _alias_index(config))
    if entry is None:
        return None
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt_rate = _rate(pricing.get("prompt_usd_per_m"))
    completion_rate = _rate(pricing.get("completion_usd_per_m"))
    if prompt_rate is None or completion_rate is None:
        # Half a price table would need the other half substituted, and
        # substituting is inventing. Stay unknown.
        return None

    cost = (
        (prompt_tokens + cache_tokens) * prompt_rate + completion_tokens * completion_rate
    ) / 1_000_000.0
    if not math.isfinite(cost) or cost < 0:
        return None
    model_key = str(entry.get("key") or model)
    as_of = pricing.get("as_of")
    stamp = f"@{as_of}" if isinstance(as_of, str) and as_of else ""
    return TokenCostEstimate(
        cost_usd=cost,
        source=f"modelintel:{model_key}{stamp}",
        model_key=model_key,
    )


__all__ = [
    "QUALITY_ESTIMATED",
    "QUALITY_EXACT",
    "QUALITY_UNKNOWN",
    "TokenCostEstimate",
    "cost_quality",
    "estimate_token_cost",
]
