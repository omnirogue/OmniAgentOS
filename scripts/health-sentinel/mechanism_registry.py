"""Strict reader for the durable mechanism-registry JSONL index.

The durable operator record lives outside the repository at
``/Users/youruser/Work/Ops/mechanism-registry``.  JSONL is deliberately used
instead of YAML so the sentinel does not acquire a parser dependency and so its
schema comment can live in the file without becoming data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MECHANISM_REGISTRY_PATH = Path(
    os.environ.get("OMNIAGENTOS_MECHANISM_REGISTRY")
    or (Path.home() / "Work" / "Ops" / "mechanism-registry" / "registry.jsonl")
)
_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "schedule",
        "expected_output_path",
        "freshness_SLA",
        "named_consumer",
        "state",
        "enabled",
        "installed",
        "launchd_label",
        "last_known_good_evidence",
    }
)
_STATES = frozenset({"enabled", "disabled"})


def _validate(entry: Any, line_number: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(entry, dict):
        return None, f"line {line_number}: entry is not an object"
    missing = sorted(_REQUIRED_FIELDS - entry.keys())
    if missing:
        return None, f"line {line_number}: missing required field(s) {', '.join(missing)}"
    for field in ("id", "schedule", "expected_output_path", "last_known_good_evidence"):
        if not isinstance(entry[field], str) or not entry[field].strip():
            return None, f"line {line_number}: {field} must be a non-empty string"
    if entry["state"] not in _STATES:
        return None, f"line {line_number}: state must be enabled or disabled"
    if not isinstance(entry["enabled"], bool) or not isinstance(entry["installed"], bool):
        return None, f"line {line_number}: enabled and installed must be booleans"
    if entry["enabled"] != (entry["state"] == "enabled"):
        return None, f"line {line_number}: state and enabled disagree"
    if not isinstance(entry["named_consumer"], list) or not all(
        isinstance(value, str) for value in entry["named_consumer"]
    ):
        return None, f"line {line_number}: named_consumer must be a list of strings"
    freshness = entry["freshness_SLA"]
    if freshness is not None and (
        not isinstance(freshness, (int, float))
        or isinstance(freshness, bool)
        or freshness < 0
    ):
        return None, f"line {line_number}: freshness_SLA must be seconds or null"
    label = entry["launchd_label"]
    if label is not None and (not isinstance(label, str) or not label.strip()):
        return None, f"line {line_number}: launchd_label must be a non-empty string or null"
    return entry, None


def load_registry(path: Path = MECHANISM_REGISTRY_PATH) -> tuple[list[dict[str, Any]], str | None]:
    """Read and validate every JSONL entry, returning one total error on failure."""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], f"could not read registry {path} ({type(exc).__name__}: {exc})"
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(rows, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            raw = json.loads(text)
        except ValueError as exc:
            return [], f"line {line_number}: invalid JSON ({exc})"
        entry, error = _validate(raw, line_number)
        if error:
            return [], error
        assert entry is not None
        mechanism_id = entry["id"]
        if mechanism_id in seen:
            return [], f"line {line_number}: duplicate id {mechanism_id!r}"
        seen.add(mechanism_id)
        entries.append(entry)
    if not entries:
        return [], f"registry {path} has no entries"
    return entries, None
