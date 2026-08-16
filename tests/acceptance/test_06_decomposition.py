"""AT3 area 6 — TASK DECOMPOSITION.

Acceptance claims under test:

  1. The project lead decomposes correctly (a plan is produced, capped, and
     structurally valid).
  2. Team leads decompose correctly (dependency edges survive, cycles are
     repaired or refused, ownership overlap is serialized).
  3. Dependencies are correct (topological order, integration last).
  4. Work is parallelized MAXIMALLY with a SEPARATE MERGER — the parallel
     worker tasks own pairwise-DISJOINT paths, and a distinct integration task
     exists that is not one of the workers.

Ground truth:
  * ``omniagentos/swarm/planner.py`` — ``build_plan``, ``topo_sort_with_repair``,
    ``add_ownership_overlap_edges``, ``_compute_disjoint_dag_width``,
    ``_check_disjoint_owned_paths``, ``resolve_owned_path``, the auto-planned
    integration task (planner.py:1070-1094), ``INTEGRATION_TASK_ID``.
  * ``omniagentos/allocation/fanout.py`` — ``decide_fanout``, ``TOPOLOGY_CAPS``.
  * ``omniagentos/allocation/arbiter.py`` — ``decide_route``.

Hermetic: ``build_plan`` is pure with ``workspace_dir=None``; the one test that
exercises path resolution uses ``tmp_path``. No network, no LLM.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from omniagentos.allocation.arbiter import decide_route
from omniagentos.allocation.characterize import TaskCharacterization
from omniagentos.allocation.fanout import TOPOLOGY_CAPS, decide_fanout
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.planner import (
    BOOTSTRAP_TASK_ID,
    INTEGRATION_TASK_ID,
    MAX_TASKS,
    SwarmPlanError,
    _check_disjoint_owned_paths,
    _compute_disjoint_dag_width,
    add_ownership_overlap_edges,
    build_plan,
    paths_overlap,
    resolve_owned_path,
    topo_sort_with_repair,
)

_RESERVED = (INTEGRATION_TASK_ID, BOOTSTRAP_TASK_ID)


@pytest.fixture(autouse=True)
def _planner_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``build_plan`` pure: the shadow-topology flags append JSONL to CWD."""
    for var in (
        "OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE",
        "OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE",
        "OMNIAGENTOS_SWARM_TARGET_CAP",
    ):
        monkeypatch.delenv(var, raising=False)


def _raw(
    task_id: str,
    *,
    deps: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    agent: int = 10,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": list(paths),
        "est_agent_minutes": agent,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }


def _spec(task_id: str, deps: tuple[str, ...] = (), **kw: Any) -> SwarmTaskSpec:
    return SwarmTaskSpec(id=task_id, title=task_id.upper(), depends_on=list(deps), **kw)


def _workers(plan: SwarmPlan) -> list[SwarmTaskSpec]:
    """Every task the swarm actually fans out to (auto tasks excluded)."""
    return [t for t in plan.tasks if t.id not in _RESERVED]


def _fanout_plan() -> SwarmPlan:
    """A four-way independent decomposition with disjoint ownership."""
    return build_plan(
        "ship the widget",
        [_raw(t, paths=(f"src/{t}.py",)) for t in ("alpha", "beta", "gamma", "delta")],
        suite_command="pytest -q",
    )


# ---------------------------------------------------------------------------
# 1. The project lead decomposes correctly
# ---------------------------------------------------------------------------


class TestProjectLeadDecomposition:
    def test_independent_work_becomes_a_swarm_not_a_solo_run(self) -> None:
        plan = _fanout_plan()
        assert plan.mode == "swarm"
        assert plan.target_n >= 2
        assert {t.id for t in _workers(plan)} == {"alpha", "beta", "gamma", "delta"}

    def test_a_sequential_chain_is_refused_fan_out_and_stays_solo(self) -> None:
        """Decomposing a strict chain into parallel workers would be WRONG."""
        plan = build_plan(
            "chained work",
            [
                _raw("a", paths=("src/a.py",)),
                _raw("b", deps=("a",), paths=("src/b.py",)),
                _raw("c", deps=("b",), paths=("src/c.py",)),
            ],
            suite_command="pytest -q",
        )
        assert plan.mode == "solo"
        assert plan.target_n == 1
        assert plan.integration_task_id is None

    def test_planner_refuses_a_plan_that_exceeds_the_task_cap(self) -> None:
        with pytest.raises(SwarmPlanError, match="task cap"):
            build_plan("too big", [_raw(f"t{i}") for i in range(MAX_TASKS + 1)])

    def test_planner_refuses_reserved_and_duplicate_task_ids(self) -> None:
        with pytest.raises(SwarmPlanError, match="reserved"):
            build_plan("goal", [_raw(INTEGRATION_TASK_ID), _raw("b"), _raw("c")])
        with pytest.raises(SwarmPlanError, match="reserved"):
            build_plan("goal", [_raw(BOOTSTRAP_TASK_ID), _raw("b"), _raw("c")])
        with pytest.raises(SwarmPlanError, match="duplicate"):
            build_plan("goal", [_raw("a"), _raw("a"), _raw("c")])

    def test_owned_paths_may_not_escape_the_workspace(self) -> None:
        with pytest.raises(SwarmPlanError, match="escapes"):
            build_plan("goal", [_raw("a", paths=("../outside",)), _raw("b"), _raw("c")])

    def test_every_worker_task_carries_a_verify_command(self) -> None:
        """A subtask with no gate cannot be graded — see area 8."""
        plan = _fanout_plan()
        for task in plan.tasks:
            assert task.verify_command, f"task {task.id} has no verify_command"


# ---------------------------------------------------------------------------
# 2 + 3. Dependencies are correct
# ---------------------------------------------------------------------------


class TestDependenciesAreCorrect:
    def test_topological_order_puts_every_prerequisite_first(self) -> None:
        specs = [_spec("c", deps=("b",)), _spec("b", deps=("a",)), _spec("a")]
        order, removed = topo_sort_with_repair(specs)
        assert order == ["a", "b", "c"]
        assert removed == []

    def test_declared_dependency_edges_survive_planning(self) -> None:
        plan = build_plan(
            "goal",
            [
                _raw("a", paths=("src/a.py",)),
                _raw("b", deps=("a",), paths=("src/b.py",)),
                _raw("c", paths=("src/c.py",)),
                _raw("d", paths=("src/d.py",)),
            ],
            suite_command="pytest -q",
        )
        task_b = next(t for t in plan.tasks if t.id == "b")
        assert "a" in task_b.depends_on

    def test_a_two_cycle_is_repaired_and_the_repair_is_recorded(self) -> None:
        plan = build_plan(
            "goal",
            [
                _raw("a", deps=("b",), paths=("src/a.py",)),
                _raw("b", deps=("a",), paths=("src/b.py",)),
                _raw("c", paths=("src/c.py",)),
            ],
        )
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert "b" not in task_a.depends_on
        assert any("cycle repaired" in note for note in plan.assumptions)

    def test_an_unrepairable_cycle_is_refused_not_silently_reordered(self) -> None:
        specs = [_spec("a", deps=("c",)), _spec("b", deps=("c",)), _spec("c", deps=("a", "b"))]
        with pytest.raises(SwarmPlanError, match="cycle"):
            topo_sort_with_repair(specs)

    def test_unknown_and_self_dependencies_are_dropped_with_a_note(self) -> None:
        plan = build_plan("goal", [_raw("a", deps=("a", "ghost")), _raw("b"), _raw("c")])
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert task_a.depends_on == []
        assert any("ghost" in note for note in plan.assumptions)

    def test_the_plan_dag_is_acyclic_and_every_edge_resolves(self) -> None:
        plan = _fanout_plan()
        ids = {t.id for t in plan.tasks}
        for task in plan.tasks:
            for dep in task.depends_on:
                assert dep in ids, f"{task.id} depends on unknown task {dep!r}"
        order, removed = topo_sort_with_repair(plan.tasks)
        assert removed == [], "a shipped plan must already be acyclic"
        assert len(order) == len(plan.tasks)


# ---------------------------------------------------------------------------
# 4a. Work is parallelized maximally — DISJOINT ownership across parallel tasks
# ---------------------------------------------------------------------------


class TestParallelWorkIsDisjoint:
    def test_parallel_worker_owned_paths_are_pairwise_disjoint(self) -> None:
        """THE decomposition invariant: two concurrent writers never share a path.

        Checked with ``paths_overlap`` (equal-or-nested), which is strictly
        stronger than the string-equality check the planner's own telemetry
        helper uses.
        """
        plan = _fanout_plan()
        workers = _workers(plan)
        assert len(workers) >= 2

        for left, right in combinations(workers, 2):
            # Only tasks that can run CONCURRENTLY need disjoint ownership.
            if right.id in left.depends_on or left.id in right.depends_on:
                continue
            for path_l in left.owned_paths:
                for path_r in right.owned_paths:
                    assert not paths_overlap(path_l, path_r), (
                        f"concurrent tasks {left.id}/{right.id} both own "
                        f"{path_l!r} and {path_r!r}"
                    )

    def test_overlapping_ownership_is_serialized_rather_than_run_concurrently(self) -> None:
        """When two tasks DO overlap, the planner must add an ordering edge."""
        plan = build_plan(
            "goal",
            [
                _raw("a", paths=("src/a.py",)),
                _raw("b", paths=("src",)),  # the directory containing src/a.py
                _raw("c", paths=("lib",)),
            ],
        )
        task_a = next(t for t in plan.tasks if t.id == "a")
        task_b = next(t for t in plan.tasks if t.id == "b")
        assert "a" in task_b.depends_on, "overlapping owners must be serialized"
        assert "b" not in task_a.depends_on, "the edge must never point backwards"
        assert any("ownership overlap" in note for note in plan.assumptions)

    def test_ownership_edges_are_not_added_for_already_ordered_pairs(self) -> None:
        specs = [
            _spec("a", owned_paths=["src/x.py"]),
            _spec("b", deps=("a",), owned_paths=["src/x.py"]),
        ]
        assert add_ownership_overlap_edges(specs) == []
        assert specs[1].depends_on.count("a") == 1

    def test_shared_manifest_files_are_taken_away_from_workers(self) -> None:
        """``package.json``-class files cannot be owned by a parallel worker."""
        plan = build_plan(
            "goal",
            [
                _raw("a", paths=("src/a.py", "package.json")),
                _raw("b", paths=("src/b.py",)),
                _raw("c", paths=("src/c.py",)),
            ],
        )
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert task_a.owned_paths == ["src/a.py"]
        assert any("package.json" in n and "integration" in n for n in plan.assumptions)

    def test_disjointness_helper_detects_a_shared_path(self) -> None:
        assert _check_disjoint_owned_paths(
            [{"id": "a", "owned_paths": ["src/a.py"]}, {"id": "b", "owned_paths": ["src/b.py"]}]
        )
        assert not _check_disjoint_owned_paths(
            [
                {"id": "a", "owned_paths": ["src/a.py", "src/shared.py"]},
                {"id": "b", "owned_paths": ["src/b.py", "src/shared.py"]},
            ]
        )

    def test_disjointness_helper_ignores_the_integration_task(self) -> None:
        """``integration`` owns ``.``; counting it would report every plan unsafe."""
        plan = _fanout_plan()
        assert _check_disjoint_owned_paths(plan.tasks) is True

    def test_ambiguous_owned_path_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        (tmp_path / "pkg_a" / "widgets").mkdir(parents=True)
        (tmp_path / "pkg_b" / "widgets").mkdir(parents=True)
        with pytest.raises(SwarmPlanError, match="ambiguous"):
            resolve_owned_path("widgets", tmp_path)

    def test_unambiguous_owned_path_is_auto_qualified(self, tmp_path: Path) -> None:
        (tmp_path / "pkg_a" / "widgets").mkdir(parents=True)
        resolved, note = resolve_owned_path("widgets", tmp_path)
        assert resolved == "pkg_a/widgets"
        assert note is not None and "auto-qualified" in note


# ---------------------------------------------------------------------------
# 4b. Maximal width: the DAG width drives fan-out
# ---------------------------------------------------------------------------


class TestMaximalParallelism:
    @pytest.mark.parametrize(
        ("tasks", "expected_width"),
        [
            ([{"id": "a"}, {"id": "b"}, {"id": "c"}], 3),
            ([{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "depends_on": ["b"]}], 1),
            ([{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c"}], 2),
        ],
    )
    def test_dag_width_is_the_maximum_safe_concurrency(
        self, tasks: list[dict[str, Any]], expected_width: int
    ) -> None:
        assert _compute_disjoint_dag_width(tasks) == expected_width

    def test_dag_width_excludes_the_auto_planned_tasks(self) -> None:
        """``integration`` depends on every leaf; counting it would collapse width."""
        plan = _fanout_plan()
        assert _compute_disjoint_dag_width(plan.tasks) == len(_workers(plan))

    def test_wider_dag_buys_more_generators_up_to_the_topology_cap(self) -> None:
        char = TaskCharacterization(
            D=0.9, I=0.9, S=0.1, U=0.3, V=0.6, G=0.9, C=0.5, M=0.3, R=0.2,
            K=0.4, W=1.0, P=0.2, confidence=0.9, task_class=["code"],
        )
        def _fanout(width: int) -> int:
            return decide_fanout(
                char, free_slots=16, writer_slots=16, verifier_capacity=2,
                preferred_topology="parallel_sections", disjoint_dag_width=width,
            ).worker_count

        # Width buys width, monotonically, until the topology cap bites.
        assert _fanout(1) == 2  # PARALLEL_SECTIONS_FLOOR: never below 2 generators
        assert _fanout(6) == 6
        assert _fanout(99) == TOPOLOGY_CAPS["parallel_sections"]
        assert _fanout(6) > _fanout(1)

        wide = decide_fanout(
            char, free_slots=16, writer_slots=16, verifier_capacity=2,
            preferred_topology="parallel_sections", disjoint_dag_width=6,
        )
        assert wide.verifier_count == 1, "a separate critic accompanies the generators"

    def test_sequential_dependencies_forbid_multi_writer_fanout(self) -> None:
        char = TaskCharacterization(
            D=0.9, I=0.9, S=0.8, U=0.3, V=0.6, G=0.9, C=0.5, M=0.3, R=0.2,
            K=0.4, W=1.0, P=0.2, confidence=0.9, task_class=["code"],
        )
        decision = decide_fanout(char, free_slots=8, writer_slots=8, verifier_capacity=2)
        assert decision.topology == "sequential"
        assert decision.worker_count <= 1

    def test_no_topology_is_allowed_to_exceed_its_cap(self) -> None:
        char = TaskCharacterization(
            D=0.9, I=0.9, S=0.1, U=0.9, V=0.9, G=0.9, C=0.9, M=0.9, R=0.2,
            K=0.4, W=1.0, P=0.2, confidence=0.9, task_class=["code"],
        )
        for topology, cap in TOPOLOGY_CAPS.items():
            decision = decide_fanout(
                char, free_slots=64, writer_slots=64, verifier_capacity=4,
                preferred_topology=topology, disjoint_dag_width=64,
            )
            assert decision.worker_count <= cap, f"{topology} exceeded its cap {cap}"

    def test_route_worker_count_never_exceeds_the_topology_cap(self) -> None:
        char = TaskCharacterization(
            D=0.9, I=0.9, S=0.1, U=0.9, V=0.9, G=0.9, C=0.9, M=0.9, R=0.2,
            K=0.4, W=1.0, P=0.2, confidence=0.9, task_class=["code"],
        )
        fanout = decide_fanout(char, free_slots=16, writer_slots=16, verifier_capacity=2)
        route = decide_route(char, fanout)
        assert route.route == "parallel_review"
        assert route.worker_count <= TOPOLOGY_CAPS[route.topology]
        assert route.worker_count >= 2


# ---------------------------------------------------------------------------
# 4c. A SEPARATE MERGER
# ---------------------------------------------------------------------------


class TestSeparateMerger:
    def test_a_dedicated_integration_task_exists_and_is_not_a_worker(self) -> None:
        plan = _fanout_plan()
        assert plan.integration_task_id == INTEGRATION_TASK_ID
        integration = plan.tasks[-1]
        assert integration.id == INTEGRATION_TASK_ID
        assert integration.id not in {t.id for t in _workers(plan)}
        assert len(_workers(plan)) >= 2, "a merger only means something above one worker"

    def test_the_merger_depends_on_every_leaf_so_it_runs_last(self) -> None:
        plan = build_plan(
            "goal",
            [
                _raw("a", paths=("src/a.py",)),
                _raw("b", deps=("a",), paths=("src/b.py",)),
                _raw("c", paths=("src/c.py",)),
                _raw("d", paths=("src/d.py",)),
            ],
            suite_command="pytest -q",
        )
        integration = plan.tasks[-1]
        depended_on = {dep for t in _workers(plan) for dep in t.depends_on}
        leaves = {t.id for t in _workers(plan) if t.id not in depended_on}
        assert set(integration.depends_on) == leaves
        assert "a" not in integration.depends_on  # 'a' is not a leaf; 'b' covers it

        order, _ = topo_sort_with_repair(plan.tasks)
        assert order[-1] == INTEGRATION_TASK_ID, "the merger must be scheduled last"

    def test_the_merger_owns_the_whole_workspace_and_runs_the_full_suite(self) -> None:
        plan = build_plan(
            "goal",
            [_raw(t, paths=(f"src/{t}.py",)) for t in ("a", "b", "c")],
            suite_command="pytest -q --full",
        )
        integration = plan.tasks[-1]
        assert integration.owned_paths == ["."]
        assert integration.verify_command == "pytest -q --full"
        # No worker may claim the whole tree — that is the merger's alone.
        assert all(w.owned_paths != ["."] for w in _workers(plan))

    def test_nothing_depends_on_the_merger(self) -> None:
        plan = _fanout_plan()
        for task in plan.tasks:
            assert INTEGRATION_TASK_ID not in task.depends_on

    def test_bootstrap_is_a_separate_prerequisite_of_every_worker(self) -> None:
        plan = build_plan(
            "goal",
            [_raw(t, paths=(f"src/{t}.py",)) for t in ("a", "b", "c", "d")],
            needs_install=True,
            install_command="npm ci",
            suite_command="npm test",
        )
        assert plan.tasks[0].id == BOOTSTRAP_TASK_ID
        assert plan.tasks[0].verify_command == ""
        assert "npm ci" in plan.tasks[0].description
        for worker in _workers(plan):
            assert BOOTSTRAP_TASK_ID in worker.depends_on
        assert plan.tasks[-1].id == INTEGRATION_TASK_ID
