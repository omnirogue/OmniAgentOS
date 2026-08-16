"""Shared sample data for tests/selfimprove (plain functions, not fixtures —
imported directly by test files, mirrors tests/vault/helpers.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omniagentos.selfimprove.models import GateStatus, SkillMetadata, VerificationGate


def write_status_json(run_dir: Path, **overrides: Any) -> Path:
    """Write a Fusion-worker-shaped status.json into run_dir (mirrors the
    real schema written to .fusion/runs/*/sessions/*/status.json)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "sessionId": "w4-test-1",
        "package": "w4-skillcapture",
        "agent": "claude-coder",
        "model": "sonnet/high",
        "state": "done",
        "started_at": "2026-07-20T12:00:00Z",
        "completed_at": "2026-07-20T12:30:00Z",
        "commits": [{"hash": "abc1234", "message": "fusion(w4): example commit"}],
        "validation": {"pytest": "12 passed", "ruff": "clean", "mypy": "clean"},
    }
    data.update(overrides)
    path = run_dir / "status.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def sample_gate(**overrides: Any) -> VerificationGate:
    base: dict[str, Any] = {
        "status": GateStatus.PASSED,
        "source_run_id": "run_test0000000000000001",
        "evidence": "12 passed, 0 failed; ruff/mypy clean",
    }
    base.update(overrides)
    return VerificationGate(**base)


def sample_metadata(**overrides: Any) -> SkillMetadata:
    base: dict[str, Any] = {
        "skill_id": "add-additive-migration",
        "title": "Add an additive SQLite migration",
        "discipline": "code-changes",
        "summary": "Adds a new NNN_*.sql migration file and applies it idempotently.",
        "input_format": "A short description of the new column/table needed.",
        "steps": [
            "Read the latest omniagentos/db/migrations/NNN_*.sql for the next number.",
            "Write an additive-only NNN_description.sql (no destructive edits).",
            "Run python -m omniagentos.db.migrate against a throwaway copy of the db.",
        ],
        "output_structure": "A new omniagentos/db/migrations/NNN_*.sql file, migration applied.",
        "validation_rules": [
            "Migration must be additive only (no DROP/ALTER destructive ops).",
            "python -m omniagentos.db.migrate must succeed twice in a row (idempotent).",
        ],
        "tags": ["db", "migrations"],
    }
    base.update(overrides)
    return SkillMetadata(**base)
