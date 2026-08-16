"""Daily model-intelligence update — the launchd entry point.

Order matters: fetch (cheap, deterministic) -> research (one Grok web-search
sweep) -> registry build (merge + score) -> fusion digest -> fusion rankings
refresh -> vault notes. Every stage is best-effort; the registry keeps
last-known-good rows for any source that failed today, so a bad morning on
one leaderboard degrades freshness, never availability. A one-line JSON
summary is appended to var/modelintel/update.log for the operator.
"""

from __future__ import annotations

import json
import os
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.modelintel import registry as registry_mod
from omniagentos.modelintel import research as research_mod
from omniagentos.modelintel import vault_notes
from omniagentos.modelintel.config import load_config, var_dir, xai_api_key
from omniagentos.modelintel.sources import fetch_all


def run(research_enabled: bool | None = None) -> dict[str, Any]:
    cfg = load_config()
    if research_enabled is None:
        research_enabled = os.environ.get("OMNIAGENTOS_MODELINTEL_RESEARCH", "1") != "0"

    fetches = fetch_all(cfg)
    research_result = None
    api_key = xai_api_key()
    if research_enabled and api_key:
        research_result = research_mod.sweep(cfg, api_key)

    previous = registry_mod.load_previous()
    registry = registry_mod.build(cfg, fetches, research_result, previous)
    registry_path = registry_mod.save(registry)
    digest_path = registry_mod.write_fusion_digest(cfg, registry)
    rankings = registry_mod.refresh_fusion_rankings(cfg, registry)
    notes = vault_notes.render_all(cfg, registry)

    summary: dict[str, Any] = {
        "at": utc_now_iso(),
        "sources": {
            key: ("ok" if status.ok else f"failed: {status.error}")
            for key, status in registry.sources.items()
        },
        "research": (
            "disabled"
            if not research_enabled
            else "no-key"
            if not api_key
            else "ok"
            if research_result and research_result.ok
            else f"failed: {research_result.error if research_result else 'unknown'}"
        ),
        "models": len(registry.models),
        "watchlist": len(registry.watchlist),
        "notesWritten": len(notes),
        "registry": str(registry_path),
        "digest": str(digest_path),
        # A corrupt/wrong-shape rankings file is a FAILURE (Fusion keeps routing
        # on stale constants) and must never share wording with the benign
        # "file not created yet" case — describe() carries the real reason.
        "rankings": rankings.describe(),
    }
    log_path = var_dir() / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary) + "\n")
    return summary


def main() -> None:
    print(json.dumps(run(), indent=2))


if __name__ == "__main__":
    main()
