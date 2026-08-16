"""Shared loader for configs/coverage_policy.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def repo_root() -> Path:
    """Repo root = parent of the omniagentos package (cwd-independent)."""
    return Path(__file__).resolve().parents[2]


def default_policy_path() -> Path:
    override = os.environ.get("OMNIAGENTOS_COVERAGE_POLICY")
    if override:
        return Path(override).expanduser()
    return repo_root() / "configs" / "coverage_policy.yaml"


@lru_cache(maxsize=4)
def load_policy_dict(path: str | None = None) -> dict[str, Any]:
    policy_path = Path(path) if path else default_policy_path()
    raw = policy_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"coverage policy must be a mapping: {policy_path}")
    return data


def clear_policy_cache() -> None:
    load_policy_dict.cache_clear()
