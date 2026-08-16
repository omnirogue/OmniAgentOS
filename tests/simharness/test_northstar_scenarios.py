"""North Star deterministic scenario carriers.

These scenarios deliberately use the existing SimulationCampaign and scripted
provider seam: no model, network, or production database is involved.
"""

from __future__ import annotations

from pathlib import Path

from tests.simharness.assertions import (
    assert_attempt_usage_complete,
    assert_concurrency_matches_plan,
    assert_terminal,
)
from tests.simharness.runner import SimulationCampaign, dependency_tasks, standard_tasks
from tests.simharness.stub_provider import full_usage


def _tasks(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"project-{index}",
            "title": f"Project {index}",
            "description": "deterministic portfolio work",
            "depends_on": [],
            "owned_paths": [f"src/project-{index}.py"],
            "complexity": "simple",
            "est_agent_minutes": 10,
            "est_manual_minutes": 20,
            "acceptance": "project complete",
            "verify_command": "",
        }
        for index in range(count)
    ]


def test_side_effect_boundary_and_crash_resume_are_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """A cancelled run closes attempts and never performs network side effects."""
    with SimulationCampaign(tmp_path / "resume-boundary", monkeypatch, scenario="resume-boundary") as sim:
        run_id = sim.dispatch(
            standard_tasks(),
            [full_usage(f"boundary {index}") for index in range(4)],
            barrier_size=1,
            activate=False,
        )
        response = sim.cancel(run_id)
        result = sim.result(run_id)

        assert response["status"] == "cancelled"
        assert result.status == "cancelled"
        assert result.network_attempts == 0
        assert all(row["end_reason"] is not None for row in result.attempts)


def test_dag_width_and_makespan_follow_dependencies(tmp_path: Path, monkeypatch) -> None:
    """Independent leaves may overlap, while the dependent child waits."""
    with SimulationCampaign(tmp_path / "dag-width", monkeypatch, scenario="dag-width", target_cap=2) as sim:
        run_id = sim.dispatch(
            dependency_tasks(),
            [full_usage(f"dag {index}") for index in range(4)],
            barrier_size=2,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)

        assert_terminal(status)
        assert_concurrency_matches_plan(result)
        assert result.max_interval_overlap == 2
        assert {row["task_key"] for row in result.attempts} >= {"parent", "child", "sibling"}


def test_portfolio_scheduler_has_bounded_width_and_equal_progress(
    tmp_path: Path, monkeypatch
) -> None:
    """Ten projects receive one deterministic attempt under a fixed cap."""
    with SimulationCampaign(tmp_path / "portfolio", monkeypatch, scenario="portfolio", target_cap=3) as sim:
        run_id = sim.dispatch(
            _tasks(10),
            [full_usage(f"portfolio {index}") for index in range(10)],
            barrier_size=3,
        )
        status = sim.join(run_id)
        result = sim.result(run_id)

        assert_terminal(status)
        assert_concurrency_matches_plan(result)
        project_attempts = tuple(
            row for row in result.attempts if str(row["task_key"]).startswith("project-")
        )
        assert_attempt_usage_complete(project_attempts)
        assert result.network_attempts == 0
        assert result.scripts_remaining == 0
        counts = {key: 0 for key in (f"project-{index}" for index in range(10))}
        for row in project_attempts:
            counts[str(row["task_key"])] += 1
        assert set(counts.values()) == {1}
