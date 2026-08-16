"""AT-01 — Agent creation and hierarchy.

Two hierarchies decide *which agents exist* in this system, and both are
checked here against their real implementations:

* the **run hierarchy** — ``swarm.planner.build_plan`` turns raw planner output
  into the DAG that the scheduler spawns one worker per. The integration task
  is the run's leader (appended by the planner, never by the model, owning the
  whole workspace and gating on every leaf), and ``provision_run`` is where the
  leader/worker split becomes a persisted ``formation_role`` on a board card.
* the **org hierarchy** — ``orgdims.company_org.seed`` builds company →
  department → agent. Leadership is ``org_role='manager'`` scoped to a
  department; parentage is ``agents.org_unit_id`` / ``org_units.parent_id``.

The questions this file answers: are the right agents spawned, are leaders and
workers created correctly, is the hierarchy well-formed, and are duplicate or
orphaned agents prevented.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.orgdims import company_org as org
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.swarm.contracts import SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import (
    BOOTSTRAP_TASK_ID,
    INTEGRATION_TASK_ID,
    MAX_TASKS,
    SwarmPlanError,
    build_plan,
    provision_run,
)


def _raw(
    task_id: str,
    *,
    deps: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    agent_minutes: int = 10,
) -> dict[str, object]:
    """One raw planner task, the shape ``build_plan`` actually consumes."""

    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"deliver {task_id}",
        "depends_on": list(deps),
        "owned_paths": list(paths or (f"src/{task_id}",)),
        "est_agent_minutes": agent_minutes,
        "est_manual_minutes": agent_minutes * 3,
        "acceptance": f"{task_id} works",
        "verify_command": "git diff --check",
    }


# ---------------------------------------------------------------------------
# Run hierarchy: which workers exist, and who leads them
# ---------------------------------------------------------------------------


class TestRunHierarchy:
    def test_exactly_one_integration_leader_is_appended_by_the_planner(self) -> None:
        plan = build_plan("goal", [_raw("a"), _raw("b"), _raw("c")])

        leaders = [task for task in plan.tasks if task.id == INTEGRATION_TASK_ID]
        assert len(leaders) == 1, f"expected one integration leader, got {[t.id for t in leaders]}"
        assert plan.integration_task_id == INTEGRATION_TASK_ID
        # The leader must be last: the scheduler walks the DAG in plan order and
        # a leader placed among the workers would be eligible before they finish.
        assert plan.tasks[-1].id == INTEGRATION_TASK_ID
        assert {task.id for task in plan.tasks} == {"a", "b", "c", INTEGRATION_TASK_ID}

    def test_integration_leader_gates_on_every_leaf_and_only_leaves(self) -> None:
        # c depends on a, so the leaves are b and c. A leader that also listed
        # 'a' would serialize pointlessly; one that omitted 'c' would merge
        # before c finished.
        plan = build_plan("goal", [_raw("a"), _raw("b"), _raw("c", deps=("a",))])

        leader = next(task for task in plan.tasks if task.id == INTEGRATION_TASK_ID)
        assert sorted(leader.depends_on) == ["b", "c"]

    def test_leader_owns_the_workspace_and_workers_never_do(self) -> None:
        plan = build_plan("goal", [_raw("a"), _raw("b"), _raw("c")])

        leader = next(task for task in plan.tasks if task.id == INTEGRATION_TASK_ID)
        assert leader.owned_paths == ["."]
        for worker in (task for task in plan.tasks if task.id != INTEGRATION_TASK_ID):
            assert worker.owned_paths and "." not in worker.owned_paths, (
                f"worker {worker.id!r} owns the whole workspace: {worker.owned_paths}"
            )

    def test_bootstrap_leader_is_created_and_every_worker_waits_on_it(self) -> None:
        plan = build_plan(
            "goal",
            [_raw("a"), _raw("b"), _raw("c")],
            needs_install=True,
            install_command="uv sync",
        )

        assert plan.tasks[0].id == BOOTSTRAP_TASK_ID
        assert plan.tasks[0].verify_command == ""
        assert "uv sync" in plan.tasks[0].description
        for worker in plan.tasks:
            if worker.id == BOOTSTRAP_TASK_ID:
                continue
            reachable = worker.depends_on
            if worker.id == INTEGRATION_TASK_ID:
                # The leader gates on the leaves, which each gate on bootstrap.
                continue
            assert BOOTSTRAP_TASK_ID in reachable, (
                f"worker {worker.id!r} could start before dependencies were installed"
            )

    def test_duplicate_worker_ids_are_rejected_before_anything_is_spawned(self) -> None:
        with pytest.raises(SwarmPlanError, match="duplicate task id"):
            build_plan("goal", [_raw("a"), _raw("b"), _raw("a")])

    @pytest.mark.parametrize("reserved", [INTEGRATION_TASK_ID, BOOTSTRAP_TASK_ID])
    def test_a_worker_may_not_impersonate_a_reserved_leader(self, reserved: str) -> None:
        # Without this, a model-authored task named "integration" would collide
        # with the planner's own leader and one of the two would silently win.
        with pytest.raises(SwarmPlanError, match="reserved"):
            build_plan("goal", [_raw("a"), _raw(reserved)])

    def test_more_workers_than_the_cap_is_refused_not_truncated(self) -> None:
        with pytest.raises(SwarmPlanError, match="task cap exceeded"):
            build_plan("goal", [_raw(f"t{i}") for i in range(MAX_TASKS + 1)])

    def test_orphan_dependency_on_a_nonexistent_worker_is_dropped_and_recorded(self) -> None:
        plan = build_plan("goal", [_raw("a"), _raw("b", deps=("ghost",)), _raw("c")])

        known = {task.id for task in plan.tasks}
        for task in plan.tasks:
            dangling = [dep for dep in task.depends_on if dep not in known]
            assert not dangling, f"task {task.id!r} still waits on missing worker(s) {dangling}"
        assert any("ghost" in note for note in plan.assumptions), (
            f"the dropped dependency was not recorded: {plan.assumptions}"
        )

    def test_self_dependency_is_dropped_so_a_worker_cannot_deadlock_itself(self) -> None:
        plan = build_plan("goal", [_raw("a"), _raw("b", deps=("b",)), _raw("c")])

        worker_b = next(task for task in plan.tasks if task.id == "b")
        assert "b" not in worker_b.depends_on

    def test_a_dependency_cycle_is_repaired_rather_than_shipped(self) -> None:
        plan = build_plan("goal", [_raw("a", deps=("b",)), _raw("b", deps=("a",)), _raw("c")])

        # Topologically orderable == acyclic. Any remaining cycle leaves at
        # least one task permanently ineligible and the run hangs.
        ordered: list[str] = []
        by_id = {task.id: task for task in plan.tasks}
        remaining = set(by_id)
        while remaining:
            ready = sorted(
                task_id
                for task_id in remaining
                if all(dep in ordered or dep not in by_id for dep in by_id[task_id].depends_on)
            )
            assert ready, f"cycle survived planning among {sorted(remaining)}"
            ordered.extend(ready)
            remaining -= set(ready)
        assert any("cycle repaired" in note for note in plan.assumptions), plan.assumptions

    def test_workers_owning_the_same_path_are_serialized_not_run_concurrently(self) -> None:
        plan = build_plan(
            "goal", [_raw("a", paths=("src/shared",)), _raw("b", paths=("src/shared",))]
        )

        a_then_b = "a" in next(t for t in plan.tasks if t.id == "b").depends_on
        b_then_a = "b" in next(t for t in plan.tasks if t.id == "a").depends_on
        assert a_then_b or b_then_a, (
            "two workers own src/shared with no ordering edge — they would collide"
        )
        assert any("ownership overlap" in note for note in plan.assumptions), plan.assumptions


# ---------------------------------------------------------------------------
# Provisioning: the leader/worker split becomes a persisted role
# ---------------------------------------------------------------------------


class TestProvisionedRoles:
    def test_leader_is_provisioned_as_integrator_and_workers_as_implementers(
        self, migrated_db_path: str, tmp_path: Path
    ) -> None:
        collab = CollabStore(migrated_db_path)
        dal = SwarmDal(migrated_db_path)
        try:
            plan = build_plan("goal", [_raw("a"), _raw("b"), _raw("c")])
            assert plan.formation is not None, "no formation bound; role stamping is unreachable"
            result = provision_run(plan, dal=dal, working_dir=str(tmp_path), write_plan_doc=False)

            roles: dict[str, str] = {}
            for task_key, card_id in result["card_ids"].items():
                card = collab.get_board_task(card_id)
                assert card is not None
                swarm_json = json.loads(card["swarm_json"])
                roles[task_key] = swarm_json["formation_role"]
                assert swarm_json["integration"] is (task_key == INTEGRATION_TASK_ID)

            # The integration task is `integrator`, not `reviewer`: 8b462c78 collapsed
            # the two names planner.py and scheduler.py used for this one task.
            assert roles[INTEGRATION_TASK_ID] == "integrator"
            assert {roles[key] for key in ("a", "b", "c")} == {"implementer"}
        finally:
            dal.close()

    def test_every_planned_worker_gets_exactly_one_board_card(
        self, migrated_db_path: str, tmp_path: Path
    ) -> None:
        collab = CollabStore(migrated_db_path)
        dal = SwarmDal(migrated_db_path)
        try:
            plan = build_plan("goal", [_raw("a"), _raw("b"), _raw("c")])
            result = provision_run(plan, dal=dal, working_dir=str(tmp_path), write_plan_doc=False)

            card_ids = result["card_ids"]
            assert set(card_ids) == {task.id for task in plan.tasks}
            assert len(set(card_ids.values())) == len(card_ids), "two tasks share a board card"
            # The root card is the run itself, never an eligible work unit.
            root = collab.get_board_task(result["root_card_id"])
            assert root is not None
            assert root["id"] not in set(card_ids.values())
            assert root["status"] == "in_progress"
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# Org hierarchy: company -> department -> agent
# ---------------------------------------------------------------------------


class TestOrgHierarchy:
    def test_seed_builds_a_single_rooted_two_level_tree(
        self, reliability_store: SqliteReliabilityStore, vault_dir: str
    ) -> None:
        org.seed(reliability_store, vault_dir=vault_dir, vault_autocommit=False)

        units = reliability_store.list_org_units()
        roots = [unit for unit in units if unit.parent_id is None]
        assert len(roots) == 1, f"expected one company root, got {[u.name for u in roots]}"
        assert roots[0].kind == "company"
        assert roots[0].name == org.COMPANY_NAME

        departments = [unit for unit in units if unit.kind == "department"]
        assert {unit.name for unit in departments} == {d["name"] for d in org.DEPARTMENTS}
        for department in departments:
            assert department.parent_id == roots[0].id, (
                f"department {department.name!r} is parented to {department.parent_id!r}, "
                f"not the company root"
            )

    def test_no_seeded_agent_is_an_orphan(
        self, reliability_store: SqliteReliabilityStore, vault_dir: str
    ) -> None:
        org.seed(reliability_store, vault_dir=vault_dir, vault_autocommit=False)

        unit_ids = {unit.id for unit in reliability_store.list_org_units()}
        # ``agents`` is also the identity table broker grants FK to, so since
        # migration 108 it holds machine grant holders (``loop:<instance>``)
        # alongside the org roster. Those are deliberately unparented; this
        # assertion is about the agents ORG SEEDING created.
        from omniagentos.context.lanes import is_machine_identity

        everyone = reliability_store.list_agents()
        # State the contract this filter depends on, rather than only relying
        # on it: machine grant holders are present and are deliberately
        # unparented, because they are identities, not staff.
        machines = [agent for agent in everyone if is_machine_identity(agent.id)]
        assert machines, "migration 108's grant holders must be visible in `agents`"
        assert all(agent.org_unit_id is None for agent in machines)

        agents = [agent for agent in everyone if not is_machine_identity(agent.id)]
        assert agents
        for agent in agents:
            assert agent.org_unit_id is not None, f"agent {agent.name!r} has no org unit"
            assert agent.org_unit_id in unit_ids, (
                f"agent {agent.name!r} points at missing org unit {agent.org_unit_id!r}"
            )

    def test_every_department_has_exactly_one_manager(
        self, reliability_store: SqliteReliabilityStore, vault_dir: str
    ) -> None:
        org.seed(reliability_store, vault_dir=vault_dir, vault_autocommit=False)

        managers = reliability_store.list_agents(org_role="manager")
        by_unit: dict[str, list[str]] = {}
        for manager in managers:
            assert manager.org_unit_id is not None
            by_unit.setdefault(manager.org_unit_id, []).append(manager.name)

        departments = [u for u in reliability_store.list_org_units() if u.kind == "department"]
        for department in departments:
            leaders = by_unit.get(department.id, [])
            assert len(leaders) == 1, (
                f"department {department.name!r} has {len(leaders)} managers: {leaders}"
            )

    def test_reseeding_creates_no_duplicate_agents_or_units(
        self, reliability_store: SqliteReliabilityStore, vault_dir: str
    ) -> None:
        first = org.seed(reliability_store, vault_dir=vault_dir, vault_autocommit=False)
        assert first["agents_created"]

        agents_after_first = {a.name for a in reliability_store.list_agents()}
        units_after_first = {u.name for u in reliability_store.list_org_units()}

        second = org.seed(reliability_store, vault_dir=vault_dir, vault_autocommit=False)

        assert second["agents_created"] == [], (
            f"re-seeding spawned duplicate agents: {second['agents_created']}"
        )
        assert second["org_units_created"] == []
        assert {a.name for a in reliability_store.list_agents()} == agents_after_first
        assert {u.name for u in reliability_store.list_org_units()} == units_after_first
        names = [a.name for a in reliability_store.list_agents()]
        assert len(names) == len(set(names))

    def test_database_refuses_a_second_agent_with_the_same_name(
        self, reliability_store: SqliteReliabilityStore
    ) -> None:
        unit_id = reliability_store.create_org_unit(name="Unit", kind="company")
        reliability_store.create_agent(name="Solo", org_unit_id=unit_id)

        with pytest.raises(sqlite3.IntegrityError):
            reliability_store.create_agent(name="Solo", org_unit_id=unit_id)

    def test_database_refuses_an_agent_parented_to_a_missing_org_unit(
        self, reliability_store: SqliteReliabilityStore
    ) -> None:
        # agents.org_unit_id REFERENCES org_units(id) and PRAGMA foreign_keys=ON:
        # an agent can never be created under a department that does not exist.
        with pytest.raises(sqlite3.IntegrityError):
            reliability_store.create_agent(name="Ghost", org_unit_id="org_does_not_exist")

    def test_database_refuses_an_org_unit_parented_to_a_missing_unit(
        self, reliability_store: SqliteReliabilityStore
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            reliability_store.create_org_unit(
                name="Floating", kind="department", parent_id="org_missing"
            )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: org_units has no self-parent CHECK and no cycle guard anywhere. "
            "ProjectStore.set_parent walks the ancestor chain and raises ProjectError on "
            "a cycle; the org tree has no equivalent, so a unit can be made its own "
            "ancestor and the hierarchy stops being a tree. This xfail flips to a pass "
            "the day a CHECK (parent_id IS NULL OR parent_id <> id) or an equivalent "
            "guard lands. See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_org_unit_cannot_become_its_own_parent(
        self, reliability_store: SqliteReliabilityStore
    ) -> None:
        unit_id = reliability_store.create_org_unit(name="Co", kind="company")
        conn = reliability_store._connection
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE org_units SET parent_id = id WHERE id = ?", (unit_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# Claiming: exactly one agent may own a unit of work
# ---------------------------------------------------------------------------


class TestClaimExclusivity:
    def _open_task(self, collab: CollabStore) -> str:
        from omniagentos.collab.contracts import BoardTask

        task = BoardTask(id="btk_accept_1", title="Work", description="body")
        collab.create_board_task(task)
        return task.id

    def test_only_one_agent_wins_a_concurrent_claim(self, collab_store: CollabStore) -> None:
        task_id = self._open_task(collab_store)
        row = collab_store.get_board_task(task_id)
        assert row is not None
        version = int(row["claim_version"])

        outcomes = [
            collab_store.claim_task(task_id, "agt_one", version),
            collab_store.claim_task(task_id, "agt_two", version),
        ]
        assert outcomes.count(True) == 1, f"claim CAS admitted {outcomes.count(True)} winners"

        claimed = collab_store.get_board_task(task_id)
        assert claimed is not None
        assert claimed["status"] == "claimed"
        assert claimed["claimed_by"] == "agt_one"
        assert int(claimed["claim_version"]) == version + 1

    def test_a_stale_claim_version_never_steals_a_claimed_task(
        self, collab_store: CollabStore
    ) -> None:
        task_id = self._open_task(collab_store)
        row = collab_store.get_board_task(task_id)
        assert row is not None
        version = int(row["claim_version"])
        assert collab_store.claim_task(task_id, "agt_one", version) is True

        assert collab_store.claim_task(task_id, "agt_two", version) is False
        assert collab_store.claim_task(task_id, "agt_two", version + 1) is False
        still = collab_store.get_board_task(task_id)
        assert still is not None
        assert still["claimed_by"] == "agt_one"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: CollabStore.claim_task validates only (id, status, claim_version). "
            "board_tasks.claimed_by has no FK to agents(id) and no application check, so "
            "an unregistered/never-spawned agent id can own a task and the board shows "
            "work assigned to nobody. See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_an_unregistered_agent_cannot_claim_work(self, collab_store: CollabStore) -> None:
        task_id = self._open_task(collab_store)
        row = collab_store.get_board_task(task_id)
        assert row is not None
        assert (
            collab_store.claim_task(task_id, "agt_never_registered", int(row["claim_version"]))
            is False
        )


def test_planner_task_spec_is_the_shape_the_scheduler_spawns() -> None:
    """Guard against a rename silently decoupling planning from spawning."""

    plan = build_plan("goal", [_raw("a"), _raw("b")])
    for task in plan.tasks:
        assert isinstance(task, SwarmTaskSpec)
        assert task.id and task.title
