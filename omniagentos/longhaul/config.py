"""Longhaul config loader with validation and defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Nested maps that deep-merge with defaults rather than wholesale replace.
_NESTED_KEYS = frozenset({"weights", "static_fallback_order", "review", "prep"})


def load_config(path: str = "configs/longhaul.yaml") -> dict[str, Any]:
    """Load and validate longhaul config, applying defaults.

    Args:
        path: path to config file (relative to repo root)

    Returns:
        Validated config dict with defaults applied

    Unknown top-level keys under ``longhaul:`` are preserved (H-29). The previous
    implementation only copied keys already present in ``_default_config()``,
    which silently dropped ``attempt_wall_ms`` and left the Codex longhaul path
    without a wall-clock timeout.
    """
    config_path = Path(path)

    # Load YAML
    if not config_path.exists():
        # Return defaults if config doesn't exist
        return _default_config()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        # Return defaults on load error
        return _default_config()

    # Extract longhaul section
    longhaul_cfg = data.get("longhaul", {})

    # Apply defaults for missing keys
    defaults = _default_config()
    result = defaults.copy()

    if isinstance(longhaul_cfg, dict):
        for key, value in longhaul_cfg.items():
            if (
                key in _NESTED_KEYS
                and isinstance(value, dict)
                and isinstance(result.get(key), dict)
            ):
                # Deep-merge nested maps so partial YAML still inherits defaults.
                merged = dict(result[key])
                merged.update(value)
                result[key] = merged
            elif key == "static_fallback_order" and isinstance(value, list):
                result[key] = value
            else:
                # Preserve unknown keys (attempt_wall_ms, fast_crash_*, …).
                result[key] = value

    return result


def _default_config() -> dict[str, Any]:
    """Return default config."""
    return {
        "default_for_planned_coding": True,
        "max_sessions": 8,
        "default_cooldown_s": 3600,
        # Per-session idle threshold (minutes) the engine persists onto every
        # longhaul session row at spawn; the A2 reaper enforces it (D8).
        "idle_minutes": 45,
        # Codex / non-Claude executor wall-clock budget (milliseconds).
        # The engine clamps non-positive values to an immediate positive wall;
        # this containment boundary cannot be disabled from YAML.
        "attempt_wall_ms": 1_800_000,
        # A timed-out process group receives TERM, then KILL, and every pipe
        # drain/reap remains bounded by these two horizons (H-29).
        "attempt_term_grace_ms": 3_000,
        "attempt_kill_reap_ms": 3_000,
        # Explicit operator elevation for an unattended CLI that the adapter
        # would otherwise refuse (notably force-auto Kimi / no-sandbox hosts).
        "unattended_elevated_harnesses": [],
        # Fast-crash loop guard (L-13): attempts that die within fast_crash_s
        # of start accumulate exponential dispatch backoff. Set fast_crash_s
        # to 0 to disable (tests).
        "fast_crash_s": 30,
        "fast_crash_backoff_s": 5,
        "fast_crash_max_backoff_s": 300,
        "excluded_models": ["fable"],
        "cross_harness_fallback": True,
        "weights": {
            "quality": 0.6,
            "speed": 0.25,
            "cost": 0.15,
        },
        "static_fallback_order": [
            {"harness": "cli-claude", "model": "opus"},
            {"harness": "cli-claude", "model": "sonnet"},
            {"harness": "cli-codex", "model": "gpt-5.6-sol"},
            {"harness": "cli-grok", "model": "grok-4.5"},
            {"harness": "cli-gemini", "model": "gemini-3.1-pro"},
            {"harness": "cli-kimi", "model": "kimi-k3"},
        ],
        "review": {
            "enabled": True,
            "harness": "cli-codex",
            "deny_respawns": 2,
            "unavailable_retries": 3,
            "backoff_s": 30,
            "max_backoff_s": 900,
        },
        "prep": {
            "model": "sonnet",
            "wall_ms": 90000,
        },
    }
