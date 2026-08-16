"""Load CSI config. Defaults force the system off."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _ROOT / "configs" / "self_improvement.yaml"

_DEFAULT_FROZEN = (
    "omniagentos/selfimprove/**",
    "omniagentos/csi/**",
    "omniagentos/scheduler/routines*.py",
    "configs/policy.yaml",
    "configs/roots.yaml",
    "configs/self_improvement.yaml",
    "omniagentos/gates/**",
    "omniagentos/grants/**",
)


@dataclass(frozen=True)
class RoutineSpec:
    id: str
    window_days: int = 7
    planners: tuple[str, ...] = ("grok", "sol")
    enabled: bool = False


@dataclass(frozen=True)
class CsiConfig:
    enabled: bool = False
    global_halt: bool = False
    max_merges_per_day: int = 2
    planner_timeout_s: int = 900
    wall_clock_budget_s: int = 5400
    observation_window_days: int = 7
    min_panel_size: int = 2
    use_live_planners: bool = False
    frozen_surfaces: tuple[str, ...] = _DEFAULT_FROZEN
    routines: dict[str, RoutineSpec] = field(default_factory=dict)

    def is_runnable(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "self_improvement.enabled is false"
        if self.global_halt:
            return False, "self_improvement.global_halt is true"
        return True, "ok"

    def routine(self, routine_id: str) -> RoutineSpec | None:
        return self.routines.get(routine_id)


def _parse_routines(raw: dict[str, Any]) -> dict[str, RoutineSpec]:
    out: dict[str, RoutineSpec] = {}
    block = raw.get("routines") or {}
    if not isinstance(block, dict):
        return out
    for key, spec in block.items():
        if not isinstance(spec, dict):
            continue
        rid = str(spec.get("id") or key)
        planners = tuple(str(p) for p in (spec.get("planners") or ["grok", "sol"]))
        out[rid] = RoutineSpec(
            id=rid,
            window_days=int(spec.get("window_days") or 7),
            planners=planners,
            enabled=bool(spec.get("enabled", False)),
        )
    return out


@lru_cache(maxsize=4)
def load_csi_config(path: str | None = None) -> CsiConfig:
    cfg_path = Path(path) if path else _DEFAULT_PATH
    if not cfg_path.is_file():
        return CsiConfig(
            routines={
                "self_learning": RoutineSpec(
                    id="self_learning",
                    window_days=7,
                    planners=("grok", "sol", "kimi"),
                    enabled=True,
                )
            }
        )
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    frozen = data.get("frozen_surfaces") or list(_DEFAULT_FROZEN)
    return CsiConfig(
        enabled=bool(data.get("enabled", False)),
        global_halt=bool(data.get("global_halt", False)),
        max_merges_per_day=int(data.get("max_merges_per_day") or 2),
        planner_timeout_s=int(data.get("planner_timeout_s") or 900),
        wall_clock_budget_s=int(data.get("wall_clock_budget_s") or 5400),
        observation_window_days=int(data.get("observation_window_days") or 7),
        # Never allow unchallenged single-planner propose (Opus CSI P1).
        min_panel_size=max(2, int(data.get("min_panel_size") or 2)),
        use_live_planners=bool(data.get("use_live_planners", False)),
        frozen_surfaces=tuple(str(x) for x in frozen),
        routines=_parse_routines(data),
    )


def clear_csi_config_cache() -> None:
    load_csi_config.cache_clear()


__all__ = ["CsiConfig", "RoutineSpec", "clear_csi_config_cache", "load_csi_config"]
