"""Feature flags for the metacognition stack (Phase 0 + 8)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

Mode = Literal["off", "shadow", "enforce"]
SkillMode = Literal["off", "shadow", "canary", "enforce"]

_TRUTHY = frozenset({"1", "true", "yes", "on", "enforce"})
_FALSY = frozenset({"0", "false", "no", "off"})
_SHADOW = frozenset({"shadow", "observe", "record"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_path() -> Path:
    override = os.environ.get("OMNIAGENTOS_METACOG_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return _repo_root() / "configs" / "metacog.yaml"


@lru_cache(maxsize=4)
def _load_yaml(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_metacog_config() -> dict[str, Any]:
    return dict(_load_yaml(str(_config_path())))


def clear_metacog_config_cache() -> None:
    _load_yaml.cache_clear()


def _coerce_mode(raw: Any) -> Mode | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "enforce" if raw else "off"
    text = str(raw).strip().lower()
    if text in _FALSY:
        return "off"
    if text in _SHADOW:
        return "shadow"
    if text in _TRUTHY or text == "enforce":
        return "enforce"
    return None


def _section_mode(key: str, *, env: str, default: Mode = "enforce") -> Mode:
    from_env = _coerce_mode(os.environ.get(env))
    if from_env is not None:
        return from_env
    cfg = load_metacog_config()
    from_cfg = _coerce_mode(cfg.get(key))
    if from_cfg is not None:
        return from_cfg
    return default


def metacog_mode() -> Mode:
    """Master gate for metacognitive auto-actions."""
    return _section_mode("mode", env="OMNIAGENTOS_METACOG_MODE", default="enforce")


def memory_promotion_mode() -> Mode:
    return _section_mode(
        "memory_promotion", env="OMNIAGENTOS_METACOG_MEMORY_PROMOTION", default="enforce"
    )


def strategy_switch_mode() -> Mode:
    return _section_mode(
        "strategy_switch", env="OMNIAGENTOS_METACOG_STRATEGY_SWITCH", default="enforce"
    )


def skill_canary_mode() -> SkillMode:
    raw = os.environ.get("OMNIAGENTOS_METACOG_SKILL_CANARY")
    if raw is not None:
        text = str(raw).strip().lower()
        if text in _FALSY:
            return "off"
        if text in _SHADOW:
            return "shadow"
        if text in {"canary"}:
            return "canary"
        if text in _TRUTHY or text == "enforce":
            return "enforce"
    cfg = load_metacog_config()
    text = str(cfg.get("skill_canary") or "off").strip().lower()
    if text in {"off", "shadow", "canary", "enforce"}:
        return text  # type: ignore[return-value]
    return "off"


def stall_config() -> dict[str, Any]:
    cfg = load_metacog_config().get("stall") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "max_no_progress_cycles": int(cfg.get("max_no_progress_cycles") or 2),
        "repetition_threshold": float(cfg.get("repetition_threshold") or 0.55),
        "min_evidence_delta": float(cfg.get("min_evidence_delta") or 0.05),
    }


def context_config() -> dict[str, Any]:
    cfg = load_metacog_config().get("context") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "max_tokens": int(cfg.get("max_tokens") or 4000),
        "max_memories": int(cfg.get("max_memories") or 8),
        "max_artifacts": int(cfg.get("max_artifacts") or 12),
        "max_skills": int(cfg.get("max_skills") or 4),
    }


def artifacts_root() -> Path:
    cfg = load_metacog_config().get("artifacts") or {}
    rel = "metacog-artifacts"
    if isinstance(cfg, dict) and cfg.get("root"):
        rel = str(cfg["root"])
    override = os.environ.get("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return _repo_root() / "var" / rel
