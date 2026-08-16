"""Shared sample data for tests/lab/vault (not a fixture module — plain
functions imported directly by test files, mirroring tests/vault/helpers.py's
style). Shapes match the raw LabStore row columns (contracts/schema.sql
migration 003) — `*_json` fields are JSON-encoded strings, exactly what
`LabStore.get_experiment` / `.eval_results` / etc. return."""

from __future__ import annotations

import json
from typing import Any


def sample_experiment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "exp_test0000000000000001",
        "hypothesis": "Adding a regression-test requirement improves bugfix correctness",
        "discipline": "coding",
        "explore_policy": "exploit",
        "mutable_surface_kind": "prompt",
        "champion_surface_id": "srf_champion0000000001",
        "challenger_surface_id": "srf_challenger000001",
        "eval_suite_id": "evs_coding0000000001",
        "dataset_hash": "ds-hash-1",
        "primary_metric": "pass_rate",
        "budgets_json": json.dumps({"wall_minutes": 30.0, "replicates": 2}),
        "promotion_json": json.dumps({"primary_delta_min": 0.03}),
        "status": "decided",
        "disposition": "promote",
        "scorecard_json": None,
        "snapshot_hash": "snap-1",
        "created_at": "2026-07-11T10:00:00Z",
        "updated_at": "2026-07-11T12:00:00Z",
    }
    base.update(overrides)
    return base


def sample_eval_results() -> list[dict[str, Any]]:
    return [
        {
            "id": "evr_champion0000000001",
            "experiment_id": "exp_test0000000000000001",
            "arm": "champion",
            "suite_id": "evs_coding0000000001",
            "suite_version": 1,
            "split": "dev",
            "metrics_json": json.dumps({"pass_rate": 0.72, "cost_usd": 0.10}),
            "per_case_json": json.dumps({"c1": {"pass_rate": 1.0}, "c2": {"pass_rate": 0.0}}),
            "replicate": 0,
            "deterministic_passed": 1,
            "created_at": "2026-07-11T11:00:00Z",
        },
        {
            "id": "evr_challenger000001",
            "experiment_id": "exp_test0000000000000001",
            "arm": "challenger",
            "suite_id": "evs_coding0000000001",
            "suite_version": 1,
            "split": "dev",
            "metrics_json": json.dumps({"pass_rate": 0.81, "cost_usd": 0.11}),
            "per_case_json": json.dumps({"c1": {"pass_rate": 1.0}, "c2": {"pass_rate": 1.0}}),
            "replicate": 0,
            "deterministic_passed": 1,
            "created_at": "2026-07-11T11:05:00Z",
        },
    ]


def sample_scorecard(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "champion": {"pass_rate": 0.72, "cost_usd": 0.10},
        "challenger": {"pass_rate": 0.81, "cost_usd": 0.11},
        "primary_delta": 0.09,
        "utility": 0.07,
        "complexity_delta": 0.01,
        "confidence_interval": [0.02, 0.15],
        "safety_regression": False,
        "audit_flags": [],
    }
    base.update(overrides)
    return base


def sample_surface(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "srf_champion0000000001",
        "kind": "prompt",
        "discipline": "coding",
        "version": 3,
        "path": "prompts/coder/v3.md",
        "content_hash": "sha256:abc123",
        "parent_version": 2,
        "status": "champion",
        "label": "coder v3",
        "safety_relevant": 0,
        "created_at": "2026-07-10T10:00:00Z",
    }
    base.update(overrides)
    return base


def sample_tournament(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "tnm_test0000000000000001",
        "subject": "coding-orchestration",
        "discipline": "coding",
        "arena_task_hash": "arena-hash-1",
        "config_ids_json": json.dumps(["srf_champion0000000001", "srf_challenger000001"]),
        "winner_config_id": "srf_challenger000001",
        "status": "done",
        "created_at": "2026-07-11T13:00:00Z",
    }
    base.update(overrides)
    return base


def sample_matches() -> list[dict[str, Any]]:
    return [
        {
            "id": "mch_test0000000001",
            "tournament_id": "tnm_test0000000000000001",
            "config_a": "srf_champion0000000001",
            "config_b": "srf_challenger000001",
            "winner": "srf_challenger000001",
            "score_a": 0.4,
            "score_b": 0.6,
            "judge_notes": "Challenger's regression tests caught an edge case the champion missed.",
            "blind": 1,
            "created_at": "2026-07-11T13:05:00Z",
        }
    ]


def sample_elo() -> list[dict[str, Any]]:
    return [
        {
            "subject": "coding-orchestration",
            "config_id": "srf_champion0000000001",
            "rating": 988.0,
            "matches": 1,
            "wins": 0,
            "losses": 1,
            "draws": 0,
            "updated_at": "2026-07-11T13:05:00Z",
        },
        {
            "subject": "coding-orchestration",
            "config_id": "srf_challenger000001",
            "rating": 1012.0,
            "matches": 1,
            "wins": 1,
            "losses": 0,
            "draws": 0,
            "updated_at": "2026-07-11T13:05:00Z",
        },
    ]


def sample_leaderboard_rows() -> list[dict[str, Any]]:
    return [
        {
            "subject": "coding-orchestration",
            "discipline": "coding",
            "rank": 1,
            "config_id": "srf_challenger000001",
            "elo": 1012.0,
            "summary": "Adds a regression-test-writing stage after the fix.",
            "judge_notes": "Consistently catches edge cases; slightly higher cost.",
            "source_experiments_json": json.dumps(["exp_test0000000000000001"]),
            "updated_by": "curator",
            "updated_at": "2026-07-11T14:00:00Z",
        },
        {
            "subject": "coding-orchestration",
            "discipline": "coding",
            "rank": 2,
            "config_id": "srf_champion0000000001",
            "elo": 988.0,
            "summary": "Baseline single-pass fixer.",
            "judge_notes": "Reliable but occasionally misses edge cases.",
            "source_experiments_json": json.dumps(["exp_test0000000000000001"]),
            "updated_by": "curator",
            "updated_at": "2026-07-11T14:00:00Z",
        },
    ]


def sample_playbook_entries() -> list[dict[str, Any]]:
    return [
        {
            "id": "pbk_test0000000001",
            "trait": "Add a regression-test-writing stage after bugfixes",
            "discipline": "coding",
            "evidence_experiments_json": json.dumps(["exp_test0000000000000001"]),
            "evidence_tournaments_json": json.dumps(["tnm_test0000000000000001"]),
            "scope": "bugfix workflows",
            "confidence": "high",
            "elo_support": 24.0,
            "status": "validated",
            "created_at": "2026-07-11T14:30:00Z",
        },
        {
            "id": "pbk_test0000000002",
            "trait": "Prefer smaller diffs when scores tie",
            "discipline": "coding",
            "evidence_experiments_json": json.dumps([]),
            "evidence_tournaments_json": json.dumps([]),
            "scope": "",
            "confidence": "low",
            "elo_support": None,
            "status": "provisional",
            "created_at": "2026-07-09T10:00:00Z",
        },
    ]
