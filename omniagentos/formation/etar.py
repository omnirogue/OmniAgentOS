"""ETAR (Expected Time to Accepted Result) priors for formation routing.

Formula (FORMATION-ARCHITECTURE §12):

    ETAR = t_initial
         + P(repair) × t_repair
         + P(escalation) × t_escalation
         + t_review

Priors come from configs/modelintel.yaml ``speed`` / domain quality scores.
This is the L5 baseline seed — production wall-clock should overwrite via
``formation_selections`` outcomes, not replace this formula.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_MODELINTEL = _ROOT / "configs" / "modelintel.yaml"

# Wall-clock scale: speed prior 1.0 ≈ BASE_FAST_S for a standard unit of work.
_BASE_FAST_S = 90.0
_REPAIR_FRACTION = 0.55  # repair is cheaper than full re-do
_ESCALATION_FRACTION = 1.4  # escalate ≈ full re-do on a slower model
_REVIEW_FRACTION = 0.35  # review relative to implementer time

# Role → modelintel key (best-effort)
_ROLE_MODEL = {
    "grok": "grok-4.5",
    "gemini": "gemini-3.1-pro",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
    "sol": "gpt-5.6-sol",
    "claude": "claude-sonnet-5",
}

# Domain quality key by formation
_FORMATION_QUALITY_KEY = {
    "coding": "coding-implementation",
    "creative": "coding-implementation",  # no creative domain — reuse as soft prior
    "research": "web-research",
    "marketing": "web-research",
    "operations": "agentic-tool-use",
    "prediction": "coding-implementation",
}


@dataclass
class EtarBreakdown:
    t_initial_s: float
    p_repair: float
    t_repair_s: float
    p_escalation: float
    t_escalation_s: float
    t_review_s: float
    etar_s: float
    implementer: str
    reviewer: str
    quality_prior: float
    speed_prior: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=1)
def _load_model_priors() -> dict[str, dict[str, float]]:
    if not _MODELINTEL.is_file():
        return {}
    raw = yaml.safe_load(_MODELINTEL.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, float]] = {}
    for entry in raw.get("models") or []:
        key = str(entry.get("key") or "")
        priors = entry.get("priors") or {}
        if key and isinstance(priors, dict):
            out[key] = {str(k): float(v) for k, v in priors.items() if isinstance(v, (int, float))}
    return out


def _resolve_model_key(role: str) -> str:
    role_l = (role or "").lower().strip()
    if role_l in _ROLE_MODEL:
        return _ROLE_MODEL[role_l]
    priors = _load_model_priors()
    if role_l in priors:
        return role_l
    # fuzzy
    for k in priors:
        if role_l and role_l in k:
            return k
    return role_l or "grok-4.5"


def _priors_for(role: str) -> dict[str, float]:
    key = _resolve_model_key(role)
    priors = _load_model_priors().get(key)
    if priors:
        return priors
    # sensible defaults if model missing from yaml
    return {
        "coding-implementation": 0.75,
        "speed": 0.55,
        "web-research": 0.55,
        "agentic-tool-use": 0.7,
        "debugging": 0.7,
    }


def latency_from_speed(speed: float, *, base_s: float = _BASE_FAST_S) -> float:
    """Map speed prior (higher = faster) to expected seconds for one unit of work."""
    s = max(0.08, min(1.0, float(speed)))
    return base_s / s


def compute_etar(
    *,
    arm: str,
    formation_id: str | None,
    implementer: str,
    reviewer: str | None = None,
    mechanical_gate: bool = False,
    difficulty: str = "medium",
) -> EtarBreakdown:
    """Compute prior-based ETAR for a formation arm or opus-solo arm.

    ``arm`` is ``formation`` or ``opus_solo``. Difficulty scales base time.
    """
    notes: list[str] = []
    scale = {"low": 0.7, "medium": 1.0, "high": 1.45}.get(difficulty, 1.0)
    qkey = _FORMATION_QUALITY_KEY.get(formation_id or "coding", "coding-implementation")

    if arm == "opus_solo":
        impl = "opus"
        rev = "opus"
        mechanical_gate = False
        notes.append("arm=opus_solo plan+implement+self_review")
    else:
        impl = implementer or "grok"
        rev = reviewer or "opus"
        notes.append(f"arm=formation formation={formation_id}")

    ip = _priors_for(impl)
    rp = _priors_for(rev)
    quality = float(ip.get(qkey, ip.get("coding-implementation", 0.75)))
    speed = float(ip.get("speed", 0.55))
    rev_speed = float(rp.get("speed", 0.35))

    t_initial = latency_from_speed(speed) * scale
    # Solo opus does more in the initial pass (plan + implement).
    if arm == "opus_solo":
        t_initial *= 1.55
        notes.append("solo multiplies t_initial ×1.55 (plan+impl)")

    p_repair = max(0.05, min(0.85, 1.0 - quality))
    if mechanical_gate:
        p_repair *= 0.72  # gates catch a slice of mechanical failures
        notes.append("mechanical_gate reduces P(repair) ×0.72")
    if arm == "opus_solo":
        p_repair *= 0.55  # stronger first-pass
        notes.append("solo reduces P(repair) ×0.55")

    t_repair = t_initial * _REPAIR_FRACTION
    # Escalation: formation escalates to opus impl; solo escalates less often but costs more.
    if arm == "opus_solo":
        p_escalation = max(0.02, p_repair * 0.25)
        t_escalation = t_initial * _ESCALATION_FRACTION * 1.1
    else:
        p_escalation = max(0.04, p_repair * 0.4)
        t_escalation = (
            latency_from_speed(float(_priors_for("opus").get("speed", 0.35))) * scale * 1.2
        )

    if arm == "opus_solo":
        # self-review bundled; still budget residual review cost
        t_review = latency_from_speed(rev_speed) * scale * (_REVIEW_FRACTION * 0.6)
        notes.append("solo self-review residual")
    else:
        t_review = latency_from_speed(rev_speed) * scale * _REVIEW_FRACTION
        notes.append(f"reviewer={rev}")

    etar = t_initial + p_repair * t_repair + p_escalation * t_escalation + t_review
    return EtarBreakdown(
        t_initial_s=round(t_initial, 2),
        p_repair=round(p_repair, 4),
        t_repair_s=round(t_repair, 2),
        p_escalation=round(p_escalation, 4),
        t_escalation_s=round(t_escalation, 2),
        t_review_s=round(t_review, 2),
        etar_s=round(etar, 2),
        implementer=impl,
        reviewer=rev,
        quality_prior=round(quality, 4),
        speed_prior=round(speed, 4),
        notes=notes,
    )


def compare_arms(formation_etar: EtarBreakdown, solo_etar: EtarBreakdown) -> dict[str, Any]:
    delta = formation_etar.etar_s - solo_etar.etar_s
    winner = "formation" if delta < 0 else ("opus_solo" if delta > 0 else "tie")
    return {
        "winner": winner,
        "delta_s": round(delta, 2),
        "formation_etar_s": formation_etar.etar_s,
        "opus_solo_etar_s": solo_etar.etar_s,
        "formation_faster_pct": round(
            100.0 * (solo_etar.etar_s - formation_etar.etar_s) / max(solo_etar.etar_s, 1e-6), 1
        ),
    }


__all__ = [
    "EtarBreakdown",
    "compare_arms",
    "compute_etar",
    "latency_from_speed",
]
