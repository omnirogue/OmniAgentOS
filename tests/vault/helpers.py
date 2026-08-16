"""Shared sample data + small git helpers for tests/vault (not a fixture
module — plain functions imported directly by test files)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def sample_run(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "run_test0000000000000001",
        "task_id": "tsk_test000000000000001",
        "task_title": "Fix flaky retry test",
        "discipline": "code-changes",
        "arm": "b1",
        "harness": "cli-claude",
        "model": "claude-sonnet",
        "state": "completed",
        "queued_at": "2026-07-11T15:20:00Z",
        "started_at": "2026-07-11T15:20:05Z",
        "finished_at": "2026-07-11T15:24:26Z",
        "wall_ms": 261000,
        "turns": 6,
        "input_tokens": 12345,
        "output_tokens": 2345,
        "cost_usd": 0.421,
        "usage_estimated": True,
        "usage_source": "cli-report",
        "trace_id": "trace-xxxx",
        "artifacts": ["patch.diff"],
    }
    base.update(overrides)
    return base


def sample_steps() -> list[dict[str, Any]]:
    return [
        {
            "seq": 1,
            "name": "plan",
            "status": "completed",
            "started_at": "2026-07-11T15:20:05Z",
            "finished_at": "2026-07-11T15:20:10Z",
        },
        {
            "seq": 2,
            "name": "apply_patch",
            "status": "completed",
            "started_at": "2026-07-11T15:20:10Z",
            "finished_at": "2026-07-11T15:24:00Z",
        },
    ]


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def commit_count(repo_dir: Path) -> int:
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo_dir, capture_output=True, text=True, check=True
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def commit_log(repo_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo_dir, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]
