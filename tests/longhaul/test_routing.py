"""Tests for routing.py: worker ranking, registry parsing, fallback behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.longhaul.routing import rank_workers


@pytest.fixture
def tmp_registry(tmp_path: Path) -> str:
    """Create a temporary registry.json for testing."""
    registry_dir = tmp_path / "var" / "modelintel"
    registry_dir.mkdir(parents=True, exist_ok=True)

    registry = {
        "models": {
            "opus": {
                "scores": {
                    "coding-implementation": 0.95,
                    "agentic-tool-use": 0.90,
                },
                "measured_latency_ms": 3000,
                "pricing": {"per_token": 0.00015},
            },
            "sonnet": {
                "scores": {
                    "coding-implementation": 0.85,
                    "agentic-tool-use": 0.80,
                },
                "measured_latency_ms": 2000,
                "pricing": {"per_token": 0.00003},
            },
            "gpt-5.6-sol": {
                "scores": {
                    "coding-implementation": 0.80,
                    "agentic-tool-use": 0.75,
                },
                "measured_latency_ms": 4000,
                "pricing": {"per_token": 0.00001},
            },
            "grok-4.5": {
                "scores": {
                    "coding-implementation": 0.84,
                    "agentic-tool-use": 0.82,
                },
                "measured_latency_ms": 1800,
                "pricing": {"per_token": 0.00001},
            },
            "gemini-3.1-pro": {
                "scores": {
                    "coding-implementation": 0.72,
                    "agentic-tool-use": 0.74,
                },
                "measured_latency_ms": 900,
                "pricing": {"per_token": 0.00001},
            },
            "kimi-k3": {
                "scores": {
                    "coding-implementation": 0.76,
                    "agentic-tool-use": 0.70,
                },
                "measured_latency_ms": 6000,
                "pricing": {"per_token": 0.00001},
            },
            "fable": {
                "scores": {
                    "coding-implementation": 0.70,
                    "agentic-tool-use": 0.65,
                },
                "measured_latency_ms": 1000,
                "pricing": {"per_token": 0.00001},
            },
        }
    }

    registry_path = registry_dir / "registry.json"
    registry_path.write_text(json.dumps(registry))
    return str(registry_path)


@pytest.fixture
def default_config() -> dict:
    """Return default config for tests."""
    return {
        "weights": {"quality": 0.6, "speed": 0.25, "cost": 0.15},
        "excluded_models": ["fable"],
        "cross_harness_fallback": True,
        "static_fallback_order": [
            {"harness": "cli-claude", "model": "opus"},
            {"harness": "cli-claude", "model": "sonnet"},
            {"harness": "cli-codex", "model": "gpt-5.6-sol"},
            {"harness": "cli-grok", "model": "grok-4.5"},
            {"harness": "cli-gemini", "model": "gemini-3.1-pro"},
            {"harness": "cli-kimi", "model": "kimi-k3"},
        ],
    }


class TestRankWorkers:
    """Worker ranking."""

    def test_rank_workers_from_registry(self, tmp_registry: str, default_config: dict) -> None:
        """rank_workers parses registry and scores models."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=2)
        assert len(workers) > 0
        assert all("harness" in w and "model" in w and "score" in w for w in workers)

    def test_rank_workers_claude_first_with_capacity(
        self, tmp_registry: str, default_config: dict
    ) -> None:
        """With claude_capacity > 0, claude models ranked first."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=2)
        # First workers should be cli-claude
        claude_workers = [w for w in workers if w["harness"] == "cli-claude"]
        if claude_workers:
            assert workers[0]["harness"] == "cli-claude"

    def test_rank_workers_non_claude_first_no_capacity(
        self, tmp_registry: str, default_config: dict
    ) -> None:
        """With no Claude capacity, the best scored non-Claude CLI ranks first."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=0)
        fallback_workers = [worker for worker in workers if worker["harness"] != "cli-claude"]
        if fallback_workers and default_config["cross_harness_fallback"]:
            assert workers[0]["harness"] != "cli-claude"
            assert [worker["score"] for worker in fallback_workers] == sorted(
                (worker["score"] for worker in fallback_workers), reverse=True
            )

    def test_rank_workers_fable_excluded(self, tmp_registry: str, default_config: dict) -> None:
        """Fable is excluded via excluded_models."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=2)
        models = [w["model"] for w in workers]
        assert "fable" not in models

    def test_rank_workers_static_fallback(self, tmp_path: Path, default_config: dict) -> None:
        """Missing registry falls back to static_fallback_order."""
        missing_registry = str(tmp_path / "nonexistent.json")
        workers = rank_workers(missing_registry, default_config, claude_capacity=2)
        assert len(workers) > 0
        # Should match static fallback
        assert workers[0]["model"] in ["opus", "sonnet", "gpt-5.6-sol"]

    def test_rank_workers_has_why_field(self, tmp_registry: str, default_config: dict) -> None:
        """Each ranked worker has a 'why' field explaining score."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=2)
        for worker in workers:
            assert "why" in worker
            assert worker["why"]

    def test_rank_workers_sorted_by_score(self, tmp_registry: str, default_config: dict) -> None:
        """Workers of same harness are sorted by score descending."""
        workers = rank_workers(tmp_registry, default_config, claude_capacity=2)
        claude_workers = [w for w in workers if w["harness"] == "cli-claude"]
        if len(claude_workers) > 1:
            # Check that scores descend
            scores = [w["score"] for w in claude_workers]
            assert scores == sorted(scores, reverse=True)

    def test_registry_exposes_each_real_non_claude_adapter_path(
        self, tmp_registry: str, default_config: dict
    ) -> None:
        workers = rank_workers(tmp_registry, default_config, claude_capacity=0)
        pairs = {(worker["harness"], worker["model"]) for worker in workers}
        assert {
            ("cli-codex", "gpt-5.6-sol"),
            ("cli-grok", "grok-4.5"),
            ("cli-gemini", "gemini-3.1-pro"),
            ("cli-kimi", "kimi-k3"),
        }.issubset(pairs)

    def test_static_provider_pair_validation_rejects_crossed_harness(
        self, tmp_path: Path, default_config: dict
    ) -> None:
        default_config["static_fallback_order"] = [
            {"harness": "cli-grok", "model": "gemini-3.1-pro"},
            {"harness": "cli-grok", "model": "grok-4.5"},
        ]
        workers = rank_workers(str(tmp_path / "missing.json"), default_config, claude_capacity=0)
        assert [(worker["harness"], worker["model"]) for worker in workers] == [
            ("cli-grok", "grok-4.5")
        ]
