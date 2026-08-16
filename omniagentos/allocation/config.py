from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "DEFAULT_ROUTER_MODE",
    "DEFAULT_MIN_CONFIDENCE",
    "ROUTER_MODES",
    "ROUTING_CONFIG_ENV",
    "TASK_SHAPE_ROUTER_ENV",
    "RouterMode",
    "_coerce_mode",
    "_router_section",
    "router_config_version",
    "router_min_confidence",
    "routing_config",
    "routing_config_path",
    "task_shape_router_enabled",
    "task_shape_router_enforcing",
    "task_shape_router_mode",
]

RouterMode = Literal["off", "shadow", "enforce"]
ROUTER_MODES: tuple[RouterMode, ...] = ("off", "shadow", "enforce")
TASK_SHAPE_ROUTER_ENV = "OMNIAGENTOS_TASK_SHAPE_ROUTER"
ROUTING_CONFIG_ENV = "OMNIAGENTOS_ROUTING_CONFIG"
DEFAULT_ROUTER_MODE: RouterMode = "off"
DEFAULT_MIN_CONFIDENCE = 0.5

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _repo_root() -> Path:
    """Repo root = parent of the ``omniagentos`` package (cwd-independent)."""
    return Path(__file__).resolve().parent.parent.parent


def routing_config_path() -> Path:
    """Resolve ``configs/routing.yaml`` cwd-independently.

    ``OMNIAGENTOS_ROUTING_CONFIG`` overrides, the same convention as
    ``OMNIAGENTOS_SWARM_CONFIG`` / ``OMNIAGENTOS_ACCOUNTS_CONFIG``.
    """
    override = os.environ.get(ROUTING_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "configs" / "routing.yaml"


def routing_config(path: Path | None = None) -> dict[str, Any]:
    """``configs/routing.yaml`` as a plain dict; missing/broken file -> ``{}``."""
    try:
        raw = yaml.safe_load((path or routing_config_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _router_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else routing_config()
    section = config.get("task_shape_router")
    return section if isinstance(section, dict) else {}


def _coerce_mode(value: Any) -> RouterMode | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "enforce" if value else "off"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in ROUTER_MODES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    return None


def task_shape_router_mode() -> RouterMode:
    from_env = _coerce_mode(os.environ.get(TASK_SHAPE_ROUTER_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_mode(_router_section().get("mode"))
    if from_config is not None:
        return from_config
    return DEFAULT_ROUTER_MODE


def task_shape_router_enabled() -> bool:
    return task_shape_router_mode() != "off"


def task_shape_router_enforcing() -> bool:
    return task_shape_router_mode() == "enforce"


def _valid_confidence(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed == float("inf") or parsed == float("-inf"):  # NaN check
        return None
    return max(0.0, min(1.0, parsed))


def router_min_confidence() -> float:
    from_env = _valid_confidence(os.environ.get("OMNIAGENTOS_ROUTER_MIN_CONFIDENCE"))
    if from_env is not None:
        return from_env
    from_config = _valid_confidence(_router_section().get("min_confidence"))
    if from_config is not None:
        return from_config
    return DEFAULT_MIN_CONFIDENCE


def router_config_version() -> str:
    from_env = os.environ.get("OMNIAGENTOS_ROUTER_VERSION")
    if from_env is not None:
        return from_env.strip()
    from_config = _router_section().get("version")
    if from_config is not None:
        return str(from_config).strip()
    return "v1"
