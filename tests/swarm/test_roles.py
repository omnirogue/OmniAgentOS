from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import provider_hints_for_model_role
from omniagentos.graph_runtime.contracts import BUILTIN_TEMPLATES
from omniagentos.roles import JobRole, job_role_from_swarm_json
from tests.swarm.scheduler_fakes import make_harness, make_scheduler


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch in these tests must go through the harness fixtures."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


# ---------------------------------------------------------------------------
# Test 1: End-to-end JobRole Agreement between Planner and Scheduler
# ---------------------------------------------------------------------------
def test_end_to_end_role_agreement(tmp_path: Path) -> None:
    """Run a small plan through make_harness + make_scheduler.
    Assert that for every task, the planner-stamped 'job_role' in the database
    matches the scheduler-emitted 'role' in ACTION_WORKER_SPAWNED."""
    h = make_harness(
        tmp_path,
        [
            {"id": "a"},
        ],
        max_concurrency=2,
    )
    scheduler = make_scheduler(h)
    handle = scheduler.start_run(h.run_id)
    assert handle is not None
    assert handle.join(timeout=20)

    task_keys = ["a", "integration"]
    spawn_events = h.emitter.of("worker_spawned")
    spawn_map = {event["task_id"]: event["role"] for event in spawn_events}

    for key in task_keys:
        # Planner side check (read back from DB board_tasks table)
        swarm_json = h.swarm_json_of(key)
        assert swarm_json is not None
        planner_role = swarm_json.get("job_role")
        assert planner_role is not None

        # Scheduler side check
        task_card_id = h.task_id(key)
        assert task_card_id in spawn_map
        scheduler_role = spawn_map[task_card_id]

        # Agreement
        assert planner_role == scheduler_role

        # Specific expected values
        if key == "integration":
            assert planner_role == "integrator"
            assert scheduler_role == "integrator"
        elif key == "a":
            assert planner_role == "implementer"
            assert scheduler_role == "implementer"


# ---------------------------------------------------------------------------
# Test 2: Decision table for job_role_from_swarm_json
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "swarm_json,expected_role",
    [
        # integration wins even when complexity suggests review
        ({"integration": True, "complexity": "review"}, JobRole.INTEGRATOR),
        ({"integration": True, "complexity": "verify"}, JobRole.INTEGRATOR),
        ({"integration": True, "complexity": "REVIEW"}, JobRole.INTEGRATOR),
        ({"integration": True}, JobRole.INTEGRATOR),
        # complexity review/verify cases
        ({"complexity": "review"}, JobRole.REVIEWER),
        ({"complexity": "verify"}, JobRole.REVIEWER),
        ({"complexity": "REVIEW"}, JobRole.REVIEWER),
        ({"complexity": "VERIFY"}, JobRole.REVIEWER),
        ({"complexity": "verify_command"}, JobRole.IMPLEMENTER),  # verify must match exactly
        # defaults
        ({"complexity": "high"}, JobRole.IMPLEMENTER),
        ({"complexity": ""}, JobRole.IMPLEMENTER),
        ({}, JobRole.IMPLEMENTER),
        (None, JobRole.IMPLEMENTER),
    ],
)
def test_job_role_decision_table(swarm_json: dict[str, Any] | None, expected_role: JobRole) -> None:
    assert job_role_from_swarm_json(swarm_json) == expected_role


def test_job_role_decision_table_non_mapping() -> None:
    # test non-mapping
    assert job_role_from_swarm_json("not-a-mapping") == JobRole.IMPLEMENTER  # type: ignore


# ---------------------------------------------------------------------------
# Test 3: JobRole enum matches the Markdown files on disk
# ---------------------------------------------------------------------------
def test_vocabulary_matches_prompts_on_disk() -> None:
    """Assert JobRole enum values match the .md file stems in vault/prompts/roles/."""
    test_file_path = Path(__file__).resolve()
    repo_root = test_file_path.parents[2]
    prompts_dir = repo_root / "vault" / "prompts" / "roles"

    assert prompts_dir.is_dir(), f"Prompts directory not found at {prompts_dir}"

    prompt_stems = {p.stem for p in prompts_dir.glob("*.md")}
    enum_values = {r.value for r in JobRole}

    assert enum_values == prompt_stems


# ---------------------------------------------------------------------------
# Test 4: Wired model_role hints are load-bearing
# ---------------------------------------------------------------------------
def test_wired_hints_are_load_bearing() -> None:
    """Import built-in graph_runtime templates and ensure model_roles retrieve non-empty provider hints.
    Also check unknown role behavior and verify the returned list is a safe copy."""
    # Every node with a model_role must have a non-empty provider hint
    found_any_role = False
    for template in BUILTIN_TEMPLATES:
        for node in template.nodes:
            if node.model_role is not None:
                found_any_role = True
                hints = provider_hints_for_model_role(node.model_role)
                assert isinstance(hints, list)
                assert len(hints) > 0, f"No hints found for live model_role: {node.model_role}"

    assert found_any_role, "Expected to find at least one template node with a model_role"

    # Accessor returns [] for unknown / None roles
    assert provider_hints_for_model_role("nonexistent_role_xyz") == []
    assert provider_hints_for_model_role(None) == []

    # Accessor returns a copy (not a shared reference)
    role = "verifier"
    hints_1 = provider_hints_for_model_role(role)
    hints_2 = provider_hints_for_model_role(role)
    assert hints_1 == hints_2
    assert hints_1 is not hints_2

    # Mutating returned copy does not affect internal state
    original_len = len(hints_1)
    hints_1.append("evil_provider")
    hints_3 = provider_hints_for_model_role(role)
    assert "evil_provider" not in hints_3
    assert len(hints_3) == original_len
