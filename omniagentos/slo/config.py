"""Machine-readable SLO configuration loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_slos(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the packaged SLO definitions, validating their small public shape."""
    config_path = Path(path) if path is not None else Path(__file__).with_name("slos.yaml")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    slos = raw.get("slos") if isinstance(raw, dict) else None
    if not isinstance(slos, dict) or "swarm_completion" not in slos:
        raise ValueError(f"SLO config {config_path} must define swarm_completion")
    swarm_completion = slos["swarm_completion"]
    if not isinstance(swarm_completion, dict):
        raise ValueError("swarm_completion SLO must be a mapping")
    required = {"name", "target", "window_hours", "metric", "description"}
    missing = required - set(swarm_completion)
    if missing:
        raise ValueError(f"swarm_completion SLO missing keys: {sorted(missing)}")
    return {str(key): dict(value) for key, value in slos.items() if isinstance(value, dict)}
