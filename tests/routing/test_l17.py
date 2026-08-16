"""L-17: ranking freshness + real feedback terms against production registry shape."""

from __future__ import annotations

import json
import time
from pathlib import Path

from omniagentos.longhaul.routing import (
    REGISTRY_FRESHNESS_TTL_SECONDS,
    rank_workers,
)


def _cfg() -> dict:
    return {
        "weights": {"quality": 0.6, "speed": 0.25, "cost": 0.15},
        "excluded_models": ["fable"],
        "cross_harness_fallback": True,
        "static_fallback_order": [
            {"harness": "cli-claude", "model": "opus"},
            {"harness": "cli-claude", "model": "sonnet"},
            {"harness": "cli-codex", "model": "gpt-5.6-sol"},
        ],
    }


def _write_production_shape(
    path: Path,
    *,
    updated_at: str | int | float,
    models: list[dict] | None = None,
) -> str:
    """Write a registry matching real modelintel schema (list + ISO updated_at)."""
    if models is None:
        models = [
            {
                "key": "opus",
                "title": "Opus",
                "provider": "anthropic",
                "lineage": "claude",
                "pricing": {"completion_usd_per_m": 75.0},
                "measured_latency_ms": 3000,
                "capabilities": {
                    "coding-implementation": {"score": 0.95, "basis": ["prior"]},
                    "agentic-tool-use": {"score": 0.90, "basis": ["prior"]},
                },
            },
            {
                "key": "sonnet",
                "title": "Sonnet",
                "provider": "anthropic",
                "lineage": "claude",
                "pricing": {"completion_usd_per_m": 15.0},
                "measured_latency_ms": 2000,
                "capabilities": {
                    "coding-implementation": {"score": 0.85, "basis": ["prior"]},
                    "agentic-tool-use": {"score": 0.80, "basis": ["prior"]},
                },
            },
            {
                "key": "gpt-5.6-sol",
                "title": "Sol",
                "provider": "openai",
                "lineage": "codex",
                "pricing": {"completion_usd_per_m": 30.0},
                "measured_latency_ms": 4000,
                "capabilities": {
                    "coding-implementation": {"score": 0.80, "basis": ["prior"]},
                    "agentic-tool-use": {"score": 0.75, "basis": ["prior"]},
                },
            },
        ]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": updated_at,
                "sources": {},
                "models": models,
                "watchlist": [],
                "releases": [],
                "top_by_domain": {},
            }
        )
    )
    return str(path)


def test_fresh_production_registry_scores_from_capabilities(tmp_path: Path) -> None:
    reg = _write_production_shape(
        tmp_path / "registry.json",
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    workers = rank_workers(reg, _cfg(), claude_capacity=2)
    assert workers
    assert workers[0]["harness"] == "cli-claude"
    # No fabricated feedback blend when entry has no real feedback field.
    assert "fb=omit" in workers[0]["why"]
    assert "[static_fallback]" not in workers[0]["why"]


def test_stale_registry_falls_back_to_static(tmp_path: Path) -> None:
    # 30 days old → stale
    stale_ts = time.time() - (REGISTRY_FRESHNESS_TTL_SECONDS + 86400)
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stale_ts))
    reg = _write_production_shape(tmp_path / "registry.json", updated_at=stale_iso)
    workers = rank_workers(reg, _cfg(), claude_capacity=2)
    assert workers
    assert all("[static_fallback]" in w["why"] for w in workers)
    # Static order: opus then sonnet when claude capacity > 0
    assert workers[0]["model"] == "opus"


def test_epoch_zero_updated_at_forces_static_fallback(tmp_path: Path) -> None:
    reg = _write_production_shape(tmp_path / "registry.json", updated_at=0)
    workers = rank_workers(reg, _cfg(), claude_capacity=2)
    assert workers
    assert all("[static_fallback]" in w["why"] for w in workers)


def test_missing_updated_at_forces_static_fallback(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "key": "opus",
                        "capabilities": {
                            "coding-implementation": {"score": 0.99},
                            "agentic-tool-use": {"score": 0.99},
                        },
                    }
                ],
            }
        )
    )
    workers = rank_workers(str(path), _cfg(), claude_capacity=2)
    assert workers
    assert all("[static_fallback]" in w["why"] for w in workers)


def test_explicit_feedback_blended_when_present(tmp_path: Path) -> None:
    models = [
        {
            "key": "opus",
            "measured_latency_ms": 3000,
            "pricing": {"completion_usd_per_m": 75.0},
            "capabilities": {
                "coding-implementation": {"score": 0.90},
                "agentic-tool-use": {"score": 0.90},
            },
            # Real explicit feedback field — only then is the blend used.
            "recent_success_rate": 0.1,
        },
        {
            "key": "sonnet",
            "measured_latency_ms": 2000,
            "pricing": {"completion_usd_per_m": 15.0},
            "capabilities": {
                "coding-implementation": {"score": 0.80},
                "agentic-tool-use": {"score": 0.80},
            },
            "recent_success_rate": 0.95,
        },
    ]
    reg = _write_production_shape(
        tmp_path / "registry.json",
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        models=models,
    )
    workers = rank_workers(reg, _cfg(), claude_capacity=2)
    claude = [w for w in workers if w["harness"] == "cli-claude"]
    assert claude
    assert "fb=0.95" in claude[0]["why"] or claude[0]["model"] == "sonnet"
    # Higher explicit success rate should rank sonnet above opus despite lower caps.
    assert claude[0]["model"] == "sonnet"
