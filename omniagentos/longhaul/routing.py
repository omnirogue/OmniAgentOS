"""Longhaul worker ranking: mechanical scoring of registry workers.

No per-account expansion (that's spawn's job). NO LLM. Registry read is best-effort;
missing/stale registry falls back to static order from config.

L-17: uses the real modelintel registry schema (list of model entries + top-level
ISO ``updated_at``). Epoch-zero / missing / >7-day-stale registries force static
fallback. Fabricated ``feedback_score`` blends are omitted unless a real explicit
feedback field is present on the entry.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

# Registry older than this is treated as stale → static fallback (L-17).
REGISTRY_FRESHNESS_TTL_SECONDS = 7 * 24 * 60 * 60


class Worker(TypedDict):
    """Ranked worker entry."""

    harness: str  # one of the supported subscription CLI harnesses
    model: str  # model name
    score: float  # computed score
    why: str  # explanation of score


def _parse_registry_updated_at(value: Any) -> float | None:
    """Parse registry ``updated_at`` into a unix timestamp.

    Real modelintel registries use ISO-8601 strings at the *top level*
    (e.g. ``2026-07-25T11:16:16Z``). Also accepts numeric unix seconds.
    Returns ``0.0`` for explicit epoch zero (always stale). Returns ``None``
    when the value is missing/unparseable (also treated as stale).
    """
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Explicit 0 / negative → epoch-zero / invalid → stale.
        return float(value) if value > 0 else 0.0
    text = str(value).strip()
    if not text:
        return None
    if text in {"0", "0.0"}:
        return 0.0
    # Numeric string
    try:
        num = float(text)
        return num if num > 0 else 0.0
    except ValueError:
        pass
    candidate = text.replace(" ", "T")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ts = dt.timestamp()
    return ts if ts > 0 else 0.0


def _registry_is_stale(updated_at_ts: float | None, *, now: float | None = None) -> bool:
    """True when updated_at is missing, epoch-zero, or older than the TTL."""
    if updated_at_ts is None:
        return True
    if updated_at_ts <= 0:
        return True
    clock = time.time() if now is None else now
    return (clock - updated_at_ts) > REGISTRY_FRESHNESS_TTL_SECONDS


def _explicit_feedback_score(model_data: dict[str, Any]) -> float | None:
    """Return a real feedback term only when the registry entry provides one.

    Accepted keys (first hit wins): ``feedback_score``, ``recent_success_rate``,
    ``success_rate``. Fabricated defaults are intentionally not returned so the
    blend can be omitted (L-17).
    """
    for key in ("feedback_score", "recent_success_rate", "success_rate"):
        value = model_data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, min(1.0, float(value)))
    return None


def rank_workers(registry_path: str, cfg: dict[str, Any], claude_capacity: int) -> list[Worker]:
    """Rank workers from registry using mechanical scoring.

    Args:
        registry_path: path to var/modelintel/registry.json
        cfg: loaded longhaul config
        claude_capacity: count of enabled, non-cooling claude accounts

    Returns:
        list of ranked workers [{harness, model, score, why}]

    Hard filters:
        - cli-claude: only opus/sonnet
        - cli-codex: only gpt-5.6-sol/terra
        - cli-grok / cli-gemini / cli-kimi: their canonical subscription models
        - Fable excluded by cfg.excluded_models
        - Order: claude first (if capacity>0), then non-Claude CLI fallbacks

    L-17 freshness: if the registry's top-level ``updated_at`` is missing,
    epoch-zero, or older than :data:`REGISTRY_FRESHNESS_TTL_SECONDS`, the
    registry scores are ignored and ``static_fallback_order`` is used.
    """
    workers: list[Worker] = []

    # Extract config parameters
    weights = cfg.get("weights", {"quality": 0.6, "speed": 0.25, "cost": 0.15})
    w_quality = weights.get("quality", 0.6)
    w_speed = weights.get("speed", 0.25)
    w_cost = weights.get("cost", 0.15)

    excluded_models = cfg.get("excluded_models", [])
    cross_harness_fallback = cfg.get("cross_harness_fallback", True)
    static_fallback = cfg.get("static_fallback_order", [])

    # Try to load registry
    registry_models: dict[str, dict[str, Any]] = {}
    registry_updated_at: float | None = None
    try:
        if registry_path and Path(registry_path).exists():
            with open(registry_path) as f:
                registry = json.load(f)
                if isinstance(registry, dict):
                    registry_updated_at = _parse_registry_updated_at(registry.get("updated_at"))
                    raw = registry.get("models", [])
                else:
                    raw = []
                # Production modelintel registry: models is a LIST of entries
                # with a "key". Fixtures / older shapes may use {key: entry}.
                if isinstance(raw, dict):
                    registry_models = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
                elif isinstance(raw, list):
                    registry_models = {
                        str(entry.get("key")): entry
                        for entry in raw
                        if isinstance(entry, dict) and entry.get("key")
                    }
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    # Registry key → (harness, session-spawnable model name). Claude CLI takes
    # family aliases; the other subscription CLIs take their canonical model
    # ids. Anything unmapped (fable, opus-4.8, …) is not spawnable by this lane.
    # `opus` resolves to Opus 5, so claude-opus-4.8 is deliberately absent —
    # mapping both to the same alias would enqueue two identical opus workers
    # (nothing downstream dedupes by spawn model).
    spawnable = {
        "claude-opus-5": ("cli-claude", "opus"),
        "opus": ("cli-claude", "opus"),
        "claude-sonnet-5": ("cli-claude", "sonnet"),
        "sonnet": ("cli-claude", "sonnet"),
        "gpt-5.6-sol": ("cli-codex", "gpt-5.6-sol"),
        "gpt-5.6-terra": ("cli-codex", "gpt-5.6-terra"),
        "grok-4.5": ("cli-grok", "grok-4.5"),
        "gemini-3.1-pro": ("cli-gemini", "gemini-3.1-pro"),
        "kimi-k3": ("cli-kimi", "kimi-k3"),
    }

    # Collect candidates from registry or fall back to static order
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    use_registry = bool(registry_models) and not _registry_is_stale(registry_updated_at)

    if use_registry:
        for reg_key, model_data in registry_models.items():
            mapped = spawnable.get(reg_key)
            if mapped is None or not isinstance(model_data, dict):
                continue
            harness, model_name = mapped
            if model_name in excluded_models or reg_key in excluded_models:
                continue
            if any(excl and excl in reg_key for excl in excluded_models):
                continue
            candidates.append((harness, model_name, model_data))
    else:
        # Stale / missing / epoch-zero registry → static fallback (L-17).
        for entry in static_fallback:
            if isinstance(entry, dict):
                harness = entry.get("harness", "")
                model = entry.get("model", "")
                if harness and model:
                    # Validate the exact harness/model pair. A typo must not
                    # fall through to the generic non-Claude process path.
                    if spawnable.get(model) == (harness, model) and model not in excluded_models:
                        candidates.append((harness, model, {}))

    # Score candidates
    for harness, model_name, model_data in candidates:
        # Real registry: capabilities.{domain}.score; fixtures may use a flat
        # scores dict. Default 0.5 when absent either way.
        caps = model_data.get("capabilities") or {}
        flat = model_data.get("scores") or {}

        def _cap(
            name: str,
            *,
            _caps: dict[str, Any] = caps,  # bind loop vars (ruff B023)
            _flat: dict[str, Any] = flat,
        ) -> float:
            value = _caps.get(name)
            if isinstance(value, dict):
                value = value.get("score")
            if value is None:
                value = _flat.get(name)
            if not isinstance(value, (int, float, str)):
                return 0.5
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.5

        coding_impl = _cap("coding-implementation")
        agentic_tool_use = _cap("agentic-tool-use")
        measured_latency_ms = model_data.get("measured_latency_ms")
        if not isinstance(measured_latency_ms, (int, float)) or measured_latency_ms <= 0:
            measured_latency_ms = 5000
        pricing = model_data.get("pricing") or {}
        pricing_per_token = pricing.get("per_token")
        if pricing_per_token is None:
            completion = pricing.get("completion_usd_per_m")
            pricing_per_token = (
                float(completion) / 1_000_000 if isinstance(completion, (int, float)) else 0.001
            )

        # Normalize latency (assume baseline 5000ms = 0.5, scale linearly)
        latency_norm = min(1.0, measured_latency_ms / 10000.0)

        # Normalize cost (assume baseline 0.001 = 0.5, scale linearly)
        cost_norm = min(1.0, pricing_per_token / 0.002)

        # Real feedback only when the entry actually carries it; otherwise omit
        # the blend so inert constants cannot pretend to be learned signal.
        feedback = _explicit_feedback_score(model_data)
        if feedback is None:
            quality = 0.6 * coding_impl + 0.4 * agentic_tool_use
            fb_note = "fb=omit"
        else:
            quality = 0.5 * coding_impl + 0.3 * agentic_tool_use + 0.2 * feedback
            fb_note = f"fb={feedback:.2f}"

        score = w_quality * quality + w_speed * (1.0 - latency_norm) - w_cost * cost_norm

        why = (
            f"{model_name} (impl={coding_impl:.2f}, tool_use={agentic_tool_use:.2f}, "
            f"{fb_note}, latency={measured_latency_ms}ms)"
        )
        if not use_registry:
            why += " [static_fallback]"

        workers.append(
            Worker(
                harness=harness,
                model=model_name,
                score=score,
                why=why,
            )
        )

    # Sort: Claude first when it has account capacity, then every enabled
    # non-Claude CLI. Stable sorting preserves the configured static order when
    # scores tie (all static entries use the same neutral score).
    claude_workers = [w for w in workers if w["harness"] == "cli-claude"]
    fallback_workers = [w for w in workers if w["harness"] != "cli-claude"]

    # Sort each class by score descending.
    claude_workers.sort(key=lambda w: w["score"], reverse=True)
    fallback_workers.sort(key=lambda w: w["score"], reverse=True)

    # Determine order based on claude_capacity
    result: list[Worker] = []
    if claude_capacity > 0:
        # Claude is available
        result.extend(claude_workers)
        if cross_harness_fallback:
            result.extend(fallback_workers)
    else:
        # No Claude capacity; a configured non-Claude subscription CLI is the
        # real fallback path.
        if cross_harness_fallback:
            result.extend(fallback_workers)
            result.extend(claude_workers)
        else:
            result.extend(claude_workers)

    return result
