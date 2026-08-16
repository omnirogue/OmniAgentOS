"""Load-time invariants for omniagentos.integration.config."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.integration.config import (
    DEFAULT_CONFIG_PATH,
    load_integration_config,
)

REPO = Path(__file__).resolve().parents[2]
VENV_PYTHON = Path("/Users/youruser/OmniAgentOS/.venv/bin/python")


def _write_config(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(
        yaml.safe_dump({"integration": body}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _base_body() -> dict[str, Any]:
    return {
        "branch_prefix": "integration/batch",
        "protected_branches": ["main"],
        "batch": {
            "state_file": "var/integration/current-batch.json",
            "worktree_root": "var/integration/worktrees",
        },
        "roles": {
            "coder": {
                "harness": "cli-grok",
                "model": "grok-4.5",
                "effort": "high",
                "can_merge_to_main": False,
            },
            "coder_mechanical": {
                "harness": "cli-gemini",
                "model": "gemini-3.6-flash",
                "effort": None,
                "can_merge_to_main": False,
            },
            "lane_reviewer": {
                "harness": "cli-codex",
                "model": "gpt-5.6-sol",
                "effort": None,
                "can_merge_to_main": False,
            },
            "integrator": {
                "harness": "cli-codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "can_merge_to_main": False,
            },
            "lane_verifier": {
                "harness": "cli-claude",
                "model": "claude-opus-5",
                "effort": "high",
                "can_merge_to_main": False,
            },
            "aggregate_reviewer": {
                "harness": "cli-claude",
                "model": "claude-opus-5",
                "effort": "high",
                "can_merge_to_main": False,
            },
        },
        "reviewer_lineage_required": "anthropic",
        "verdicts": {"prose_fallback": True},
        "promotion": {
            "mode": "report",
            "pause_s": 1800,
            "gate_targets": ["tests/doctrine", "tests/gates_scripts"],
        },
    }


def test_shipped_config_loads() -> None:
    cfg = load_integration_config(DEFAULT_CONFIG_PATH)
    assert cfg.branch_prefix == "integration/batch"
    assert cfg.reviewer_lineage_required == "anthropic"
    assert cfg.prose_fallback is True
    assert cfg.promotion_mode == "report"
    assert all(not stage.can_merge_to_main for stage in cfg.stages.values())


def test_can_merge_to_main_true_refused(tmp_path: Path) -> None:
    body = _base_body()
    body["roles"]["coder"]["can_merge_to_main"] = True
    path = _write_config(tmp_path / "integration.yaml", body)
    with pytest.raises(ValueError, match="can_merge_to_main"):
        load_integration_config(path)


def test_reviewer_lineage_enforced(tmp_path: Path) -> None:
    """aggregate_reviewer must resolve to reviewer_lineage_required."""
    body = _base_body()
    # openai lineage, but required is anthropic
    body["roles"]["aggregate_reviewer"] = {
        "harness": "cli-codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "can_merge_to_main": False,
    }
    path = _write_config(tmp_path / "integration.yaml", body)
    with pytest.raises(ValueError, match="reviewer_lineage_required"):
        load_integration_config(path)


def test_same_lineage_reviewer_and_coder_refused(tmp_path: Path) -> None:
    """aggregate_reviewer lineage must differ from coder (and lane_reviewer)."""
    body = _base_body()
    # Force anthropic coder + anthropic aggregate → same lineage refusal.
    body["roles"]["coder"] = {
        "harness": "cli-claude",
        "model": "claude-opus-5",
        "effort": "high",
        "can_merge_to_main": False,
    }
    path = _write_config(tmp_path / "integration.yaml", body)
    with pytest.raises(ValueError, match="must differ from coder"):
        load_integration_config(path)


def test_effort_vocabulary_mirrors_improvement_chain(tmp_path: Path) -> None:
    """Same effort set as improvement_chain: {None, low, medium, high, xhigh, max}."""
    for effort in (None, "low", "medium", "high", "xhigh", "max"):
        body = _base_body()
        body["roles"]["coder"]["effort"] = effort
        path = _write_config(tmp_path / f"ok-{effort}.yaml", body)
        cfg = load_integration_config(path)
        assert cfg.stages["coder"].effort == effort

    body = _base_body()
    body["roles"]["coder"]["effort"] = "ultra"
    path = _write_config(tmp_path / "bad-effort.yaml", body)
    with pytest.raises(ValueError, match="effort"):
        load_integration_config(path)


def test_cli_get_prints_value() -> None:
    """``python -m omniagentos.integration.config get <path>`` prints the raw value."""
    py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    proc = subprocess.run(
        [
            str(py),
            "-m",
            "omniagentos.integration.config",
            "get",
            "branch_prefix",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "integration/batch"

    proc2 = subprocess.run(
        [
            str(py),
            "-m",
            "omniagentos.integration.config",
            "get",
            "roles.coder.model",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr
    assert proc2.stdout.strip() == "grok-4.5"
