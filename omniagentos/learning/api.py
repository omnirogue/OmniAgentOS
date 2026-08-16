"""Small append-only decision/outcome log for learning integrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MEMORY_LOG: list[dict[str, Any]] = []


def _record(kind: str, payload: Mapping[str, Any], path: str | Path | None) -> dict[str, Any]:
    row = {"kind": kind, **dict(payload)}
    if path is None:
        _MEMORY_LOG.append(row)
    else:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def log_decision(decision: Mapping[str, Any], *, path: str | Path | None = None) -> dict[str, Any]:
    return _record("decision", decision, path)


def attach_outcome(
    decision_id: str, outcome: Mapping[str, Any], *, path: str | Path | None = None
) -> dict[str, Any]:
    return _record("outcome", {"decision_id": decision_id, "outcome": dict(outcome)}, path)


def promote_champion(
    champion: str, *, evidence: Mapping[str, Any] | None = None, path: str | Path | None = None
) -> dict[str, Any]:
    return _record(
        "champion_promoted", {"champion": champion, "evidence": dict(evidence or {})}, path
    )
