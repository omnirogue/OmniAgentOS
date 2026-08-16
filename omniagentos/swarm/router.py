"""Adaptive swarm routing (WP5b): the real ``SwarmRouterProto`` implementation.

The route chain, in order:

1. **tier -> modelintel mode**: the scheduler's tier ladder rung (simple /
   standard / complex) maps to a modelintel routing mode (ultrafast /
   fusionbuild / ultrabuild) + difficulty/effort hints.
2. **learned start tier**: on a FIRST attempt (no ``current_tier`` /
   retries / timeouts recorded yet) the start rung may be adjusted by the
   Beta-Binomial hierarchical learner over task class ``swarm:<complexity>``
   -> all-swarm -> global prior (plan A7 idiom; ``routing.learn`` shrinkage
   with 7-14 day decay, samples mined from the durable ``swarm_attempts``
   history). Mid-ladder routes echo the scheduler's tier untouched -- the
   escalation ladder stays scheduler-owned.
3. **candidate ranking**: eligible coder agents from the fusion rankings file
   scored with the mechanical fit formula (the same weights modelintel's
   fallback scorer uses); a FRESH capability digest adds a mode-weighted
   domain-score bonus. Digest stale/absent -> pure mechanical ranking (the
   documented fallback). No rankings at all -> a built-in claude candidate,
   so a missing file can never brick the swarm.
4. **risk_class pin**: external/deploy/destructive tasks are pinned to the
   claude provider HERE, and ``provider_exec``'s hard-coded deny remains the
   enforcement backstop a mis-tag cannot slip past.
5. **lane floors** (R1.1): candidates restricted to a quality floor list based
   on tier (complex) or risk_class (high_risk). High_risk beats complex when
   both apply. No routable floor candidate → requeue (never downgrade).
6. **category pins** (R1.2): task discipline pins to a specific model if
   present in config; pins beat ranking but respect account availability
   (pinned but cooling → requeue not substitute). Pins don't override high_risk
   floors. When ``router.semantic_pins`` is on, a task with no explicit
   discipline pin gets a SEMANTIC fallback (PKG-SEMANTIC-PINS): its
   title+description text is classified with the shared local semantic router
   and, if the verdict names a pin category, takes the SAME restrict path.
7. **model ladder**: quality failures advance the configured model sequence;
   risk, floor, and category restrictions always take precedence.
8. **pressure overflow** (R1.5): when tier=complex and ALL floor providers
   report pressure ≥threshold, extend (not replace) candidate list with
   overflow models. Overflow never applies to high_risk.
9. **provider_pressure filter**: providers whose enabled accounts are ALL
   cooling are dropped; the rest are deprioritized by
   ``limit_state.provider_pressure`` (pre-429 backpressure-as-routing-metric).
10. **account pick**: ``limit_state.reserve_account`` atomically claims one
   inflight slot on the chosen provider; the reservation id rides the
   ``RouteDecision`` so the spawner can convert/release it. A provider with
   no configured accounts routes with the default CLI login.

Returns ``None`` only when NO candidate provider has capacity -- the
scheduler then parks the run in bounded cooldown backoff. Emits a
``swarm.event provider_switched`` row whenever the pick changes provider
relative to the task's previous attempt.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path
from omniagentos.routing.cascade import CascadeTier
from omniagentos.routing.learn import (
    GLOBAL_PRIOR_RATE,
    SHRINKAGE_K,
    decayed_tier_counts,
    recommend_start_tier_shrunk,
)
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.swarm.contracts import ACTION_PROVIDER_SWITCHED, SwarmEmitter
from omniagentos.swarm.provider_exec import DENIED_RISK_CLASSES
from omniagentos.swarm.scheduler import TIER_LADDER, RouteDecision

LOG = logging.getLogger(__name__)

# A7's knob name, honored here too (learn.py's recommend_start_tier default).
ROUTE_LEARN_MIN_SAMPLES_ENV = "OMNIAGENTOS_ROUTE_LEARN_MIN_SAMPLES"
_DEFAULT_LEARN_MIN_SAMPLES = 5
_DEFAULT_PROVIDER_HEALTH_MAX_AGE_HOURS = 36.0
# Package anchor on purpose (env_keys=(), environ={}): the provider-sentinel
# WRITES this snapshot file-anchored under <repo>/var, so following
# OMNIAGENTOS_VAR (runtime under the launcher) would read a tree the
# sentinel never writes — a silent fail-open for the health filter (review B1).
# environ={} keeps this import-time evaluation off simulation state (B2).
_DEFAULT_PROVIDER_HEALTH_PATH = resolve_var_root(
    env_keys=(), environ={}, leaf=("provider-health.json",)
)


def healthy_providers_from_snapshot(
    path: str | Path = _DEFAULT_PROVIDER_HEALTH_PATH,
    *,
    now: Callable[[], datetime] | None = None,
    max_age_hours: float = _DEFAULT_PROVIDER_HEALTH_MAX_AGE_HOURS,
) -> set[str] | None:
    """Return providers that passed the latest doctor snapshot.

    ``None`` means health data is unavailable (missing, malformed, or stale),
    so callers can use the documented graceful fallback.  A valid snapshot is
    fail-closed: every doctor entry for a provider must have ``ok: true``.
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            return None
        stamp = _parse_iso_ts(payload.get("ts"))
        if stamp is None:
            return None
        current = (now or (lambda: datetime.now(UTC)))()
        age_hours = (current - stamp).total_seconds() / 3600.0
        if age_hours < 0 or age_hours > max_age_hours:
            return None
        results = payload.get("results")
        if not isinstance(results, Mapping):
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None

    states: dict[str, list[bool]] = {}
    for key, status in results.items():
        provider = status.get("provider") if isinstance(status, Mapping) else None
        provider_name = str(provider or str(key).split(":", 1)[0]).strip().lower()
        if provider_name:
            states.setdefault(provider_name, []).append(
                isinstance(status, Mapping) and status.get("ok") is True
            )
    return {provider for provider, checks in states.items() if checks and all(checks)}


# scheduler tier rung -> (modelintel mode, difficulty hint, effort hint)
TIER_TO_MODE: dict[str, tuple[str, str, str]] = {
    "simple": ("ultrafast", "easy", "low"),
    "standard": ("fusionbuild", "moderate", "medium"),
    "complex": ("ultrabuild", "hard", "high"),
}

# The reasoning-effort vocabulary (mirrors modelintel.router.EFFORTS; kept
# local so config validation never needs the modelintel import at load time).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")

# Digest domain-score weights per mode (the digest bonus term). Domains are
# the fusion digest's own vocabulary (model-intel.json ``domains``).
MODE_DOMAIN_WEIGHTS: dict[str, dict[str, float]] = {
    "ultrafast": {
        # Routing optimizes for quality and wall-clock speed only. Price is
        # deliberately not a term here: it is recorded as run telemetry but
        # never selects a model. The 0.10 that cost-efficiency used to carry
        # folded into speed.
        "speed": 0.55,
        "coding-implementation": 0.30,
        "agentic-tool-use": 0.15,
    },
    "fusionbuild": {
        "coding-implementation": 0.40,
        "agentic-tool-use": 0.25,
        "speed": 0.20,
        "debugging": 0.15,
    },
    "ultrabuild": {
        "coding-architecture": 0.35,
        "coding-implementation": 0.30,
        "debugging": 0.20,
        "agentic-tool-use": 0.15,
    },
}

# Ladder used by the risk pin / no-rankings fallback (DefaultSwarmRouter's map).
_FALLBACK_CLAUDE_MODELS = {"simple": "sonnet", "standard": "sonnet", "complex": "opus"}

# The built-in fallback + short-name model keys map to the canonical modelintel
# keys the ``lane_floors`` / ``category_pins`` config uses, so floor/pin
# membership matches a claude fallback candidate (``opus``/``sonnet`` from
# ``_FALLBACK_CLAUDE_MODELS``) against a floor like ``claude-opus-5`` (finding
# #5 — without this a risk-pinned task with no claude coder in the rankings
# misses the floor and requeues forever). Kept as a small local map so routing
# never pays a modelintel yaml parse on the hot path; the modelintel alias index
# (``build_alias_index``) is the authoritative superset if heavier resolution is
# ever needed.
_MODEL_KEY_ALIASES: dict[str, str] = {
    # The claude CLI family alias `opus` resolves to Opus 5, NOT 4.8 — a floor
    # pinned at claude-opus-4.8 is no longer satisfiable by the `opus` fallback
    # (4.8 is reachable only via an explicit `--model claude-opus-4-8`).
    "opus": "claude-opus-5",
    "opus-5": "claude-opus-5",
    "claude-opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4.8",
    "claude-opus-4-8": "claude-opus-4.8",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-5",
    "sonnet-5": "claude-sonnet-5",
}


def _normalize_model_key(model: str) -> str:
    """Canonicalize a model key for floor/pin membership: lowercase, then map a
    built-in fallback / short name to its canonical modelintel key."""
    key = str(model or "").strip().lower()
    return _MODEL_KEY_ALIASES.get(key, key)


def default_model_lineage_index() -> dict[str, str]:
    """normalized model key/alias -> lineage, from the modelintel registry
    (``configs/modelintel.yaml``). Overflow candidates resolve their executing
    provider through this map + ``router.lineage_providers``, exactly like ranked
    candidates resolve theirs from the capability digest lineage — never a
    hardcoded literal (finding #3). Any failure -> {} (routing must never break
    on a missing/broken registry). Built once and cached on the router."""
    try:
        from omniagentos.modelintel.config import load_config, normalize_model_name

        cfg = load_config()
    except Exception:  # noqa: BLE001 -- a missing registry must not break routing.
        LOG.debug("model lineage index unavailable", exc_info=True)
        return {}
    index: dict[str, str] = {}
    for model in getattr(cfg, "models", None) or []:
        lineage = str(getattr(model, "lineage", "") or "").strip().lower()
        if not lineage:
            continue
        try:
            index[normalize_model_name(str(model.key))] = lineage
            for alias in getattr(model, "aliases", None) or []:
                index[normalize_model_name(str(alias))] = lineage
        except Exception:  # noqa: BLE001
            continue
    return index


# The swarm tier ladder as CascadeTier rungs for the shrinkage learner
# (cost_rank mirrors relative attempt cost: 15/30/60-minute timeout tiers).
SWARM_TIER_LADDER: tuple[CascadeTier, ...] = tuple(
    CascadeTier(name=name, adapter="swarm", cost_rank=float(rank))
    for rank, name in enumerate(TIER_LADDER, start=1)
)

_DEFAULT_DIGEST_MAX_AGE_HOURS = 24.0
_DEFAULT_PRESSURE_WEIGHT = 1.0
_DEFAULT_DIGEST_WEIGHT = 0.5

# Outcomes that count as learner samples: completed = win; review_denied and
# timeout = the tier was too weak (exactly what start-tier learning is for).
# rate_limited / killed / crashed / auth_failed are capacity or infra noise,
# not evidence about the tier.
_LEARN_WIN_REASONS = frozenset({"completed"})
_LEARN_LOSS_REASONS = frozenset({"review_denied", "timeout"})


def swarm_task_class(complexity: str) -> str:
    """The learner's task-class name for a swarm task (plan WP5b idiom)."""
    return f"swarm:{complexity}"


def learn_min_samples() -> int:
    """Minimum decayed leaf samples before the learner may DOWN-tier a start
    hint (starting higher is always allowed -- a misrouted-down task fails
    and re-runs, recreating the double-work A7 kills)."""
    raw = os.environ.get(ROUTE_LEARN_MIN_SAMPLES_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_LEARN_MIN_SAMPLES
    return value if value > 0 else _DEFAULT_LEARN_MIN_SAMPLES


def tier_timeout_minutes(config: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Per-tier attempt timeout minutes from configs/swarm.yaml
    (``tier_timeout_minutes``), falling back to the scheduler's 15/30/60."""
    from omniagentos.routing.limit_state import load_swarm_config
    from omniagentos.swarm.scheduler import DEFAULT_TIMEOUT_MINUTES

    resolved = dict(DEFAULT_TIMEOUT_MINUTES)
    section = (config if config is not None else load_swarm_config()).get("tier_timeout_minutes")
    if isinstance(section, Mapping):
        for tier, minutes in section.items():
            try:
                value = float(minutes)
            except (TypeError, ValueError):
                continue
            if str(tier) in resolved and value > 0:
                resolved[str(tier)] = value
    return resolved


def lineage_provider_map(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The configs/swarm.yaml ``router.lineage_providers`` map (lineage ->
    executing CLI provider); unknown lineages map to themselves."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved: dict[str, str] = {}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        lineages = router_section.get("lineage_providers")
        if isinstance(lineages, Mapping):
            for lineage, provider in lineages.items():
                if isinstance(lineage, str) and isinstance(provider, str):
                    resolved[lineage.strip().lower()] = provider.strip().lower()
    return resolved


def effort_by_tier_map(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The configs/swarm.yaml ``router.effort_by_tier`` map (tier rung ->
    reasoning effort), defaulted from ``TIER_TO_MODE``'s effort hints.
    Unknown tiers and out-of-vocabulary efforts are ignored."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved = {tier: TIER_TO_MODE[tier][2] for tier in TIER_LADDER}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("effort_by_tier")
        if isinstance(section, Mapping):
            for tier, effort in section.items():
                tier_name = str(tier).strip().lower()
                effort_name = str(effort).strip().lower()
                if tier_name in resolved and effort_name in EFFORT_LEVELS:
                    resolved[tier_name] = effort_name
    return resolved


def effort_overrides_map(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The configs/swarm.yaml ``router.effort_overrides`` map (model ->
    reasoning effort); an override beats the tier map for that exact model.
    Out-of-vocabulary efforts are ignored."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved: dict[str, str] = {}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("effort_overrides")
        if isinstance(section, Mapping):
            for model, effort in section.items():
                effort_name = str(effort).strip().lower()
                if isinstance(model, str) and model.strip() and effort_name in EFFORT_LEVELS:
                    resolved[model.strip().lower()] = effort_name
    return resolved


def effort_overrides_by_tier_map(
    config: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, str]]:
    """The configs/swarm.yaml ``router.effort_overrides_by_tier`` map
    (tier rung -> {model -> reasoning effort}). ADDITIVE to the flat
    ``effort_overrides``: a tier-scoped entry applies ONLY when the decided tier
    matches (finding #4 — the flat map keeps its pre-existing any-tier
    semantics, so a STANDARD route to that model is not forced to xhigh).
    Unknown tiers, blank models, and out-of-vocabulary efforts are ignored."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved: dict[str, dict[str, str]] = {}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("effort_overrides_by_tier")
        if isinstance(section, Mapping):
            for tier, models in section.items():
                tier_name = str(tier).strip().lower()
                if tier_name not in TIER_LADDER or not isinstance(models, Mapping):
                    continue
                per_model: dict[str, str] = {}
                for model, effort in models.items():
                    effort_name = str(effort).strip().lower()
                    if isinstance(model, str) and model.strip() and effort_name in EFFORT_LEVELS:
                        per_model[model.strip().lower()] = effort_name
                if per_model:
                    resolved[tier_name] = per_model
    return resolved


def lane_floors_map(config: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
    """The configs/swarm.yaml ``router.lane_floors`` map (floor scope ->
    [model keys]). A floor restricts candidates by tier (complex) or risk_class
    (high_risk). high_risk takes precedence when both apply. Missing config
    or empty scope → no restrictions (backward compatible). Returns a map
    where each key is a scope and value is a list of model keys (normalized
    to lowercase)."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved: dict[str, list[str]] = {}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("lane_floors")
        if isinstance(section, Mapping):
            for scope, models in section.items():
                scope_name = str(scope).strip().lower()
                if isinstance(models, list):
                    model_list = [
                        str(m).strip().lower() for m in models if isinstance(m, str) and m.strip()
                    ]
                    if model_list:
                        resolved[scope_name] = model_list
    return resolved


def model_ladder_list(config: Mapping[str, Any] | None = None) -> list[str]:
    """Ordered ``router.model_ladder`` retry/escalation models."""
    from omniagentos.routing.limit_state import load_swarm_config

    router_section = (config if config is not None else load_swarm_config()).get("router")
    if not isinstance(router_section, Mapping):
        return []
    entries = router_section.get("model_ladder")
    if not isinstance(entries, list):
        return []
    return [model.strip().lower() for model in entries if isinstance(model, str) and model.strip()]


def category_pins_map(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The configs/swarm.yaml ``router.category_pins`` map (discipline ->
    pinned model). A task's discipline field pins to a model if present in
    this map. Pins beat generic ranking but respect account availability.
    Returns a map where keys are discipline strings and values are model keys
    (normalized to lowercase)."""
    from omniagentos.routing.limit_state import load_swarm_config

    resolved: dict[str, str] = {}
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("category_pins")
        if isinstance(section, Mapping):
            for discipline, model in section.items():
                disc_str = str(discipline).strip().lower()
                model_str = str(model).strip().lower()
                if disc_str and model_str:
                    resolved[disc_str] = model_str
    return resolved


def semantic_pins_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """The configs/swarm.yaml ``router.semantic_pins`` flag (default ``False``).

    When truthy the SwarmRouter gives ``router.category_pins`` a SEMANTIC fallback
    (PKG-SEMANTIC-PINS): a task with no explicit ``discipline`` pin match is
    classified from its title+description text and, if the verdict names a
    configured pin category, restricted to that pin. The flag lives here because
    it is a router POLICY; the classifier's route seeds live in
    configs/dispatch.yaml with the other semantic-router utterances."""
    from omniagentos.routing.limit_state import load_swarm_config

    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        return bool(router_section.get("semantic_pins", False))
    return False


def _default_category_classifier(text: str) -> tuple[str, float] | None:
    """Lazy default seam for the semantic pin classifier: imports the dispatch
    classifier only on first use (so importing the router never pulls the
    semantic-router machinery). ``classify_category`` itself is already
    exception-proof and returns ``None`` on any degradation; this wrapper adds
    an import guard so even a broken import degrades to no-pin."""
    try:
        from omniagentos.dispatch.categories import classify_category

        return classify_category(text)
    except Exception:  # noqa: BLE001 -- an unavailable classifier must never break a route.
        LOG.debug("semantic category classifier unavailable", exc_info=True)
        return None


def overflow_config_map(
    config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, list[str]], float]:
    """The configs/swarm.yaml ``router.overflow`` map (tier -> [model keys])
    and pressure_threshold. Overflow extends (not replaces) candidates when
    all floor providers report pressure ≥ threshold. Returns (overflow_map,
    pressure_threshold) where overflow_map keys are tier names and values
    are model lists, and threshold is a float in [0.0, 1.0]. Missing config
    → ({}, 1.0) (disabled by default)."""
    from omniagentos.routing.limit_state import load_swarm_config

    overflow_map: dict[str, list[str]] = {}
    pressure_threshold = 1.0
    router_section = (config if config is not None else load_swarm_config()).get("router")
    if isinstance(router_section, Mapping):
        section = router_section.get("overflow")
        if isinstance(section, Mapping):
            # Extract tier→models mappings (all keys except pressure_threshold)
            for key, value in section.items():
                if key == "pressure_threshold":
                    try:
                        threshold_val = float(value)
                        if 0.0 <= threshold_val <= 1.0:
                            pressure_threshold = threshold_val
                    except (TypeError, ValueError):
                        pass
                else:
                    tier_name = str(key).strip().lower()
                    if isinstance(value, list):
                        model_list = [
                            str(m).strip().lower()
                            for m in value
                            if isinstance(m, str) and m.strip()
                        ]
                        if model_list:
                            overflow_map[tier_name] = model_list
    return overflow_map, pressure_threshold


def _parse_extra_candidates(entries: Any) -> list[dict[str, Any]]:
    """Structural validation for ``router.extra_candidates`` entries — shared by
    the config loader and the constructor-injection path so both apply the same
    rules. Each valid entry becomes a normalized dict
    ``{"model", "tier_ceiling", "score", "score_by_tier"}``. Malformed entries
    (missing ``model``, or ``tier_ceiling`` not in {simple, standard} — never
    complex) are warn+skipped, never raised. SEMANTIC validation (the model
    resolves to a lineage) happens at synthesis time where the lineage index
    lives, so an unknown registry key is also warn+skipped rather than routed to
    a nonexistent provider."""
    resolved: list[dict[str, Any]] = []
    if not isinstance(entries, list | tuple):
        return resolved
    for entry in entries:
        if not isinstance(entry, Mapping):
            LOG.warning("extra_candidate entry is not a mapping; skipping")
            continue
        model = str(entry.get("model") or "").strip().lower()
        if not model:
            LOG.warning("extra_candidate entry missing 'model'; skipping")
            continue
        ceiling = str(entry.get("tier_ceiling") or "").strip().lower()
        if ceiling not in ("simple", "standard"):
            LOG.warning(
                "extra_candidate %r has invalid tier_ceiling %r (must be 'simple' "
                "or 'standard', never 'complex'); skipping",
                model,
                ceiling,
            )
            continue
        score = 0.35
        raw_score = entry.get("score")
        if isinstance(raw_score, int | float) and not isinstance(raw_score, bool):
            score = float(raw_score)
        score_by_tier: dict[str, float] = {}
        raw_by_tier = entry.get("score_by_tier")
        if isinstance(raw_by_tier, Mapping):
            for tier, value in raw_by_tier.items():
                tier_name = str(tier).strip().lower()
                if (
                    tier_name in TIER_LADDER
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                ):
                    score_by_tier[tier_name] = float(value)
        resolved.append(
            {
                "model": model,
                "tier_ceiling": ceiling,
                "score": score,
                "score_by_tier": score_by_tier,
            }
        )
    return resolved


def extra_candidates_list(config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The configs/swarm.yaml ``router.extra_candidates`` list: registry-only
    models (a healthy CLI but no fusion-agent ranking, e.g. gemini) synthesized
    as candidates up to a per-model ``tier_ceiling``. Missing config → []
    (backward compatible)."""
    from omniagentos.routing.limit_state import load_swarm_config

    router_section = (config if config is not None else load_swarm_config()).get("router")
    if not isinstance(router_section, Mapping):
        return []
    return _parse_extra_candidates(router_section.get("extra_candidates"))


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def default_rankings_loader() -> dict[str, dict[str, Any]]:
    """Agents from ~/.claude/fusion/model-rankings.json keyed by id ({} when
    absent/unreadable)."""
    from omniagentos.modelintel.config import FUSION_RANKINGS

    rankings = _load_json_file(FUSION_RANKINGS) or {}
    agents = rankings.get("agents")
    if not isinstance(agents, list):
        return {}
    return {
        str(agent["id"]): dict(agent)
        for agent in agents
        if isinstance(agent, Mapping) and agent.get("id")
    }


def default_digest_loader() -> dict[str, Any] | None:
    """The live capability digest (~/.claude/fusion/model-intel.json) or None."""
    from omniagentos.modelintel.config import FUSION_DIGEST

    return _load_json_file(FUSION_DIGEST)


def _parse_iso_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class _Candidate:
    agent_id: str
    provider: str
    model: str
    score: float


def _formation_implementers(swarm_json: Mapping[str, Any]) -> list[str]:
    """Read formation implementer family names from stamped ``swarm_json``."""
    raw = swarm_json.get("formation_implementers")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip().lower() for x in raw if str(x).strip()]


def _apply_formation_implementers(
    candidates: Sequence[_Candidate], implementers: Sequence[str]
) -> list[_Candidate]:
    """Prefer formation implementers among filtered candidates (reorder; soft).

    If hard-filtering to implementers leaves ≥1 candidate, use that subset;
    otherwise reorder so preferred providers sort first without emptying the set.
    """
    from omniagentos.formation import prefer_implementers

    if not implementers or not candidates:
        return list(candidates)
    preferred = {str(name).strip().lower() for name in implementers if str(name).strip()}
    if not preferred:
        return list(candidates)
    filtered = [c for c in candidates if c.provider.strip().lower() in preferred]
    if filtered:
        return prefer_implementers(filtered, implementers)
    return prefer_implementers(candidates, implementers)


class DurableRouterLimits:
    """Default limits port with one serialized connection for its lifetime.

    Routing is called from every slot worker. Opening a fresh SQLite connection
    in each limits helper made a capacity-deferred task churn descriptors on
    every retry. The connection is lazy so constructing a router remains cheap,
    shared safely behind ``_lock``, and closed by :meth:`close`.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    def _open_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            from omniagentos.db.store import _connect

            connection = _connect(self._db_path or default_db_path())
            self._connection = connection
        return connection

    def provider_pressure(self, provider: str) -> float:
        from omniagentos.routing.limit_state import provider_pressure

        with self._lock:
            return provider_pressure(
                provider,
                db_path=self._db_path,
                _connection=self._open_connection(),
            )

    def all_cooling(self, provider: str) -> bool:
        from omniagentos.routing.limit_state import all_cooling

        with self._lock:
            return all_cooling(
                provider,
                db_path=self._db_path,
                _connection=self._open_connection(),
            )

    def reserve_account(self, provider: str) -> Any | None:
        from omniagentos.routing.limit_state import reserve_account

        with self._lock:
            return reserve_account(
                provider,
                db_path=self._db_path,
                _connection=self._open_connection(),
            )

    def enabled_account_count(self, provider: str) -> int:
        """Enabled accounts for ``provider`` REGARDLESS of cooldown -- the
        signal that distinguishes 'all cooling' (skip the provider) from
        'never configured' (default CLI login is the account)."""
        with self._lock:
            try:
                row = (
                    self._open_connection()
                    .execute(
                        "SELECT COUNT(*) AS n FROM claude_accounts "
                        "WHERE enabled = 1 AND provider = ?",
                        (provider,),
                    )
                    .fetchone()
                )
                return int(row["n"])
            except Exception:  # noqa: BLE001 -- a broken table must not stop routing.
                LOG.debug("enabled_account_count failed for %s", provider, exc_info=True)
                return 0

    def release_reservation(self, reservation_id: str) -> bool:
        from omniagentos.routing.limit_state import release_reservation

        with self._lock:
            return release_reservation(
                reservation_id,
                db_path=self._db_path,
                _connection=self._open_connection(),
            )

    def close(self) -> None:
        """Close the limits connection; idempotent."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()


def default_samples_loader(
    db_path: str | None = None, *, window: int = 500
) -> list[dict[str, Any]]:
    """Learner samples mined from the durable ``swarm_attempts`` history.

    Each sample: ``{"tier_name", "win", "ts", "complexity"}``. The trace-file
    idiom (``learn.read_traces``) has no swarm writer yet; the attempts table
    IS the recorded win/loss history, so the learner fires on real data from
    the first completed run. Any failure -> [] (a learner reading history
    must never break routing)."""
    from omniagentos.db.store import _connect

    samples: list[dict[str, Any]] = []
    try:
        conn = _connect(db_path or default_db_path())
    except Exception:  # noqa: BLE001
        return samples
    try:
        rows = conn.execute(
            "SELECT a.tier, a.end_reason, a.ended_at, t.swarm_json "
            "FROM swarm_attempts a JOIN board_tasks t ON t.id = a.board_task_id "
            "WHERE a.ended_at IS NOT NULL AND a.tier IS NOT NULL "
            "ORDER BY a.ended_at DESC LIMIT ?",
            (int(window),),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- pre-045 schema or fresh db.
        return samples
    finally:
        conn.close()
    for row in rows:
        end_reason = str(row["end_reason"] or "")
        if end_reason in _LEARN_WIN_REASONS:
            win = True
        elif end_reason in _LEARN_LOSS_REASONS:
            win = False
        else:
            continue
        ended = _parse_iso_ts(row["ended_at"])
        if ended is None:
            continue
        try:
            swarm_json = json.loads(row["swarm_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            swarm_json = {}
        complexity = str(
            (swarm_json.get("complexity") if isinstance(swarm_json, dict) else None) or "standard"
        )
        samples.append(
            {
                "tier_name": str(row["tier"]),
                "win": win,
                "ts": ended.timestamp(),
                "complexity": complexity,
            }
        )
    return samples


class SwarmRouter:
    """The WP5b ``SwarmRouterProto`` implementation (see module docstring).

    Every external surface is injectable: rankings/digest loaders, the limits
    port, the learner's sample source, the attempt-history reader for
    provider_switched, and the clock -- so tests run the full chain with fake
    stores and zero live I/O.
    """

    def __init__(
        self,
        *,
        db_path: str | None = None,
        emitter: SwarmEmitter | None = None,
        limits: Any | None = None,
        config: Mapping[str, Any] | None = None,
        lineage_providers: Mapping[str, str] | None = None,
        effort_by_tier: Mapping[str, str] | None = None,
        effort_overrides: Mapping[str, str] | None = None,
        effort_overrides_by_tier: Mapping[str, Mapping[str, str]] | None = None,
        lane_floors: Mapping[str, Sequence[str]] | None = None,
        model_ladder: Sequence[str] | None = None,
        category_pins: Mapping[str, str] | None = None,
        semantic_pins: bool | None = None,
        category_classifier: Callable[[str], tuple[str, float] | None] | None = None,
        overflow: tuple[Mapping[str, Sequence[str]], float] | None = None,
        extra_candidates: Sequence[Mapping[str, Any]] | None = None,
        model_lineages: Mapping[str, str] | None = None,
        rankings_loader: Callable[[], dict[str, dict[str, Any]]] | None = None,
        digest_loader: Callable[[], dict[str, Any] | None] | None = None,
        samples_loader: Callable[[], list[dict[str, Any]]] | None = None,
        attempts_loader: Callable[[str], list[dict[str, Any]]] | None = None,
        now: Callable[[], datetime] | None = None,
        provider_health_path: str | Path = _DEFAULT_PROVIDER_HEALTH_PATH,
        provider_health_loader: Callable[[], set[str] | None] | None = None,
        provider_health_max_age_hours: float = _DEFAULT_PROVIDER_HEALTH_MAX_AGE_HOURS,
        digest_max_age_hours: float = _DEFAULT_DIGEST_MAX_AGE_HOURS,
        pressure_weight: float = _DEFAULT_PRESSURE_WEIGHT,
        digest_weight: float = _DEFAULT_DIGEST_WEIGHT,
        learn_k: float = SHRINKAGE_K,
        learn_global_prior: float = GLOBAL_PRIOR_RATE,
        target_win_rate: float = 0.6,
    ) -> None:
        self._db_path = db_path
        self._emitter = emitter
        self._owns_limits = limits is None
        self._limits = limits if limits is not None else DurableRouterLimits(db_path)
        self._lineage_providers = (
            dict(lineage_providers)
            if lineage_providers is not None
            else lineage_provider_map(config)
        )
        self._effort_by_tier = (
            dict(effort_by_tier) if effort_by_tier is not None else effort_by_tier_map(config)
        )
        self._effort_overrides = (
            dict(effort_overrides) if effort_overrides is not None else effort_overrides_map(config)
        )
        # Tier-scoped effort overrides (finding #4): additive to the flat map,
        # applied only when the decided tier matches. The flat + tier-scoped
        # overrides are the two halves of ONE effort-override policy: a caller
        # that explicitly injects the flat ``effort_overrides`` is taking control
        # of that policy, so the tier-scoped half defaults to empty rather than
        # leaking config — otherwise a test asking for "pure tier-map effort"
        # (effort_overrides={}) would silently inherit the yaml's tier bumps.
        # Production injects neither and gets both from config.
        if effort_overrides_by_tier is not None:
            self._effort_overrides_by_tier = {
                k: dict(v) for k, v in effort_overrides_by_tier.items()
            }
        elif effort_overrides is not None:
            self._effort_overrides_by_tier = {}
        else:
            self._effort_overrides_by_tier = effort_overrides_by_tier_map(config)
        # Extra candidates (PKG-GEMINI-LANE): registry-only fast-lane models with
        # a healthy CLI but no fusion-agent ranking, synthesized as candidates up
        # to a per-model tier_ceiling. Normalized through the shared parser so an
        # injected raw list validates identically to the config path. Extras are
        # a SUPPLEMENT to the default rankings source, so a caller that injects
        # its own ``rankings_loader`` owns the candidate universe and does NOT
        # inherit config extras (otherwise a test asserting a rankings-only
        # outcome would silently gain a gemini candidate). Production injects
        # neither and loads extras from config.
        if extra_candidates is not None:
            self._extra_candidates = _parse_extra_candidates(extra_candidates)
        elif rankings_loader is not None:
            self._extra_candidates = []
        else:
            self._extra_candidates = extra_candidates_list(config)
        # Model -> lineage index for deriving overflow + extra candidate providers
        # (finding #3). Injectable for hermetic tests; else lazily built from
        # the modelintel registry and cached.
        self._model_lineages_override = dict(model_lineages) if model_lineages is not None else None
        self._model_lineages_cache: dict[str, str] | None = None
        # Lane floors: map from scope (tier name or risk class) to list of models
        self._lane_floors = (
            {k: list(v) for k, v in lane_floors.items()}
            if lane_floors is not None
            else lane_floors_map(config)
        )
        self._model_ladder = (
            [
                model.strip().lower()
                for model in model_ladder
                if isinstance(model, str) and model.strip()
            ]
            if model_ladder is not None
            else model_ladder_list(config)
            if config is not None or rankings_loader is None
            else []
        )
        # Category pins: map from discipline to model
        self._category_pins = (
            dict(category_pins) if category_pins is not None else category_pins_map(config)
        )
        # Semantic pins (PKG-SEMANTIC-PINS): when truthy, a task with no explicit
        # discipline match is classified from its title+description text and, if
        # the verdict names a configured pin category, restricted to that pin —
        # the SAME path an explicit match takes. Config-gated (router.semantic_pins,
        # default off) and fully degradable: an injected fake keeps tests hermetic;
        # a None/exception verdict leaves candidates byte-identical to the
        # explicit-only behavior.
        self._semantic_pins = (
            bool(semantic_pins) if semantic_pins is not None else semantic_pins_enabled(config)
        )
        self._category_classifier = category_classifier
        # Overflow: tuple of (overflow map, pressure threshold)
        if overflow is not None:
            overflow_map, threshold = overflow
            self._overflow = {k: list(v) for k, v in overflow_map.items()}
            self._overflow_threshold = float(threshold)
        else:
            overflow_map, threshold = overflow_config_map(config)
            self._overflow = overflow_map
            self._overflow_threshold = threshold
        self._rankings_loader = rankings_loader or default_rankings_loader
        self._digest_loader = digest_loader or default_digest_loader
        self._samples_loader = samples_loader or (lambda: default_samples_loader(self._db_path))
        self._attempts_loader = attempts_loader or self._default_attempts_loader
        self._now = now or (lambda: datetime.now(UTC))
        self._provider_health_loader = provider_health_loader or (
            lambda: healthy_providers_from_snapshot(
                provider_health_path,
                now=self._now,
                max_age_hours=provider_health_max_age_hours,
            )
        )
        self._digest_max_age_hours = float(digest_max_age_hours)
        self._pressure_weight = float(pressure_weight)
        self._digest_weight = float(digest_weight)
        self._learn_k = float(learn_k)
        self._learn_global_prior = float(learn_global_prior)
        self._target_win_rate = float(target_win_rate)
        self._dal_lock = threading.Lock()
        self._dal: Any | None = None

    # ------------------------------------------------------------------
    # SwarmRouterProto
    # ------------------------------------------------------------------

    def route(self, task: Mapping[str, Any], tier: str) -> RouteDecision | None:
        swarm_json = self._swarm_json_of(task)
        risk = str(swarm_json.get("risk_class") or "none").strip().lower()
        effective_tier = self._effective_tier(swarm_json, tier)
        mode, difficulty, reasoning = TIER_TO_MODE.get(effective_tier, TIER_TO_MODE["standard"])

        candidates = self._candidates(mode, difficulty, reasoning, effective_tier)
        candidates = self._filter_healthy_candidates(self._with_model_ladder_candidates(candidates))
        if risk in DENIED_RISK_CLASSES:
            # Risk pin: external/deploy/destructive execute on claude ONLY.
            # provider_exec's hard-coded deny is the enforcement backstop.
            candidates = [c for c in candidates if c.provider == "claude"] or [
                self._fallback_claude(effective_tier)
            ]

        # Apply lane floors: restrict candidates to a quality floor if configured
        floored = self._apply_lane_floors(candidates, effective_tier, risk)
        if floored is None:
            return None
        candidates = floored

        # Apply category pins: pin to specific model based on task discipline
        # (explicit) or the semantic fallback. ``pinned`` is True only when the
        # candidate set was actually RESTRICTED to a pinned model.
        candidates, pinned = self._apply_category_pins(candidates, task)
        if not pinned:
            candidates = self._apply_model_ladder(candidates, task)

        # Apply pressure overflow: extend candidates for complex tier if all
        # floor providers are under pressure (never for risky classes). A PINNED
        # set is exempt — pin semantics are absolute (pinned+unavailable = requeue,
        # never substitute), so overflow must not append an alternative.
        candidates = self._apply_pressure_overflow(candidates, effective_tier, risk, pinned=pinned)
        candidates = self._filter_healthy_candidates(candidates)

        # Phase B: formation implementers reorder the surviving candidate pool
        # (after risk pins / floors / category pins / overflow). Soft preference —
        # never empty the set; prefer reorder, hard-filter only when ≥1 remains.
        implementers = _formation_implementers(swarm_json)
        if implementers and not pinned:
            candidates = _apply_formation_implementers(candidates, implementers)

        # D5: per-run allowed_providers pin (params on swarm_json or task).
        allowed_providers: list[str] | None = None
        try:
            from omniagentos.dispatch.providers import (
                allowed_providers_from_params,
                filter_dispatch_candidates,
            )

            params_blob = (
                swarm_json.get("params")
                if isinstance(swarm_json.get("params"), dict)
                else swarm_json
            )
            allowed_providers = allowed_providers_from_params(
                params_blob if isinstance(params_blob, dict) else None
            )
            if allowed_providers is None and isinstance(task.get("params"), dict):
                allowed_providers = allowed_providers_from_params(task["params"])  # type: ignore[index]
            if allowed_providers is not None:
                candidates = filter_dispatch_candidates(
                    candidates,
                    {"allowed_providers": allowed_providers},
                    stage="swarm_router",
                )
        except Exception as exc:
            from omniagentos.providers.constraints import ProviderNotAllowed

            if isinstance(exc, ProviderNotAllowed):
                LOG.warning("allowed_providers exhausted swarm candidates: %s", exc)
                return None
            LOG.debug("allowed_providers router filter skipped", exc_info=True)

        decision = self._pick(
            candidates,
            effective_tier,
            preferred_implementers=implementers if not pinned else (),
            allowed_providers=allowed_providers,
        )
        if decision is None:
            return None

        self._emit_provider_switch(task, decision)
        return decision

    # ------------------------------------------------------------------
    # Lane floors, category pins, pressure overflow
    # ------------------------------------------------------------------

    def _apply_lane_floors(
        self, candidates: list[_Candidate], tier: str, risk: str
    ) -> list[_Candidate] | None:
        """Apply lane floors to restrict candidates. high_risk floors take
        precedence over tier floors. Floor entries are matched to candidates
        through model-key normalization (a claude fallback ``opus`` satisfies a
        ``claude-opus-5`` floor — finding #5). A floor entry that matches no
        candidate is a typo or an absent model and is dropped with a warning so
        it can never cause a silent permanent requeue (finding #6). Returns the
        floored candidates, ``candidates`` unchanged when no floor applies, or
        ``None`` (requeue) when the floor is satisfiable only by cooling
        providers."""
        # high_risk (external/deploy/destructive) overrides tier floor.
        floor_scope = "high_risk" if risk in DENIED_RISK_CLASSES else tier

        floor_models = self._lane_floors.get(floor_scope)
        if not floor_models:
            return candidates  # No floor configured; all candidates allowed.

        candidate_keys = {_normalize_model_key(c.model) for c in candidates}
        # Drop floor entries that match no candidate (typo / model absent from
        # the rankings): keeping them would silently strand the task in a
        # requeue loop with no diagnosable cause.
        known_floor: set[str] = set()
        for model in floor_models:
            key = _normalize_model_key(model)
            if key in candidate_keys:
                known_floor.add(key)
            else:
                LOG.warning(
                    "lane floor %r lists model %r matching no candidate; dropping "
                    "it (typo or model absent from rankings)",
                    floor_scope,
                    model,
                )

        if not known_floor:
            # Every floor entry was unknown. Never stall purely on a typo.
            if floor_scope == "high_risk":
                claude_only = [c for c in candidates if c.provider == "claude"]
                if claude_only:
                    LOG.warning(
                        "high_risk floor %r empty after dropping unknown models; "
                        "restricting to claude-lineage candidates (risky work must "
                        "not downgrade to non-claude)",
                        floor_models,
                    )
                    return claude_only
                LOG.warning(
                    "high_risk floor %r empty and no claude-lineage candidate available; requeue",
                    floor_models,
                )
                return None
            LOG.warning(
                "lane floor %r empty after dropping unknown models; treating as no restriction",
                floor_scope,
            )
            return candidates

        floored = [c for c in candidates if _normalize_model_key(c.model) in known_floor]
        if floored:
            return floored
        # Unreachable given known_floor is derived from candidate_keys, but kept
        # as a diagnosable requeue guard.
        LOG.warning(
            "no floor candidate routable for scope %r; requeue (never downgrade)",
            floor_scope,
        )
        return None

    def _apply_category_pins(
        self, candidates: list[_Candidate], task: Mapping[str, Any]
    ) -> tuple[list[_Candidate], bool]:
        """Apply category pins. Returns ``(candidates, pinned)`` where ``pinned``
        is True only when the candidate set was actually RESTRICTED to a pinned
        model (the caller then exempts it from overflow — pin semantics are
        absolute).

        The EXPLICIT ``discipline`` field is matched FIRST and, when NON-EMPTY,
        suppresses the semantic fallback entirely (finding F2): an explicit field
        — even one that maps to no pin — must never be overridden by prose. So a
        matching explicit discipline restricts to its pin, and a populated-but-
        unmapped discipline (e.g. "backend") leaves candidates untouched WITHOUT
        classifying. Only when the field is absent/empty AND
        ``router.semantic_pins`` is truthy is the task's title+description text
        classified (PKG-SEMANTIC-PINS); a verdict naming a configured pin category
        takes the SAME restrict path. Flag off / classifier ``None`` / any
        exception leaves candidates untouched — byte-identical to explicit-only."""
        discipline = str(task.get("discipline") or "").strip().lower()
        if discipline:
            # A non-empty explicit field is authoritative: pin if it maps, else
            # no pin — but NEVER fall through to the semantic classifier.
            pinned_model = self._category_pins.get(discipline)
            if pinned_model:
                return self._restrict_to_pin(candidates, pinned_model)
            return candidates, False

        # Semantic fallback: only reached when the explicit field is absent/empty.
        if not self._semantic_pins:
            return candidates, False
        category = self._classify_category_text(task)
        if category is None:
            return candidates, False
        pinned_model = self._category_pins.get(category)
        if not pinned_model:
            # A semantic verdict outside the pins map (a category with no pin, or
            # a stray label) is not a pin — leave candidates untouched.
            return candidates, False
        return self._restrict_to_pin(candidates, pinned_model)

    def _apply_model_ladder(
        self, candidates: list[_Candidate], task: Mapping[str, Any]
    ) -> list[_Candidate]:
        """Restrict to the rung selected by prior quality failures. A target
        absent after risk/floor filtering cannot override those safety rules."""
        if not self._model_ladder:
            return candidates
        task_id = str(task.get("id") or "")
        try:
            attempts = self._attempts_loader(task_id) if task_id else []
        except Exception:  # noqa: BLE001 -- history failure must not break routing.
            LOG.debug("attempts loader failed for model ladder task %s", task_id, exc_info=True)
            attempts = []
        failed_models = [
            _normalize_model_key(str(attempt.get("model") or ""))
            for attempt in attempts
            if attempt.get("ended_at")
            and str(attempt.get("end_reason") or "") in _LEARN_LOSS_REASONS
        ]
        rung = 0
        for failed_model in failed_models:
            match = next(
                (
                    index
                    for index in range(rung, len(self._model_ladder))
                    if _normalize_model_key(self._model_ladder[index]) == failed_model
                ),
                None,
            )
            if match is not None:
                rung = min(match + 1, len(self._model_ladder) - 1)
        target = self._model_ladder[rung]
        selected = [
            candidate
            for candidate in candidates
            if _normalize_model_key(candidate.model) == _normalize_model_key(target)
        ]
        return selected or candidates

    @staticmethod
    def _restrict_to_pin(
        candidates: list[_Candidate], pinned_model: str
    ) -> tuple[list[_Candidate], bool]:
        """RESTRICT the candidate set to the pinned model only (finding #1):
        _pick re-sorts by score, so merely reordering (pinned + others) lets a
        higher-scored non-pinned model win and silently substitutes for a cooling
        pinned model. Restricting means a pinned-but-cooling model flows to _pick
        -> None -> requeue rather than being replaced. When the pinned model is
        absent from the candidate set (e.g. a high_risk floor already excluded it),
        the candidates are returned unchanged (``pinned=False``) so the floor
        precedence is kept. ``pinned=True`` is returned ONLY when the restriction
        actually took effect, so the caller exempts a genuinely-pinned set from
        overflow substitution."""
        pinned = [
            c
            for c in candidates
            if _normalize_model_key(c.model) == _normalize_model_key(pinned_model)
        ]
        if pinned:
            return pinned, True
        return candidates, False

    def _classify_category_text(self, task: Mapping[str, Any]) -> str | None:
        """Classify the task's title+description into a pin category, or ``None``.

        Uses the injected classifier (hermetic tests) else the lazy default that
        calls :func:`omniagentos.dispatch.categories.classify_category`. Any
        failure or malformed result -> ``None`` (a semantic pin must never break a
        route)."""
        text = self._task_text(task)
        if not text:
            return None
        classifier = self._category_classifier or _default_category_classifier
        try:
            result = classifier(text)
        except Exception:  # noqa: BLE001 -- a classifier fault must never break a route.
            LOG.debug("semantic category classify failed", exc_info=True)
            return None
        if not result:
            return None
        try:
            category, _score = result
        except (TypeError, ValueError):
            LOG.debug("semantic category classifier returned malformed %r", result)
            return None
        category = str(category).strip().lower()
        return category or None

    @staticmethod
    def _task_text(task: Mapping[str, Any]) -> str:
        """The text route() has access to for classification: the board_tasks
        row's ``title`` + ``description`` (the only free-text task fields the
        router receives; swarm_json carries structured routing hints, not prose)."""
        parts = [
            str(task.get("title") or "").strip(),
            str(task.get("description") or "").strip(),
        ]
        return "\n".join(p for p in parts if p)

    def _apply_pressure_overflow(
        self, candidates: list[_Candidate], tier: str, risk: str, *, pinned: bool = False
    ) -> list[_Candidate]:
        """Apply pressure overflow: when tier=complex and ALL floor providers
        report pressure ≥ threshold, extend candidate list with overflow models.
        Overflow NEVER applies to a risky class (finding #2): those are
        claude-pinned, and appending a non-claude overflow model produces a
        poison route ``provider_exec`` later hard-denies. Overflow candidate
        providers are DERIVED from each model's lineage (finding #3), never a
        hardcoded literal.

        A ``pinned`` candidate set is EXEMPT (finding F1): a category pin restricts
        to one model, and pin semantics are absolute — a pinned-but-cooling model
        must requeue, never gain an overflow substitute. Overflow ran after the
        pin restriction, so without this guard a cooling pinned model silently got
        an alternative appended (broke explicit pins too)."""
        if pinned:
            return candidates
        if tier != "complex":
            return candidates
        if risk in DENIED_RISK_CLASSES:
            # Risky classes execute on claude only; never append overflow.
            return candidates

        overflow_models = self._overflow.get("complex")
        if not overflow_models:
            return candidates  # No overflow configured for complex.

        # Get the floor models for complex tier.
        floor_models = self._lane_floors.get("complex")
        if not floor_models:
            return candidates  # No floor → no overflow logic applies.

        # Which providers back the floor models currently in play.
        floor_keys = {_normalize_model_key(m) for m in floor_models}
        floor_providers = {
            c.provider for c in candidates if _normalize_model_key(c.model) in floor_keys
        }
        if not floor_providers:
            return candidates  # No floor candidates in the list.

        # Overflow fires only when EVERY floor provider is at/above threshold.
        # Unreadable pressure is not "healthy low" — count it as pressured so
        # we never free-look an unmeasured ledger as under threshold.
        def _at_or_above_threshold(provider: str) -> bool:
            pressure = self._pressure(provider)
            if pressure is None:
                return True
            return pressure >= self._overflow_threshold

        all_pressured = all(_at_or_above_threshold(provider) for provider in floor_providers)
        if not all_pressured:
            return candidates  # Pressure below threshold; no overflow needed.

        # Extend candidates with overflow models (append, not replace).
        existing_keys = {_normalize_model_key(c.model) for c in candidates}
        overflow_to_add = [
            m for m in overflow_models if _normalize_model_key(m) not in existing_keys
        ]
        if not overflow_to_add:
            return candidates

        # Synthetic candidates (score 0 → tried last). Provider is derived from
        # the model's lineage the same way ranked candidates derive theirs; an
        # unresolvable lineage means we cannot know how to execute it, so it is
        # skipped rather than mis-routed.
        overflow_candidates: list[_Candidate] = []
        for model in overflow_to_add:
            lineage = self._model_lineage(model)
            if not lineage:
                LOG.warning(
                    "overflow model %r has no resolvable lineage; skipping "
                    "(cannot derive executing provider)",
                    model,
                )
                continue
            provider = self._lineage_providers.get(lineage, lineage)
            overflow_candidates.append(
                _Candidate(
                    agent_id=f"overflow-{model}",
                    provider=provider,
                    model=model,
                    score=0.0,
                )
            )
        if not overflow_candidates:
            return candidates
        return candidates + overflow_candidates

    def _model_lineage(self, model: str) -> str | None:
        """Resolve a model key/alias to its lineage (finding #3). Uses the
        injected override when present (hermetic tests), else the cached
        modelintel registry index."""
        if self._model_lineages_override is not None:
            source = self._model_lineages_override
        else:
            if self._model_lineages_cache is None:
                self._model_lineages_cache = default_model_lineage_index()
            source = self._model_lineages_cache
        raw = str(model or "").strip().lower()
        return source.get(raw) or source.get(_normalize_model_key(model))

    def _synth_extra_candidates(self, tier: str) -> list[_Candidate]:
        """Synthesize candidates for the configured registry-only fast-lane
        models (PKG-GEMINI-LANE) eligible at ``tier`` (route tier ≤ the model's
        tier_ceiling). Provider is derived from the model's lineage the same way
        overflow candidates are — an unresolvable (unknown) model is warn+skipped
        rather than routed to a nonexistent provider. Score is
        ``score_by_tier[tier]`` when present, else the flat ``score``."""
        if not self._extra_candidates:
            return []
        if tier not in TIER_LADDER:
            tier = "standard"
        tier_index = TIER_LADDER.index(tier)
        synthesized: list[_Candidate] = []
        for entry in self._extra_candidates:
            ceiling = entry["tier_ceiling"]
            # Eligible only up to the ceiling (never at a higher tier, e.g.
            # complex): route tier index must be at or below the ceiling index.
            if TIER_LADDER.index(ceiling) < tier_index:
                continue
            model = entry["model"]
            lineage = self._model_lineage(model)
            if not lineage:
                LOG.warning(
                    "extra_candidate %r has no resolvable lineage; skipping "
                    "(unknown registry model — cannot derive provider)",
                    model,
                )
                continue
            provider = self._lineage_providers.get(lineage, lineage)
            score = float(entry["score_by_tier"].get(tier, entry["score"]))
            synthesized.append(
                _Candidate(
                    agent_id=f"extra:{model}",
                    provider=provider,
                    model=model,
                    score=score,
                )
            )
        return synthesized

    def _with_extra_candidates(self, candidates: list[_Candidate], tier: str) -> list[_Candidate]:
        """Append the tier-eligible extra candidates, de-duped against models
        already present (an extra never shadows a ranked slot)."""
        extras = self._synth_extra_candidates(tier)
        if not extras:
            return candidates
        existing = {(c.provider, _normalize_model_key(c.model)) for c in candidates}
        fresh = [e for e in extras if (e.provider, _normalize_model_key(e.model)) not in existing]
        return candidates + fresh if fresh else candidates

    def _with_model_ladder_candidates(self, candidates: list[_Candidate]) -> list[_Candidate]:
        """Make ladder-only models eligible for the normal safety chain."""
        existing = {_normalize_model_key(candidate.model) for candidate in candidates}
        augmented = list(candidates)
        for model in self._model_ladder:
            key = _normalize_model_key(model)
            if key in existing:
                continue
            lineage = self._model_lineage(model)
            if not lineage:
                LOG.warning("model_ladder model %r has no resolvable lineage; skipping", model)
                continue
            augmented.append(
                _Candidate(
                    agent_id=f"ladder:{model}",
                    provider=self._lineage_providers.get(lineage, lineage),
                    model=model,
                    score=0.0,
                )
            )
            existing.add(key)
        return augmented

    # ------------------------------------------------------------------
    # Learned start tier (Beta-Binomial hierarchical shrinkage, plan A7)
    # ------------------------------------------------------------------

    def _effective_tier(self, swarm_json: Mapping[str, Any], tier: str) -> str:
        if tier not in TIER_LADDER:
            tier = "standard"
        # D10 Mode dial: the run's persisted speed (stamped into each card's
        # swarm_json at provision) shapes the tier. Ultra FLOORS 'complex'
        # (ultrabuild) unconditionally — mid-ladder included, the floor can
        # only raise. Fast PINS the START tier to 'simple' (ultrafast); the
        # scheduler's escalation ladder stays untouched mid-run. Absent /
        # 'auto' / unknown speeds change nothing (old rows keep current
        # behavior).
        speed = str(swarm_json.get("speed") or "").strip().lower()
        if speed == "ultra":
            floor_index = TIER_LADDER.index("complex")
            if TIER_LADDER.index(tier) < floor_index:
                tier = TIER_LADDER[floor_index]
        # Mid-ladder (escalations, retries, resumes): the scheduler owns the
        # rung; learning applies to START tiers only.
        if (
            swarm_json.get("current_tier")
            or int(swarm_json.get("retries") or 0) > 0
            or int(swarm_json.get("timeout_count") or 0) > 0
        ):
            return tier
        if speed == "fast":
            # A pin, not a hint: Fast Mode always STARTS on the cheapest rung;
            # the learner must not re-raise it (escalation on failure remains
            # the scheduler's job).
            return "simple"
        if speed == "ultra":
            # Already floored to the top rung; the learner could only agree.
            return tier
        complexity = str(swarm_json.get("complexity") or tier).strip().lower()
        try:
            samples = self._samples_loader()
        except Exception:  # noqa: BLE001 -- the learner must never break a route.
            LOG.debug("learner samples unavailable", exc_info=True)
            return tier
        if not samples:
            return tier
        now_ts = self._now().timestamp()
        leaf_samples = [s for s in samples if s.get("complexity") == complexity]
        leaf_counts = decayed_tier_counts(leaf_samples, TIER_LADDER, now_ts=now_ts)
        parent_counts = decayed_tier_counts(samples, TIER_LADDER, now_ts=now_ts)
        learned_index = recommend_start_tier_shrunk(
            [leaf_counts, parent_counts],
            SWARM_TIER_LADDER,
            target_win_rate=self._target_win_rate,
            k=self._learn_k,
            global_prior=self._learn_global_prior,
        )
        hint_index = TIER_LADDER.index(tier)
        if learned_index > hint_index:
            LOG.debug(
                "learned start tier for %s: %s -> %s",
                swarm_task_class(complexity),
                tier,
                TIER_LADDER[learned_index],
            )
            return TIER_LADDER[learned_index]
        if learned_index < hint_index:
            # Down-tiering needs real leaf evidence; a lucky prior must not
            # send complex work to a weak tier (double-work is what A7 kills).
            leaf_n = sum(n for _, n in leaf_counts.values())
            if leaf_n >= learn_min_samples():
                return TIER_LADDER[learned_index]
        return tier

    # ------------------------------------------------------------------
    # Candidate ranking (rankings file + capability digest)
    # ------------------------------------------------------------------

    def _candidates(
        self, mode: str, difficulty: str, reasoning: str, tier: str
    ) -> list[_Candidate]:
        from omniagentos.modelintel.router import DIFFICULTIES, EFFORTS, FALLBACK_WEIGHTS

        try:
            agents = self._rankings_loader() or {}
        except Exception:  # noqa: BLE001
            LOG.debug("rankings loader failed", exc_info=True)
            agents = {}
        if not agents:
            # Missing/empty rankings never brick the swarm: the built-in claude
            # fallback leads the list; opted-in extra_candidates append after it.
            return self._filter_healthy_candidates(
                self._with_extra_candidates([self._fallback_claude(tier)], tier)
            )

        digest_scores = self._digest_scores(mode)
        digest_lineages = self._digest_lineages()
        weights = FALLBACK_WEIGHTS.get(mode, FALLBACK_WEIGHTS["fusionbuild"])
        difficulty_index = DIFFICULTIES.index(difficulty)
        reasoning_index = EFFORTS.index(reasoning)

        best_by_slot: dict[tuple[str, str], _Candidate] = {}
        for agent_id, agent in agents.items():
            if agent.get("role") != "coder" or not agent.get("available"):
                continue
            capability = agent.get("capabilityTier", "moderate")
            if capability not in DIFFICULTIES:
                capability = "moderate"
            if DIFFICULTIES.index(capability) < difficulty_index:
                continue
            max_effort = agent.get("maxReasoning", "medium")
            if max_effort not in EFFORTS:
                max_effort = "medium"
            if EFFORTS.index(max_effort) < reasoning_index:
                continue
            model = str(agent.get("model") or "").strip()
            if not model:
                continue
            lineage = (
                digest_lineages.get(agent_id) or str(agent.get("provider") or "").strip().lower()
            )
            provider = self._lineage_providers.get(lineage, lineage)
            if not provider:
                continue
            # Mechanical fit score (mirrors modelintel.router._mechanical).
            latency = agent.get("warmLatencyMs")
            speed = 1000.0 / latency if latency and latency > 0 else 0.1
            success = agent.get("successRate")
            score = (
                weights["wL"] * speed
                + weights["wQ"] * agent.get("codingScore", 0.5)
                + weights["wT"] * agent.get("toolUseScore", 0.5)
                + weights["wH"] * (0.5 if success is None else success)
                - weights["wC"] * (1.0 - agent.get("costScore", 0.5))
                - weights["wR"] * (agent.get("rateLimitPressure", 0.0) or 0.0)
            )
            # Digest bonus: mode-weighted live domain scores when FRESH; a
            # stale/absent digest contributes nothing (mechanical fallback).
            bonus = digest_scores.get(agent_id)
            if bonus is not None:
                score += self._digest_weight * bonus
            candidate = _Candidate(agent_id=agent_id, provider=provider, model=model, score=score)
            slot = (provider, model)
            incumbent = best_by_slot.get(slot)
            if incumbent is None or candidate.score > incumbent.score:
                best_by_slot[slot] = candidate
        ranked = sorted(best_by_slot.values(), key=lambda c: c.score, reverse=True)
        return self._filter_healthy_candidates(
            self._with_extra_candidates(ranked or [self._fallback_claude(tier)], tier)
        )

    def _filter_healthy_candidates(self, candidates: list[_Candidate]) -> list[_Candidate]:
        """Remove providers that failed the latest provider doctor check."""
        try:
            healthy = self._provider_health_loader()
        except Exception:  # noqa: BLE001 -- health telemetry must not break routing.
            LOG.debug("provider health loader failed", exc_info=True)
            healthy = None
        if healthy is None:
            return candidates
        return [candidate for candidate in candidates if candidate.provider in healthy]

    def _digest_lineages(self) -> dict[str, str]:
        digest = self._digest_cached()
        if digest is None:
            return {}
        lineages: dict[str, str] = {}
        for entry in digest.get("agents") or []:
            if isinstance(entry, Mapping) and entry.get("id") and entry.get("lineage"):
                lineages[str(entry["id"])] = str(entry["lineage"]).strip().lower()
        return lineages

    def _digest_scores(self, mode: str) -> dict[str, float]:
        """Per-agent mode-weighted digest domain score, {} when stale/absent."""
        digest = self._digest_cached()
        if digest is None or not self._digest_fresh(digest):
            return {}
        domain_weights = MODE_DOMAIN_WEIGHTS.get(mode, MODE_DOMAIN_WEIGHTS["fusionbuild"])
        scores: dict[str, float] = {}
        for entry in digest.get("agents") or []:
            if not isinstance(entry, Mapping) or not entry.get("id"):
                continue
            domains = entry.get("scores")
            if not isinstance(domains, Mapping):
                continue
            total = 0.0
            weight_sum = 0.0
            for domain, weight in domain_weights.items():
                value = domains.get(domain)
                if isinstance(value, int | float):
                    total += weight * float(value)
                    weight_sum += weight
            if weight_sum > 0:
                scores[str(entry["id"])] = total / weight_sum
        return scores

    def _digest_cached(self) -> dict[str, Any] | None:
        try:
            digest = self._digest_loader()
        except Exception:  # noqa: BLE001
            LOG.debug("digest loader failed", exc_info=True)
            return None
        return digest if isinstance(digest, dict) else None

    def _digest_fresh(self, digest: Mapping[str, Any]) -> bool:
        stamp = _parse_iso_ts(digest.get("updatedAt") or digest.get("refreshedAt"))
        if stamp is None:
            return False
        age_hours = (self._now() - stamp).total_seconds() / 3600.0
        return 0 <= age_hours <= self._digest_max_age_hours

    def _fallback_claude(self, tier: str) -> _Candidate:
        return _Candidate(
            agent_id="claude-fallback",
            provider="claude",
            model=_FALLBACK_CLAUDE_MODELS.get(tier, "sonnet"),
            score=0.0,
        )

    # ------------------------------------------------------------------
    # Pressure filter + account reservation
    # ------------------------------------------------------------------

    def _pick(
        self,
        candidates: Sequence[_Candidate],
        tier: str,
        preferred_implementers: Sequence[str] = (),
        allowed_providers: Sequence[str] | None = None,
    ) -> RouteDecision | None:
        viable: list[tuple[float, _Candidate, bool]] = []
        # provider -> (configured, cooling, pressure) once measured; absent
        # means the provider is unusable this pick (health unreadable).
        provider_state: dict[str, tuple[bool, bool, float] | None] = {}
        for candidate in candidates:
            provider = candidate.provider
            if provider not in provider_state:
                count = self._enabled_count(provider)
                if count is None:
                    # Could not measure availability — not "0 accounts / default CLI".
                    provider_state[provider] = None
                else:
                    configured = count > 0
                    if configured:
                        cooling = self._all_cooling(provider)
                        pressure = self._pressure(provider)
                        # Unreadable cooling/pressure is not healthy False / 0.0.
                        if cooling is None or pressure is None:
                            provider_state[provider] = None
                        else:
                            provider_state[provider] = (configured, cooling, pressure)
                    else:
                        # Measured zero accounts: default CLI login path is intentional.
                        provider_state[provider] = (False, False, 0.0)
            state = provider_state[provider]
            if state is None:
                continue  # unreadable health: skip, never free-look as viable
            configured, cooling, pressure = state
            if cooling:
                continue  # every enabled account is cooling: route around it.
            adjusted = candidate.score - self._pressure_weight * pressure
            viable.append((adjusted, candidate, configured))
        if not viable:
            # Phase 1.3: G2 records capacity exhaustion on the real route path.
            # Distinguish between unreadable provider health vs actual cooling/pressure.
            has_unreadable = any(state is None for state in provider_state.values())
            reason = (
                "provider_health_unreadable"
                if has_unreadable
                else "all_providers_cooling_or_pressured"
            )
            try:
                from omniagentos.gates.service import GateService

                GateService().g2_dispatch(
                    {
                        "capacity_ok": False,
                        "tier": str(tier),
                        "reason": reason,
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            return None  # all cooling -> the scheduler parks the run.
        # U5 worker abstraction: consult select_worker for a soft preference among
        # providers that already survived pressure/cooling. Only break ties — never
        # override pressure scores (that would defeat backpressure routing).
        worker_pref: str | None = None
        try:
            from omniagentos.routing.workers import select_worker

            pref_providers = [c.provider for _, c, _ in viable]
            sel = select_worker(
                tier=str(tier),
                effort=self._decide_effort(str(tier), pref_providers[0], ""),
                preferred_providers=pref_providers,
                store=getattr(self, "_store", None) or getattr(self, "store", None),
                allowed_providers=allowed_providers,
            )
            if sel.endpoint is not None:
                worker_pref = sel.endpoint.provider
        except Exception:  # noqa: BLE001 — routing must never fail open to empty
            worker_pref = None
        impl_order = {
            str(name).strip().lower(): idx
            for idx, name in enumerate(preferred_implementers)
            if str(name).strip()
        }
        impl_cap = len(impl_order)

        def _impl_rank(provider: str) -> int:
            if not impl_order:
                return 0
            return impl_order.get(str(provider).strip().lower(), impl_cap)

        # Formation implementers first (Phase B bind), then pressure-adjusted
        # score, then worker-abstraction tie-break. Risk pins / cooling already
        # filtered the pool above.
        viable.sort(
            key=lambda item: (
                _impl_rank(item[1].provider),
                -item[0],
                0 if (worker_pref and item[1].provider == worker_pref) else 1,
            )
        )
        for _, candidate, configured in viable:
            if not configured:
                # No accounts registered for the provider: the default CLI
                # login is the account; nothing to reserve.
                return RouteDecision(
                    provider=candidate.provider,
                    model=candidate.model,
                    tier=tier,
                    account_id=None,
                    reservation_id=None,
                    effort=self._decide_effort(tier, candidate.provider, candidate.model),
                )
            reservation = self._reserve(candidate.provider)
            if reservation is None:
                continue  # cooled/saturated since the pressure read: next.
            return RouteDecision(
                provider=candidate.provider,
                model=candidate.model,
                tier=tier,
                account_id=str(reservation.account.account_id),
                reservation_id=str(reservation.id),
                effort=self._decide_effort(tier, candidate.provider, candidate.model),
            )
        return None

    # ------------------------------------------------------------------
    # Effort decision (048): tier map + per-model override
    # ------------------------------------------------------------------

    def _decide_effort(self, tier: str, provider: str, model: str) -> str:
        """The orchestrator-decided reasoning effort for one attempt.

        A map lookup with three tiers of precedence: (1) a flat
        ``router.effort_overrides`` entry for the exact model wins at ANY tier
        (pre-existing 048 semantics, untouched); (2) a tier-scoped
        ``router.effort_overrides_by_tier`` entry applies ONLY when the decided
        tier matches (finding #4 — so pinning gpt-5.6-sol to xhigh on COMPLEX
        does not force xhigh on a STANDARD route to the same model); (3)
        otherwise ``router.effort_by_tier`` for the tier rung (defaulted from
        ``TIER_TO_MODE``'s effort hints). This method is deliberately the single
        seam a learned adjustment plugs into later — the optimizer's
        win-rate-by-effort playbook aggregation over the durable
        ``swarm_attempts.effort`` history is its future input.
        """
        del provider  # reserved for a future provider-scoped adjustment
        model_key = model.strip().lower()
        override = self._effort_overrides.get(model_key)
        if override:
            return override
        tier_scoped = self._effort_overrides_by_tier.get(tier)
        if tier_scoped:
            scoped = tier_scoped.get(model_key)
            if scoped:
                return scoped
        return self._effort_by_tier.get(tier, TIER_TO_MODE.get(tier, TIER_TO_MODE["standard"])[2])

    def _enabled_count(self, provider: str) -> int | None:
        """Enabled-account count, or None when the ledger could not be read.

        None is NOT zero: callers must not treat measurement failure as
        "no accounts registered → default CLI login" (the free-look defect).
        """
        try:
            return int(self._limits.enabled_account_count(provider))
        except Exception:  # noqa: BLE001
            LOG.debug("enabled_account_count failed for %s", provider, exc_info=True)
            return None

    def _all_cooling(self, provider: str) -> bool | None:
        """True/False when measured; None when cooling state is unreadable.

        None is NOT False: callers must not free-look unreadable as healthy.
        """
        try:
            return bool(self._limits.all_cooling(provider))
        except Exception:  # noqa: BLE001
            LOG.debug("all_cooling failed for %s", provider, exc_info=True)
            return None

    def _pressure(self, provider: str) -> float | None:
        """Provider pressure in [0, 1], or None when unreadable.

        None is NOT 0.0: callers must not free-look unreadable as healthy.
        """
        try:
            return max(0.0, min(1.0, float(self._limits.provider_pressure(provider))))
        except Exception:  # noqa: BLE001
            LOG.debug("provider_pressure failed for %s", provider, exc_info=True)
            return None

    def _reserve(self, provider: str) -> Any | None:
        try:
            return self._limits.reserve_account(provider)
        except Exception:  # noqa: BLE001
            LOG.debug("reserve_account failed for %s", provider, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # provider_switched emission
    # ------------------------------------------------------------------

    def _emit_provider_switch(self, task: Mapping[str, Any], decision: RouteDecision) -> None:
        if self._emitter is None:
            return
        task_id = str(task.get("id") or "")
        run_id = str(task.get("swarm_run_id") or "")
        if not task_id or not run_id:
            return
        try:
            attempts = self._attempts_loader(task_id)
        except Exception:  # noqa: BLE001
            LOG.debug("attempts loader failed for %s", task_id, exc_info=True)
            return
        ended = [a for a in attempts if a.get("ended_at")]
        if not ended:
            return
        prior = max(ended, key=lambda a: int(a.get("seq") or 0))
        from_provider = str(prior.get("provider") or "")
        if not from_provider or from_provider == decision.provider:
            return
        try:
            self._emitter.emit(
                run_id,
                ACTION_PROVIDER_SWITCHED,
                {
                    "task_id": task_id,
                    "from_provider": from_provider,
                    "to_provider": decision.provider,
                    "reason": "reroute",
                },
            )
        except Exception:  # noqa: BLE001 -- emission is observability, never control flow.
            LOG.debug("provider_switched emit failed for %s", task_id, exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _swarm_json_of(task: Mapping[str, Any]) -> dict[str, Any]:
        raw = task.get("swarm_json")
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _default_attempts_loader(self, task_id: str) -> list[dict[str, Any]]:
        with self._dal_lock:
            if self._dal is None:
                from omniagentos.swarm.dal import SwarmDal

                self._dal = SwarmDal(self._db_path or default_db_path())
            dal = self._dal
        return dal.list_attempts(task_id)

    def release_reservation(self, reservation_id: str) -> bool:
        """Release a route-owned reservation through the router's limits DAL."""
        release = getattr(self._limits, "release_reservation", None)
        if callable(release):
            return bool(release(reservation_id))
        from omniagentos.routing.limit_state import release_reservation

        return release_reservation(reservation_id, db_path=self._db_path)

    def close(self) -> None:
        """Close lazily-owned DALs after all scheduler workers have stopped."""
        if self._owns_limits:
            close_limits = getattr(self._limits, "close", None)
            if callable(close_limits):
                close_limits()
        with self._dal_lock:
            dal = self._dal
            self._dal = None
        if dal is not None:
            dal.close()


__all__ = [
    "EFFORT_LEVELS",
    "MODE_DOMAIN_WEIGHTS",
    "ROUTE_LEARN_MIN_SAMPLES_ENV",
    "SWARM_TIER_LADDER",
    "TIER_TO_MODE",
    "DurableRouterLimits",
    "SwarmRouter",
    "category_pins_map",
    "default_model_lineage_index",
    "effort_by_tier_map",
    "effort_overrides_by_tier_map",
    "effort_overrides_map",
    "extra_candidates_list",
    "lane_floors_map",
    "model_ladder_list",
    "lineage_provider_map",
    "overflow_config_map",
    "prefer_implementers",  # re-exported from formation for router-side tests
    "semantic_pins_enabled",
    "swarm_task_class",
]


def prefer_implementers(candidates: Sequence[Any], implementers: Sequence[str]) -> list[Any]:
    """Re-export: reorder candidates so formation implementers come first."""
    from omniagentos.formation import prefer_implementers as _prefer

    return _prefer(candidates, implementers)
