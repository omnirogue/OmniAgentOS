"""Champion-informed routing for CBM (D1 / N2).

Consumes promoted ``model_assignment`` champions **only** via a lazy optional
import of :mod:`omniagentos.lab.runtime` (Lane B's read-only accessor). When
that module or its accessor is absent, this module is a pure no-op so baseline
routing is preserved.

Mode flag (default **off**):

``OMNIAGENTOS_CHAMPION_ROUTING_MODE`` ∈ {``off``, ``shadow``, ``enforce``}

* ``off``     — never consult the lab accessor.
* ``shadow``  — compute champion-informed decision, log a structured delta,
  return the **baseline** decision.
* ``enforce`` — return the champion-informed decision when one is available.

Accessor failures and empty results always degrade to baseline + a warning;
they never raise into the routing hot path.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

LOG = logging.getLogger(__name__)

ChampionMode = Literal["off", "shadow", "enforce"]

CHAMPION_ROUTING_MODE_ENV = "OMNIAGENTOS_CHAMPION_ROUTING_MODE"
CHAMPION_MODES: tuple[ChampionMode, ...] = ("off", "shadow", "enforce")
DEFAULT_CHAMPION_MODE: ChampionMode = "off"

# Short per-process TTL so routing does not hit the lab store every decision.
_CACHE_TTL_S = 30.0
_cache_at: float = 0.0
_cache_rows: list[dict[str, Any]] | None = None

__all__ = [
    "CHAMPION_MODES",
    "CHAMPION_ROUTING_MODE_ENV",
    "DEFAULT_CHAMPION_MODE",
    "ChampionMode",
    "apply_champion_to_recommendation",
    "champion_routing_mode",
    "clear_champion_cache",
    "get_promoted_model_assignments",
    "preferred_providers_for_role",
]


def champion_routing_mode() -> ChampionMode:
    """Resolve ``OMNIAGENTOS_CHAMPION_ROUTING_MODE`` (default ``off``)."""
    raw = os.environ.get(CHAMPION_ROUTING_MODE_ENV, DEFAULT_CHAMPION_MODE)
    value = str(raw or DEFAULT_CHAMPION_MODE).strip().lower()
    if value in CHAMPION_MODES:
        return value  # type: ignore[return-value]
    LOG.warning(
        "champion_routing: unknown mode %r; treating as %s",
        raw,
        DEFAULT_CHAMPION_MODE,
    )
    return DEFAULT_CHAMPION_MODE


def clear_champion_cache() -> None:
    """Drop the process-local champion cache (tests / forced refresh)."""
    global _cache_at, _cache_rows
    _cache_at = 0.0
    _cache_rows = None


def _normalize_rows(raw: Any) -> list[dict[str, Any]]:
    """Coerce the lab accessor's return value into a list of assignment dicts."""
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        # Mapping form: role -> assignment dict, or a single assignment dict.
        if any(k in raw for k in ("model_role", "provider", "model", "task_class")):
            return [dict(raw)]  # type: ignore[arg-type]
        rows: list[dict[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("model_role", str(key))
                row.setdefault("task_class", str(key))
                rows.append(row)
        return rows
    if isinstance(raw, (list, tuple)):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    return []


def get_promoted_model_assignments(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch promoted model_assignment champions via lab.runtime (lazy optional).

    Returns an empty list when mode is ``off``, the accessor is absent, or the
    accessor raises / returns nothing. Never raises to the caller.
    """
    global _cache_at, _cache_rows

    if champion_routing_mode() == "off":
        return []

    now = time.monotonic()
    if (
        not force_refresh
        and _cache_rows is not None
        and (now - _cache_at) < _CACHE_TTL_S
    ):
        return list(_cache_rows)

    rows: list[dict[str, Any]] = []
    try:
        # Lazy + optional: Lane B may not have landed the accessor yet.
        from omniagentos.lab import runtime as lab_runtime  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — optional dependency
        LOG.debug(
            "champion_routing: omniagentos.lab.runtime unavailable (%s); baseline preserved",
            exc,
        )
        _cache_at = now
        _cache_rows = []
        return []

    accessor = None
    for name in (
        "get_promoted_model_assignments",
        "get_champion_model_assignments",
        "promoted_model_assignments",
    ):
        candidate = getattr(lab_runtime, name, None)
        if callable(candidate):
            accessor = candidate
            break

    if accessor is None:
        LOG.debug(
            "champion_routing: lab.runtime has no model_assignment accessor; baseline preserved"
        )
        _cache_at = now
        _cache_rows = []
        return []

    try:
        rows = _normalize_rows(accessor())
    except Exception as exc:  # noqa: BLE001 — degrade safely
        LOG.warning(
            "champion_routing: accessor raised (%s); degrading to baseline",
            exc,
        )
        rows = []

    _cache_at = now
    _cache_rows = list(rows)
    return list(rows)


def _match_assignment(
    rows: list[dict[str, Any]],
    *,
    model_role: str | None,
    task_class: str | None,
) -> dict[str, Any] | None:
    role = (model_role or "").strip()
    tclass = (task_class or "").strip()
    for row in rows:
        row_role = str(row.get("model_role") or row.get("role") or "").strip()
        row_class = str(row.get("task_class") or row.get("class") or "").strip()
        if role and row_role and row_role == role:
            return row
        if tclass and row_class and row_class == tclass:
            return row
    # Single global champion (no role key) applies to every role.
    if len(rows) == 1:
        only = rows[0]
        if not any(
            str(only.get(k) or "").strip()
            for k in ("model_role", "role", "task_class", "class")
        ):
            return only
    return None


def apply_champion_to_recommendation(
    baseline: Mapping[str, Any],
    *,
    task_class: str | None = None,
) -> dict[str, Any]:
    """Return a recommendation dict under the active champion-routing mode.

    Always returns a new dict. In ``shadow`` mode the baseline values are
    preserved and a ``champion_shadow`` key records the would-be delta. In
    ``enforce`` mode champion fields overwrite baseline fields when present.
    """
    result: dict[str, Any] = dict(baseline)
    mode = champion_routing_mode()
    result["champion_routing_mode"] = mode

    if mode == "off":
        return result

    rows = get_promoted_model_assignments()
    role = str(baseline.get("model_role") or "") or None
    match = _match_assignment(rows, model_role=role, task_class=task_class)

    if match is None:
        result["champion_applied"] = False
        return result

    champion_view: dict[str, Any] = {}
    if match.get("model_role"):
        champion_view["model_role"] = str(match["model_role"])
    if match.get("provider"):
        champion_view["provider"] = str(match["provider"]).strip().lower()
    if match.get("model"):
        champion_view["model"] = str(match["model"])
    if match.get("reasoning_effort"):
        champion_view["reasoning_effort"] = str(match["reasoning_effort"])
    if match.get("provider_hints"):
        hints = match["provider_hints"]
        if isinstance(hints, (list, tuple)):
            champion_view["provider_hints"] = [str(h).strip().lower() for h in hints]

    # Prefer a single champion provider as the head of provider_hints.
    if "provider" in champion_view and "provider_hints" not in champion_view:
        baseline_hints = list(baseline.get("provider_hints") or [])
        head = champion_view["provider"]
        rest = [h for h in baseline_hints if str(h).strip().lower() != head]
        champion_view["provider_hints"] = [head, *rest]

    delta = {
        key: {"baseline": baseline.get(key), "champion": value}
        for key, value in champion_view.items()
        if baseline.get(key) != value
    }

    shadow_record = {
        "mode": mode,
        "matched": {
            k: match.get(k)
            for k in ("model_role", "task_class", "provider", "model", "surface_id")
            if match.get(k) is not None
        },
        "delta": delta,
    }

    if mode == "shadow":
        result["champion_shadow"] = shadow_record
        result["champion_applied"] = False
        LOG.info(
            "champion_routing shadow delta: %s",
            shadow_record,
        )
        return result

    # enforce
    for key, value in champion_view.items():
        result[key] = value
    result["champion_applied"] = bool(delta)
    result["champion_shadow"] = shadow_record  # keep provenance even in enforce
    if delta:
        LOG.info("champion_routing enforce applied: %s", shadow_record)
    return result


def preferred_providers_for_role(
    model_role: str | None,
    baseline_hints: list[str] | None = None,
) -> list[str]:
    """Champion-aware preferred provider order for worker selection.

    Under ``off``/``shadow`` returns baseline (or empty). Under ``enforce``
    returns champion provider_hints when a match exists.
    """
    base = list(baseline_hints or [])
    mode = champion_routing_mode()
    if mode != "enforce":
        # Shadow still logs via apply_champion_to_recommendation; selection stays baseline.
        return base

    rec = apply_champion_to_recommendation(
        {"model_role": model_role or "", "provider_hints": base},
    )
    hints = rec.get("provider_hints")
    if isinstance(hints, list) and hints:
        return [str(h) for h in hints]
    return base


def merge_into(
    target: MutableMapping[str, Any],
    recommendation: Mapping[str, Any],
) -> None:
    """Copy champion fields from *recommendation* into *target* (in place)."""
    for key in (
        "model_role",
        "reasoning_effort",
        "provider_hints",
        "champion_routing_mode",
        "champion_applied",
        "champion_shadow",
        "provider",
        "model",
    ):
        if key in recommendation:
            target[key] = recommendation[key]
