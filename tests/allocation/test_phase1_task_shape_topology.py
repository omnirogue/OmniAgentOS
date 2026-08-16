import json
from pathlib import Path

from pytest import MonkeyPatch

from omniagentos.allocation.characterize import characterize
from omniagentos.allocation.fanout import TOPOLOGY_CAPS, decide_fanout
from omniagentos.formation.selector import topology_for_formation
from omniagentos.swarm.planner import (
    TARGET_N_MAX,
    _check_disjoint_owned_paths,
    _compute_disjoint_dag_width,
    build_plan,
)


def test_flags_default_off_baseline_unchanged(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", raising=False)

    # Unset defaults preserve the baseline creative topology.
    assert topology_for_formation("creative") == "generator_critic"

    raw_tasks = [
        {
            "id": "t1",
            "title": "Research 1",
            "depends_on": [],
            "owned_paths": ["src/r1.py"],
        }
    ]
    plan_unset = build_plan("research competitive pricing", raw_tasks)

    # Explicit off and default/unset are byte-equivalent planning behavior.
    monkeypatch.setenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "off")
    plan_off = build_plan("research competitive pricing", raw_tasks)
    assert plan_unset.model_dump(mode="json") == plan_off.model_dump(mode="json")
    assert plan_unset.target_n == 1


def test_parallel_sections_sizing(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "enforce")

    # Topology is parallel_sections under enforce mode
    assert topology_for_formation("creative") == "parallel_sections"

    # Floor sizing: dag_width = 1 is raised to floor 2
    char = characterize({"I": 0.5, "uncertainty": 0.5})
    decision_floor = decide_fanout(
        char,
        free_slots=8,
        writer_slots=8,
        verifier_capacity=2,
        independent_units=1,
        preferred_topology="parallel_sections",
        disjoint_dag_width=1,
    )
    assert decision_floor.topology == "parallel_sections"
    assert decision_floor.worker_count == 2  # floor 2
    assert decision_floor.verifier_count == 1  # exactly one critic

    # Observe the independent-unit capacity before the parallel-sections floor
    # replaces worker_count. A width inflation can leave the downstream worker
    # count unchanged while corrupting the capacity recorded by allocation.
    decision_units = decide_fanout(
        char,
        free_slots=8,
        writer_slots=8,
        verifier_capacity=2,
        independent_units=1,
    )
    assert decision_units.hard_capacity == 1

    # Cap sizing: dag_width = 10 is clamped to cap 8
    decision_cap = decide_fanout(
        char,
        free_slots=10,
        writer_slots=10,
        verifier_capacity=2,
        independent_units=10,
        preferred_topology="parallel_sections",
        disjoint_dag_width=10,
    )
    assert decision_cap.topology == "parallel_sections"
    assert decision_cap.worker_count == 8  # cap 8
    assert decision_cap.verifier_count == 1  # exactly one critic

    # Middle sizing: dag_width = 5
    decision_mid = decide_fanout(
        char,
        free_slots=8,
        writer_slots=8,
        verifier_capacity=2,
        independent_units=5,
        preferred_topology="parallel_sections",
        disjoint_dag_width=5,
    )
    assert decision_mid.topology == "parallel_sections"
    assert decision_mid.worker_count == 5
    assert decision_mid.verifier_count == 1  # exactly one critic


def _research_tasks(count: int) -> list[dict]:
    return [
        {
            "id": f"t{i}",
            "title": f"Research {i}",
            "depends_on": [],
            "owned_paths": [f"src/r{i}.py"],
        }
        for i in range(1, count + 1)
    ]


def test_research_widening_applies_only_to_open_ended_parallel_plans(
    monkeypatch: MonkeyPatch,
) -> None:
    """Enforce widens open-ended research toward ~5, clamped by capacity —
    and never below what the plan already earned."""
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "enforce")

    plan = build_plan(
        "research the competitive landscape and gather evidence", _research_tasks(5)
    )
    assert 2 <= plan.target_n <= 5
    # Note emission is pinned by test_research_widen_never_shrinks_and_notes_only_on_change.


def test_solo_research_plan_never_widens(monkeypatch: MonkeyPatch) -> None:
    """A solo/well-specified research plan must not be un-soloed by enforce
    (the widening default is for open-ended shapes only)."""
    goal = "research the competitive landscape and gather evidence"
    monkeypatch.delenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", raising=False)
    baseline = build_plan(goal, _research_tasks(2))

    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "enforce")
    enforced = build_plan(goal, _research_tasks(2))

    assert enforced.target_n == baseline.target_n
    assert not any(
        "research task shape targeted ~5 workers" in note for note in enforced.assumptions
    )


def test_topology_for_formation_normalizes_invalid_gate_values(
    monkeypatch: MonkeyPatch,
) -> None:
    from omniagentos.formation.selector import topology_for_formation

    monkeypatch.setenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "banana")
    assert topology_for_formation("creative") != "parallel_sections"
    monkeypatch.setenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "enforce")
    assert topology_for_formation("creative") == "parallel_sections"


def test_coding_task_uses_plan_then_implement_no_sample_and_vote(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "enforce")

    raw_tasks = [
        {
            "id": "t1",
            "title": "Fix Auth bug",
            "depends_on": [],
            "owned_paths": ["src/auth.py"],
        }
    ]
    # Coding goal maps to coding category
    plan = build_plan("implement and fix the auth bug in backend", raw_tasks)
    # Check that plan-then-implement note is present and sample-and-vote is never present
    assert any("coding task shape set to plan-then-implement" in note for note in plan.assumptions)
    assert not any("sample-and-vote" in note for note in plan.assumptions)


def test_no_increase_to_global_default_agent_counts() -> None:
    assert TARGET_N_MAX == 5
    assert TOPOLOGY_CAPS["sequential"] == 1
    assert TOPOLOGY_CAPS["generator_critic"] == 2
    assert TOPOLOGY_CAPS["specialist_panel"] == 3
    assert TOPOLOGY_CAPS["parallel_sections"] == 8


def test_shadow_row_emission(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE", "shadow")
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "shadow")

    # Define tasks with disjoint-DAG width > 2 and disjoint owned paths
    raw_tasks = [
        {
            "id": "t1",
            "title": "Design banner",
            "depends_on": [],
            "owned_paths": ["assets/banner.png"],
        },
        {
            "id": "t2",
            "title": "Create logo",
            "depends_on": [],
            "owned_paths": ["assets/logo.png"],
        },
        {
            "id": "t3",
            "title": "Mock illustration",
            "depends_on": [],
            "owned_paths": ["assets/illust.png"],
        },
    ]

    shadow_file = Path("var/swarm/shadow_topology.jsonl")

    # Build plan for a creative task
    build_plan("design creative hero concept images and branding", raw_tasks)

    # Verify that shadow file was written and is not empty
    assert shadow_file.exists()
    with open(shadow_file, encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) >= 1
    log_row = json.loads(lines[-1])

    assert log_row["creative_mode"] == "shadow"
    assert log_row["fanout_mode"] == "shadow"
    assert log_row["disjoint_dag_width"] == 3
    assert log_row["disjoint_owned_paths"] is True
    assert log_row["challenger_creative_topology"] == "parallel_sections"
    assert log_row["challenger_creative_generator_count"] == 3
    assert log_row["challenger_creative_critic_count"] == 1


def test_helper_disjoint_dag_width() -> None:
    # 3 completely independent tasks -> width should be 3
    tasks_independent = [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": []},
        {"id": "c", "depends_on": []},
    ]
    assert _compute_disjoint_dag_width(tasks_independent) == 3

    # 3 tasks with linear dependency chain (a -> b -> c) -> width should be 1
    tasks_chain = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["c"]},
        {"id": "c", "depends_on": []},
    ]
    assert _compute_disjoint_dag_width(tasks_chain) == 1

    # Two disjoint components: component 1 (a -> b), component 2 (c) -> width should be 2
    tasks_disjoint = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": []},
        {"id": "c", "depends_on": []},
    ]
    assert _compute_disjoint_dag_width(tasks_disjoint) == 2


def test_helper_disjoint_owned_paths() -> None:
    # Disjoint paths
    tasks_disjoint = [
        {"id": "a", "owned_paths": ["src/a.py"]},
        {"id": "b", "owned_paths": ["src/b.py"]},
    ]
    assert _check_disjoint_owned_paths(tasks_disjoint) is True

    # Overlapping paths
    tasks_overlapping = [
        {"id": "a", "owned_paths": ["src/a.py", "src/shared.py"]},
        {"id": "b", "owned_paths": ["src/b.py", "src/shared.py"]},
    ]
    assert _check_disjoint_owned_paths(tasks_overlapping) is False


def test_research_widen_defers_to_router_enforce(monkeypatch: MonkeyPatch) -> None:
    """When the task-shape router is itself in enforce, its clamp is
    authoritative — the research widen must not re-widen past it."""
    goal = "research the competitive landscape and gather evidence"
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "enforce")
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", "enforce")
    plan = build_plan(goal, _research_tasks(5))
    assert not any(
        "research task shape targeted ~5 workers" in note for note in plan.assumptions
    )
    assert any("deferred to task-shape router enforce" in note for note in plan.assumptions)


def test_research_widen_never_shrinks_and_notes_only_on_change(
    monkeypatch: MonkeyPatch,
) -> None:
    goal = "research the competitive landscape and gather evidence"
    monkeypatch.delenv("OMNIAGENTOS_TASK_SHAPE_ROUTER", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", raising=False)
    baseline = build_plan(goal, _research_tasks(5))
    monkeypatch.setenv("OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE", "enforce")
    enforced = build_plan(goal, _research_tasks(5))
    assert enforced.target_n >= baseline.target_n
    widened = enforced.target_n != baseline.target_n
    note_present = any(
        "research task shape targeted ~5 workers" in note for note in enforced.assumptions
    )
    assert note_present == widened
