"""The Phase-3 toolplane knobs: deferred tool catalog + resource-aware scheduler.

Provides configuration resolution following the repository's standard environment
and config file overrides. The default mode is "off" (ships dark), ensuring byte-identical
behavior with the catalog and scheduler disabled.

Three modes:
* "off": The mechanism is entirely disabled.
* "shadow": Evaluates predicates and records metrics/logs but does not block.
* "enforce": Actively enforces bounds or policies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

__all__ = [
    "CATALOG_MODES",
    "DEFAULT_CATALOG_MODE",
    "DEFAULT_CONCURRENCY_BUDGET",
    "DEFAULT_CORE_TOOLS",
    "DEFAULT_MAX_DEFINITIONS",
    "DEFAULT_SCHEDULER_MODE",
    "DEFAULT_SMALL_CATALOG_TOKENS",
    "DEFAULT_SMALL_CATALOG_TOOLS",
    "TOOL_CATALOG_ENV",
    "TOOL_SCHEDULER_ENV",
    "ToolCatalogMode",
    "concurrency_budget",
    "core_tools",
    "max_definitions",
    "small_catalog_max_tokens",
    "small_catalog_max_tools",
    "tool_catalog_enabled",
    "tool_catalog_enforcing",
    "tool_catalog_mode",
    "tool_scheduler_enabled",
    "tool_scheduler_enforcing",
    "tool_scheduler_mode",
    "toolplane_config",
    "toolplane_config_path",
]

ToolCatalogMode = Literal["off", "shadow", "enforce"]
CATALOG_MODES: tuple[ToolCatalogMode, ...] = ("off", "shadow", "enforce")

TOOL_CATALOG_ENV = "OMNIAGENTOS_TOOL_CATALOG"
TOOL_SCHEDULER_ENV = "OMNIAGENTOS_TOOL_SCHEDULER"
_CONFIG_ENV = "OMNIAGENTOS_TOOLPLANE_CONFIG"

DEFAULT_CATALOG_MODE: ToolCatalogMode = "off"
DEFAULT_SCHEDULER_MODE: ToolCatalogMode = "off"
DEFAULT_CORE_TOOLS: tuple[str, ...] = ()  # configured per host
DEFAULT_MAX_DEFINITIONS = 5  # full defs returned per search
DEFAULT_SMALL_CATALOG_TOOLS = 10  # <=10 tools -> no deferral
DEFAULT_SMALL_CATALOG_TOKENS = 10_000  # <=10k schema tokens -> no deferral
DEFAULT_CONCURRENCY_BUDGET = 4  # max calls in one concurrent wave

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _repo_root() -> Path:
    """Repo root = parent of the ``omniagentos`` package (cwd-independent)."""
    return Path(__file__).resolve().parent.parent.parent


def toolplane_config_path() -> Path:
    """Resolve configs/toolplane.yaml cwd-independently.

    ``OMNIAGENTOS_TOOLPLANE_CONFIG`` overrides.
    """
    override = os.environ.get(_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "configs" / "toolplane.yaml"


def toolplane_config(path: Path | None = None) -> dict[str, Any]:
    """``configs/toolplane.yaml`` as a plain dict; missing/broken file -> ``{}``.

    Best-effort by design: a fresh checkout, a test tmpdir, or a half-edited YAML
    file must degrade to the hard-coded defaults rather than take a lane down.
    """
    try:
        target = path or toolplane_config_path()
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _catalog_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else toolplane_config()
    section = cfg.get("tool_catalog")
    return section if isinstance(section, dict) else {}


def _scheduler_section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else toolplane_config()
    section = cfg.get("tool_scheduler")
    return section if isinstance(section, dict) else {}


def _coerce_mode(value: Any) -> ToolCatalogMode | None:
    """Parse one mode spelling; ``None`` when the value says nothing.

    Accepts the three mode names, and also the boolean spellings, because
    ``OMNIAGENTOS_TOOL_CATALOG=1`` is what a hand at a terminal actually types and
    silently ignoring it would be the worst outcome. Truthy maps to ``enforce``:
    the flag is named TOOL_CATALOG, and enabling a catalog that never enforces is not useful.
    The measurement ramp is one explicit word away (``=shadow``), whereas mapping
    truthy to ``shadow`` would leave an operator believing they had enabled
    protection they had not.

    ``bool`` is handled explicitly because YAML parses a bare ``off:`` value as
    ``False`` — an unquoted ``mode: off`` in the config file must mean the OFF
    MODE and not "the boolean false, which is unparseable".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "enforce" if value else "off"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in CATALOG_MODES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    return None


def tool_catalog_mode() -> ToolCatalogMode:
    """The effective tool-catalog mode: env override, else config, else ``off``.

    The env override is bidirectional: ``OMNIAGENTOS_TOOL_CATALOG=off`` disables
    the mechanism on a host whose config file enables it, which is the emergency
    lever. An UNPARSEABLE env value (a typo like ``=enfroce``) is ignored rather
    than treated as on.
    """
    from_env = _coerce_mode(os.environ.get(TOOL_CATALOG_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_mode(_catalog_section().get("mode"))
    if from_config is not None:
        return from_config
    return DEFAULT_CATALOG_MODE


def tool_catalog_enabled() -> bool:
    """True when tool catalog is active (``shadow`` or ``enforce``)."""
    return tool_catalog_mode() != "off"


def tool_catalog_enforcing() -> bool:
    """True only in ``enforce`` — i.e. when policies are actively blocked."""
    return tool_catalog_mode() == "enforce"


def tool_scheduler_mode() -> ToolCatalogMode:
    """The effective tool-scheduler mode: env override, else config, else ``off``."""
    from_env = _coerce_mode(os.environ.get(TOOL_SCHEDULER_ENV))
    if from_env is not None:
        return from_env
    from_config = _coerce_mode(_scheduler_section().get("mode"))
    if from_config is not None:
        return from_config
    return DEFAULT_SCHEDULER_MODE


def tool_scheduler_enabled() -> bool:
    """True when tool scheduler is active (``shadow`` or ``enforce``)."""
    return tool_scheduler_mode() != "off"


def tool_scheduler_enforcing() -> bool:
    """True only in ``enforce`` — i.e. when schedule queues are active."""
    return tool_scheduler_mode() == "enforce"


def core_tools() -> tuple[str, ...]:
    """Reads ``tool_catalog.core_tools``, coerces list of strings, drops blanks, caps at 5."""
    section = _catalog_section()
    val = section.get("core_tools")
    if not isinstance(val, list):
        return DEFAULT_CORE_TOOLS
    cleaned = []
    for item in val:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
    return tuple(cleaned[:5])


def _positive_int(value: Any) -> int | None:
    """A usable positive integer, or ``None`` for anything else.

    Non-positive is rejected rather than clamped, and boolean types are rejected.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def max_definitions() -> int:
    """Full definitions returned per search (config or default 5)."""
    val = _positive_int(_catalog_section().get("max_definitions"))
    return val if val is not None else DEFAULT_MAX_DEFINITIONS


def small_catalog_max_tools() -> int:
    """Small catalog tool count threshold (config or default 10)."""
    val = _positive_int(_catalog_section().get("small_catalog_max_tools"))
    return val if val is not None else DEFAULT_SMALL_CATALOG_TOOLS


def small_catalog_max_tokens() -> int:
    """Small catalog schema token limit (config or default 10000)."""
    val = _positive_int(_catalog_section().get("small_catalog_max_tokens"))
    return val if val is not None else DEFAULT_SMALL_CATALOG_TOKENS


def concurrency_budget() -> int:
    """Max calls in one concurrent wave (config or default 4)."""
    val = _positive_int(_scheduler_section().get("concurrency_budget"))
    return val if val is not None else DEFAULT_CONCURRENCY_BUDGET
