from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.swarm.router import SwarmRouter, healthy_providers_from_snapshot

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _agent(provider: str, model: str) -> dict[str, Any]:
    return {
        "id": f"{provider}-coder",
        "provider": provider,
        "model": model,
        "role": "coder",
        "capabilityTier": "architectural",
        "maxReasoning": "xhigh",
        "available": True,
        "warmLatencyMs": 100,
        "codingScore": 0.9,
        "toolUseScore": 0.9,
        "costScore": 0.5,
    }


def _router(health: set[str] | None) -> SwarmRouter:
    return SwarmRouter(
        lineage_providers={},
        rankings_loader=lambda: {
            "codex-coder": _agent("codex", "gpt-5.6-sol"),
            "gemini-coder": _agent("gemini", "gemini-3.1-pro-preview"),
        },
        digest_loader=lambda: None,
        provider_health_loader=lambda: health,
    )


def test_failed_provider_is_excluded_from_candidates() -> None:
    candidates = _router({"gemini"})._candidates("fusionbuild", "moderate", "medium", "standard")

    assert {candidate.provider for candidate in candidates} == {"gemini"}


def test_passing_provider_remains_routable() -> None:
    candidates = _router({"codex", "gemini"})._candidates(
        "fusionbuild", "moderate", "medium", "standard"
    )

    assert {candidate.provider for candidate in candidates} == {"codex", "gemini"}


def test_missing_health_data_gracefully_preserves_candidates() -> None:
    candidates = _router(None)._candidates("fusionbuild", "moderate", "medium", "standard")

    assert {candidate.provider for candidate in candidates} == {"codex", "gemini"}


def test_snapshot_requires_fresh_all_account_checks(tmp_path: Path) -> None:
    path = tmp_path / "provider-health.json"
    path.write_text(
        json.dumps(
            {
                "ts": NOW.isoformat(),
                "results": {
                    "codex:default": {"provider": "codex", "ok": True},
                    "gemini:default": {"provider": "gemini", "ok": False},
                },
            }
        ),
        encoding="utf-8",
    )
    assert healthy_providers_from_snapshot(path, now=lambda: NOW) == {"codex"}

    path.write_text(
        json.dumps({"ts": (NOW - timedelta(hours=37)).isoformat(), "results": {}}),
        encoding="utf-8",
    )
    assert healthy_providers_from_snapshot(path, now=lambda: NOW) is None
