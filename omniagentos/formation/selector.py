"""Select a formation (team structure) for a task.

Reconciles with existing machinery:
* CBM still decides effort / rung (``omniagentos.cbm``)
* assess still escalates (``omniagentos.execution.assess``)
* routing still picks models (``omniagentos.routing``)

This module only answers: which *formation* (role-typed team) fits the task.
Phase B also returns confidence + topology and a pure implementer-preference
helper so the decision can bind to execution (not just a plan note).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml

LOG = logging.getLogger(__name__)

T = TypeVar("T")

# Formation id → intended allocation topology (B2). Stored on the plan binding;
# fanout may consume later — this pass only records the join.
FORMATION_TOPOLOGY: dict[str, str] = {
    "coding": "hierarchical",
    "research": "map_reduce",
    "creative": "generator_critic",
    "marketing": "generator_critic",
    "operations": "sequential",
    "prediction": "specialist_panel",
}

# Confidence ladder (B3 lite).
_CONF_TASK_CLASS = 0.95
_CONF_KEYWORD = 0.85
_CONF_DEFAULT = 0.4
# Below this, selection is low-confidence (T1: must not silently route as sure).
CONFIDENCE_THRESHOLD = 0.5
_REASON_TASK_CLASS = "task_class_match"
_REASON_KEYWORD = "keyword_match"
_REASON_DEFAULT = "default_fallback"
_REASON_LOW_CONFIDENCE = "low_confidence_fallback"


@dataclass(frozen=True)
class FormationRole:
    name: str
    preferred_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Formation:
    id: str
    implementers: tuple[str, ...] = ()
    reviewer: str = "opus"
    planner: str = "opus"
    mechanical_gate: bool = True
    use_for: tuple[str, ...] = ()
    review_classes: tuple[str, ...] = ()

    def roles(self) -> list[FormationRole]:
        return [
            FormationRole("planner", (self.planner,)),
            FormationRole("implementer", self.implementers),
            FormationRole("reviewer", (self.reviewer,)),
        ]


@dataclass(frozen=True)
class FormationSelection:
    """Formation plus light confidence metadata (Phase B / T1)."""

    formation: Formation
    confidence: float
    reason: str


def _default_config_path() -> Path:
    # OMNIAGENTOS_FORMATIONS_PATH lets isolated harnesses (simharness) pin a
    # formations fixture: deterministic simulations must not change verdicts
    # when the operator retunes the live config.
    override = os.environ.get("OMNIAGENTOS_FORMATIONS_PATH")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "configs" / "formations.yaml"


@lru_cache(maxsize=2)
def _load_raw(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        LOG.warning("formation config load failed: %s", exc)
        return {}


def clear_formation_cache() -> None:
    _load_raw.cache_clear()


def _formations_from_config() -> dict[str, Formation]:
    raw = _load_raw(str(_default_config_path()))
    out: dict[str, Formation] = {}
    block = raw.get("formations") or {}
    if not isinstance(block, dict):
        return _builtin_formations()
    for fid, spec in block.items():
        if not isinstance(spec, dict):
            continue
        out[str(fid)] = Formation(
            id=str(fid),
            implementers=tuple(spec.get("implementers") or ("grok", "gemini")),
            reviewer=str(spec.get("reviewer") or "opus"),
            planner=str(spec.get("planner") or "opus"),
            mechanical_gate=bool(spec.get("mechanical_gate", True)),
            use_for=tuple(str(x).lower() for x in (spec.get("use_for") or [])),
            review_classes=tuple(str(x) for x in (spec.get("review_classes") or [])),
        )
    return out or _builtin_formations()


def _builtin_formations() -> dict[str, Formation]:
    return {
        "coding": Formation(
            id="coding",
            implementers=("grok", "gemini"),
            reviewer="opus",
            planner="opus",
            mechanical_gate=True,
            use_for=("feature", "bug", "refactor", "code", "architecture"),
            review_classes=("R1", "R2", "R3", "R4"),
        ),
        "creative": Formation(
            id="creative",
            implementers=("grok", "gemini"),
            reviewer="fable",
            planner="fable",
            mechanical_gate=False,
            use_for=("design", "branding", "ad_copy", "image", "video"),
        ),
        "research": Formation(
            id="research",
            implementers=("grok", "gemini"),
            reviewer="opus",
            planner="sol",
            mechanical_gate=False,
            use_for=("research", "competitive", "evidence"),
        ),
        "marketing": Formation(
            id="marketing",
            implementers=("grok", "gemini"),
            reviewer="fable",
            planner="fable",
            mechanical_gate=False,
            use_for=("campaign", "funnel", "landing", "ad", "marketing"),
        ),
        "operations": Formation(
            id="operations",
            implementers=("grok", "gemini"),
            reviewer="opus",
            planner="opus",
            mechanical_gate=True,
            use_for=("automation", "ops", "finance", "admin", "workflow"),
        ),
        "prediction": Formation(
            id="prediction",
            implementers=("grok", "gemini"),
            reviewer="opus",
            planner="sol",
            mechanical_gate=True,
            use_for=(
                "prediction",
                "forecast",
                "conversion",
                "backtest",
                "simulation",
                "decision_support",
            ),
            review_classes=("R2", "R3", "R4"),
        ),
    }


FORMATIONS: dict[str, Formation] = {}  # populated lazily


def _all() -> dict[str, Formation]:
    global FORMATIONS
    if not FORMATIONS:
        FORMATIONS = _formations_from_config()
    return FORMATIONS


# Weighted keyword patterns — best total score wins (not first-match).
# Creative patterns score higher on "design/brand/hero" so they beat marketing
# "landing/ad" when both appear (F7).
_KEYWORD_SCORES: list[tuple[re.Pattern[str], str, float]] = [
    (
        re.compile(r"\b(design|brand(?:ing)?|hero|visual|copy|image|video|webinar|script)\b", re.I),
        "creative",
        3.0,
    ),
    (
        re.compile(r"\b(ad(?:vertising)?|campaign|funnel|landing|offer|marketing)\b", re.I),
        "marketing",
        2.0,
    ),
    (re.compile(r"\b(research|competitive|investigate|evidence)\b", re.I), "research", 3.0),
    (
        re.compile(r"\b(automat\w*|ops|support|finance|admin|workflow|integrat\w*)\b", re.I),
        "operations",
        2.5,
    ),
    (re.compile(r"\b(bug|fix|refactor|api|code|implement|feature)\b", re.I), "coding", 2.5),
    # B7 prediction — high weight so "prediction system / forecast / conversion rates" wins.
    (
        re.compile(
            r"\b(prediction|forecast(?:ing)?|backtest|simulation|decision[_\s-]?support|"
            r"conversion\s+rates?|predict(?:ive)?)\b",
            re.I,
        ),
        "prediction",
        3.5,
    ),
]
# Back-compat alias for tests that iterate patterns.
_KEYWORD_MAP: list[tuple[re.Pattern[str], str]] = [(pat, fid) for pat, fid, _w in _KEYWORD_SCORES]


def topology_for_formation(formation_id: str) -> str | None:
    """Map a formation id to its intended allocation topology (B2)."""
    fid = str(formation_id or "").strip().lower()
    creative_mode = os.environ.get("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "off").strip().lower()
    if creative_mode not in {"off", "shadow", "enforce"}:
        creative_mode = "off"
    if fid == "creative" and creative_mode == "enforce":
        return "parallel_sections"
    return FORMATION_TOPOLOGY.get(fid)


def select_formation_with_confidence(
    *,
    goal: str | None = None,
    task_class: str | None = None,
    risk_class: str | None = None,
) -> FormationSelection:
    """Pick a formation and record light confidence (B3 lite).

    Prefer an explicit ``task_class`` match against ``use_for`` tags; otherwise
    keyword-scan the goal; default to coding (or ``default_formation`` config).
    """
    formations = _all()
    raw = _load_raw(str(_default_config_path()))
    default_id = str(raw.get("default_formation") or "coding")

    if task_class:
        tc = task_class.strip().lower()
        for fid, form in formations.items():
            if tc == fid or tc in form.use_for:
                return FormationSelection(
                    formation=form,
                    confidence=_CONF_TASK_CLASS,
                    reason=_REASON_TASK_CLASS,
                )

    text = f"{goal or ''} {task_class or ''} {risk_class or ''}".strip()
    scores: dict[str, float] = {}
    if text:
        for pattern, fid, weight in _KEYWORD_SCORES:
            if fid not in formations:
                continue
            hits = pattern.findall(text)
            if hits:
                scores[fid] = scores.get(fid, 0.0) + weight * len(hits)
    if scores:
        best_fid = max(scores, key=lambda k: (scores[k], k))
        conf = min(0.95, _CONF_KEYWORD + 0.02 * (scores[best_fid] - 2.0))
        return FormationSelection(
            formation=formations[best_fid],
            confidence=max(_CONF_KEYWORD, conf) if scores[best_fid] >= 2.0 else _CONF_KEYWORD,
            reason=_REASON_KEYWORD,
        )

    # Low-confidence default: still return a formation object (callers may
    # park/surface), but mark reason so T1 does not treat it as a sure route.
    fallback = formations.get(default_id) or next(iter(formations.values()))
    return FormationSelection(
        formation=fallback,
        confidence=_CONF_DEFAULT,
        reason=_REASON_LOW_CONFIDENCE if not text else _REASON_DEFAULT,
    )


def is_low_confidence(selection: FormationSelection) -> bool:
    """True when selection confidence is below CONFIDENCE_THRESHOLD (F5/F6)."""
    return float(selection.confidence) < CONFIDENCE_THRESHOLD


def select_formation(
    *,
    goal: str | None = None,
    task_class: str | None = None,
    risk_class: str | None = None,
) -> Formation:
    """Pick the best formation for a task.

    Prefer an explicit ``task_class`` match against ``use_for`` tags; otherwise
    keyword-scan the goal; default to coding.
    """
    return select_formation_with_confidence(
        goal=goal, task_class=task_class, risk_class=risk_class
    ).formation


def prefer_implementers[T](candidates: Sequence[T], implementers: Sequence[str]) -> list[T]:
    """Reorder candidates so formation implementers come first (stable).

    Family names (``grok``, ``gemini``, ``opus``, …) match candidate
    ``provider`` (attribute or mapping key). Non-matching candidates keep
    relative order after preferred ones. Empty implementers → identity copy.
    Does not drop candidates (routing may still hard-filter when capacity
    leaves ≥1 preferred survivor).
    """
    if not implementers:
        return list(candidates)
    order = {
        str(name).strip().lower(): idx for idx, name in enumerate(implementers) if str(name).strip()
    }
    if not order:
        return list(candidates)

    def _provider_of(item: T) -> str:
        provider = getattr(item, "provider", None)
        if provider is None and isinstance(item, Mapping):
            provider = item.get("provider")
        return str(provider or "").strip().lower()

    def _rank(item: T) -> int:
        key = _provider_of(item)
        return order.get(key, len(order))

    # Stable sort: preferred providers by implementer order, then others.
    return sorted(candidates, key=_rank)


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "FORMATIONS",
    "FORMATION_TOPOLOGY",
    "Formation",
    "FormationRole",
    "FormationSelection",
    "clear_formation_cache",
    "is_low_confidence",
    "prefer_implementers",
    "select_formation",
    "select_formation_with_confidence",
    "topology_for_formation",
]
