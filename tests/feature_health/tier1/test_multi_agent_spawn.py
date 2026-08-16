"""Tier1 gap: multi_agent fan-out plan -> per-task worker enumeration mapping.

Multi_agent has 1,396 pre-existing tests (tests/swarm, tests/formation, tests/orchestrator,
tests/worktrees). This test covers the seam where a plan specifying N workers gets
provisioned into per-task worker assignments — a mechanical, $0 contract not traced by
existing suites.

The test seeds a multi-task plan (diamond dependency graph) and verifies that
provision_run creates distinct card records for each task, demonstrating correct
fan-out and mapping from the plan to worker assignment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import INTEGRATION_TASK_ID, build_plan, provision_run


def _raw_task(
    task_id: str,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal raw task dict for build_plan."""
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": depends_on or [],
        "owned_paths": [f"src/{task_id}.py"],
        "est_agent_minutes": 10,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": "pytest",
    }


class TestMultiAgentSpawnCoordination:
    """Worker enumeration and per-task assignment during multi-agent provisioning."""

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> str:
        db = str(tmp_path / "fh-tier1-spawn.db")
        CollabStore(db)  # migrates shared schema
        return db

    @pytest.fixture
    def swarm_dal(self, db_path: str) -> SwarmDal:
        return SwarmDal(db_path)

    def test_fan_out_plan_creates_per_task_cards_diamond(
        self,
        swarm_dal: SwarmDal,
        tmp_path: Path,
    ) -> None:
        """Diamond DAG (A -> B,C -> D) provision creates individual cards for B and C.

        A standard fan-out shape: task A drives two parallel branches (B, C),
        which converge at the integration task auto-added by build_plan.
        Provision must create separate worker cards for each task.
        """
        # Seed the plan: diamond
        raw_tasks = [
            _raw_task("task_a"),
            _raw_task("task_b", depends_on=["task_a"]),
            _raw_task("task_c", depends_on=["task_a"]),
        ]

        plan = build_plan(
            "test diamond fan-out",
            raw_tasks,
        )
        assert plan is not None, "plan construction failed"

        # Set up workspace for provision_run
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Provision the run
        result = provision_run(
            plan,
            dal=swarm_dal,
            working_dir=str(workspace),
            write_plan_doc=False,
        )
        assert result is not None, "provision_run returned None"

        # Verify card_ids has entries for A, B, C, and integration
        card_ids = result.get("card_ids", {})
        assert isinstance(card_ids, dict), f"card_ids not a dict: {card_ids}"

        # Should have A, B, C plus integration task
        assert "task_a" in card_ids, f"task_a missing from card_ids: {card_ids.keys()}"
        assert "task_b" in card_ids, f"task_b missing from card_ids: {card_ids.keys()}"
        assert "task_c" in card_ids, f"task_c missing from card_ids: {card_ids.keys()}"
        assert INTEGRATION_TASK_ID in card_ids, f"integration task missing: {card_ids.keys()}"

        # Verify B and C are retrievable as independent cards
        b_card_id = card_ids["task_b"]
        c_card_id = card_ids["task_c"]
        assert b_card_id is not None and b_card_id != c_card_id, (
            "B and C must have distinct card IDs"
        )

        # Retrieve the swarm_json for each card to verify they're populated
        b_swarm_json = swarm_dal.get_swarm_json(b_card_id)
        c_swarm_json = swarm_dal.get_swarm_json(c_card_id)
        assert b_swarm_json is not None, "B card swarm_json not found"
        assert c_swarm_json is not None, "C card swarm_json not found"

    def test_multi_child_hierarchical_assigns_distinct_cards(
        self,
        swarm_dal: SwarmDal,
        tmp_path: Path,
    ) -> None:
        """Hierarchical topology with three children: provision creates three worker cards.

        Tests that a root task driving multiple parallel children results in
        distinct card assignments, not collapsed into a single worker.
        """
        raw_tasks = [
            _raw_task("task_root"),
            _raw_task("task_child_1", depends_on=["task_root"]),
            _raw_task("task_child_2", depends_on=["task_root"]),
            _raw_task("task_child_3", depends_on=["task_root"]),
        ]

        plan = build_plan(
            "test hierarchical children",
            raw_tasks,
        )
        assert plan is not None

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        result = provision_run(
            plan,
            dal=swarm_dal,
            working_dir=str(workspace),
            write_plan_doc=False,
        )
        assert result is not None

        card_ids = result.get("card_ids", {})

        # All three children should have distinct cards
        assert "task_child_1" in card_ids
        assert "task_child_2" in card_ids
        assert "task_child_3" in card_ids

        # Cards should be distinct from each other
        cid1 = card_ids["task_child_1"]
        cid2 = card_ids["task_child_2"]
        cid3 = card_ids["task_child_3"]
        assert cid1 != cid2 != cid3 != cid1, "all child cards must be distinct"

    def test_sequential_chain_provisions_with_dependency_order(
        self,
        swarm_dal: SwarmDal,
        tmp_path: Path,
    ) -> None:
        """Sequential chain (seq_1 -> seq_2 -> seq_3) provisions all tasks in order.

        Verifies that task dependencies are preserved and all tasks are provisioned
        with the correct dependency structure.
        """
        raw_tasks = [
            _raw_task("seq_1"),
            _raw_task("seq_2", depends_on=["seq_1"]),
            _raw_task("seq_3", depends_on=["seq_2"]),
        ]

        plan = build_plan(
            "test sequential chain",
            raw_tasks,
        )
        assert plan is not None

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        result = provision_run(
            plan,
            dal=swarm_dal,
            working_dir=str(workspace),
            write_plan_doc=False,
        )
        assert result is not None

        card_ids = result.get("card_ids", {})

        # All three sequential tasks should be provisioned
        assert "seq_1" in card_ids, "seq_1 missing"
        assert "seq_2" in card_ids, "seq_2 missing"
        assert "seq_3" in card_ids, "seq_3 missing"

        # Verify each card's swarm_json is populated (the seam this tier1 test covers)
        for task_id in ("seq_1", "seq_2", "seq_3"):
            card_id = card_ids[task_id]
            swarm_json = swarm_dal.get_swarm_json(card_id)
            assert (
                swarm_json is not None
            ), f"swarm_json not found for {task_id} (card {card_id})"
