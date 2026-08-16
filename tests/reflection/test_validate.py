"""Unit tests for the validation gates (omniagentos/reflection/validate.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omniagentos.reflection.validate import (
    is_hard_stop,
    traverse_and_set,
    validate_batch,
    validate_proposal,
    validate_router_weight_shadow,
    validate_yaml_round_trip,
)


def test_is_hard_stop() -> None:
    # 1. Paths with external, deploy, or destructive
    assert is_hard_stop("deploy_scripts/run.sh") is True
    assert is_hard_stop("src/external_service.py") is True
    assert is_hard_stop("scripts/destructive_clean.py") is True

    # 2. configs/governance.yaml
    assert is_hard_stop("configs/governance.yaml") is True
    assert is_hard_stop("CONFIGS/governance.yaml") is True

    # 3. omniagentos/policy/**
    assert is_hard_stop("omniagentos/policy/rules.yaml") is True

    # 4. Budgets and credentials
    assert is_hard_stop("configs/accounts.yaml") is True
    assert is_hard_stop("configs/connectors.yaml", "auth_token") is True
    assert is_hard_stop("configs/cognitive_budget.yaml") is True
    assert is_hard_stop("src/main.py", "api_key") is True

    # 5. Normal files should pass
    assert is_hard_stop("configs/swarm.yaml", "router.lane_floors.weights") is False
    assert is_hard_stop("configs/reflection.yaml") is False


def test_traverse_and_set() -> None:
    data = {"router": {"lane_floors": {"weights": [1.0, 2.0]}}}

    # Traverse nested dict
    traverse_and_set(data, "router.lane_floors.weights.0", 1.5)
    assert data["router"]["lane_floors"]["weights"][0] == 1.5

    # Assign in list
    traverse_and_set(data, "router.lane_floors.weights.1", 2.5)
    assert data["router"]["lane_floors"]["weights"][1] == 2.5

    # New nested key creation
    traverse_and_set(data, "router.lane_floors.new_key", "value")
    assert data["router"]["lane_floors"]["new_key"] == "value"

    # Invalid list index raises ValueError
    with pytest.raises(ValueError):
        traverse_and_set(data, "router.lane_floors.weights.abc", 3.0)


def test_validate_yaml_round_trip(tmp_path: Path) -> None:
    yaml_file = tmp_path / "test_config.yaml"
    initial_data = {"router": {"lane_floors": {"weights": [1.0, 2.0]}}}
    with open(yaml_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(initial_data, f)

    # Success round-trip
    ok, err = validate_yaml_round_trip(str(yaml_file), "router.lane_floors.weights.0", 1.2)
    assert ok is True
    assert err == ""

    # Verify original file untouched (only in-memory test)
    with open(yaml_file, encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert loaded["router"]["lane_floors"]["weights"][0] == 1.0


def test_validate_router_weight_shadow(tmp_path: Path) -> None:
    log_file = tmp_path / "router_shadow.jsonl"

    # Test missing file
    ok, err, wr, decs = validate_router_weight_shadow(str(tmp_path / "nonexistent.jsonl"))
    assert ok is False
    assert "does not exist" in err

    # Create dummy shadow log (12 entries: 7 challenger wins, 5 incumbent wins)
    entries = []
    # 7 Challenger wins
    for _i in range(7):
        entries.append(
            {
                "timestamp": "2026-07-26T12:00:00Z",
                "incumbent_decision": {"pick": "deep-coder"},
                "flash_decision": {"pick": "fast-coder"},
                "challenger_win": True,
            }
        )
    # 5 Incumbent wins
    for _i in range(5):
        entries.append(
            {
                "timestamp": "2026-07-26T12:01:00Z",
                "incumbent_decision": {"pick": "deep-coder"},
                "flash_decision": {"pick": "fast-coder"},
                "challenger_win": False,
            }
        )

    with open(log_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    # Win rate = 7 / 12 = 58.33% (>= 55% threshold)
    ok, err, wr, decs = validate_router_weight_shadow(
        str(log_file), threshold=0.55, min_decisions=10
    )
    assert ok is True
    assert wr == pytest.approx(0.5833, abs=1e-3)
    assert decs == 12

    # High threshold (e.g. 70%) should fail
    ok, err, wr, decs = validate_router_weight_shadow(
        str(log_file), threshold=0.70, min_decisions=10
    )
    assert ok is False
    assert "below threshold" in err

    # Insufficient decisions (min 15) should fail
    ok, err, wr, decs = validate_router_weight_shadow(
        str(log_file), threshold=0.55, min_decisions=15
    )
    assert ok is False
    assert "Not enough shadow decisions" in err


def test_validate_proposal(tmp_path: Path) -> None:
    limits_config = {
        "limits": {
            "max_formation_changes": 1,
            "max_router_weight_changes": 2,
        },
        "validation": {
            "router_win_rate_threshold": 0.55,
            "router_min_decisions": 10,
        },
    }

    # Schema invalid
    proposal_invalid_schema = {"id": "prop_1", "kind": "router_weight"}
    ok, err = validate_proposal(proposal_invalid_schema, limits_config)
    assert ok is False
    assert "Missing required proposal schema key" in err

    # Hard-stop file touch (governance.yaml)
    proposal_hard_stop = {
        "id": "prop_1",
        "kind": "router_weight",
        "target": {"file": "configs/governance.yaml", "key": "some_rule"},
        "current": 1,
        "proposed": 2,
    }
    ok, err = validate_proposal(proposal_hard_stop, limits_config)
    assert ok is False
    assert "terminal refusal" in err


def test_validate_batch() -> None:
    limits_config = {
        "limits": {
            "max_formation_changes": 1,
            "max_router_weight_changes": 2,
        }
    }

    # Two formation changes (exceeds limit of 1)
    proposals = [
        {
            "id": "p_1",
            "kind": "formation",
            "target": {"file": "configs/formations.yaml", "key": "form_1"},
            "current": "a",
            "proposed": "b",
        },
        {
            "id": "p_2",
            "kind": "formation",
            "target": {"file": "configs/formations.yaml", "key": "form_2"},
            "current": "x",
            "proposed": "y",
        },
    ]

    results = validate_batch(proposals, limits_config)
    assert len(results) == 2
    assert results[0][1] is True  # First one is ok
    assert results[1][1] is False  # Second is blocked by budget cap
    assert "Formation change exceeds bounded-change budget limit" in results[1][2]
