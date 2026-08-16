"""WP4 swarm planner: pure validation, pipeline seams, transactional provisioning,
PLAN.md rendering, bundle decomposition, and plan-hash stability."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import (
    BOOTSTRAP_TASK_ID,
    INTEGRATION_TASK_ID,
    LESSONS_TOKEN_CAP,
    MAX_TASKS,
    PLAYBOOK_FEED_ENV,
    SwarmPlanError,
    build_lessons_block,
    build_plan,
    critical_path_minutes,
    is_shared_path,
    normalize_owned_path,
    parallelism_stats,
    plan_fingerprint,
    plan_swarm,
    plan_swarm_bundles,
    provision_run,
    render_plan_md,
    topo_sort_with_repair,
    write_plan_md,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "swarm-planner.db")
    CollabStore(db)  # migrates the shared schema (incl. 045)
    return db


def _raw_task(
    task_id: str,
    deps: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    agent: int = 10,
    manual: int = 30,
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": list(paths),
        "est_agent_minutes": agent,
        "est_manual_minutes": manual,
        "acceptance": f"{task_id} done",
        "verify_command": "git diff --check",
    }
    base.update(overrides)
    return base


def _spec(task_id: str, deps: tuple[str, ...] = (), agent: int = 10, **kw) -> SwarmTaskSpec:
    return SwarmTaskSpec(
        id=task_id,
        title=task_id.upper(),
        depends_on=list(deps),
        est_agent_minutes=agent,
        **kw,
    )


class RecordingLLM:
    """Fake planner seam: returns queued responses, records every call."""

    def __init__(self, *responses: dict[str, Any] | None) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
        self.calls.append({"prompt": prompt, "schema": schema, "effort": effort})
        return self.responses.pop(0) if self.responses else None


def _clarify_questions(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "questions", "questions": ["Which database should this use?"]}


def _clarify_spec(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "spec",
        "spec": {
            "title": "Refined widget goal",
            "description": "Ship the widget end to end.",
            "acceptance_criteria": ["widget ships"],
        },
    }


# ---------------------------------------------------------------------------
# Pure validation
# ---------------------------------------------------------------------------


class TestOwnedPathContainment:
    def test_normalizes_relative_paths(self) -> None:
        assert normalize_owned_path("./a//b/") == "a/b"
        assert normalize_owned_path("a/../b") == "b"
        assert normalize_owned_path("a/..") == "."
        assert normalize_owned_path("src\\api") == "src/api"

    @pytest.mark.parametrize(
        "bad", ["/etc/passwd", "~secrets", "C:/windows", "../up", "a/../../b", "  "]
    )
    def test_rejects_absolute_and_escaping(self, bad: str) -> None:
        with pytest.raises(SwarmPlanError):
            normalize_owned_path(bad)

    def test_build_plan_rejects_escaping_owned_path(self) -> None:
        tasks = [_raw_task("a", paths=("../outside",)), _raw_task("b"), _raw_task("c")]
        with pytest.raises(SwarmPlanError, match="escapes"):
            build_plan("goal", tasks)


class TestSharedPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "package.json",
            "web/package.json",
            "uv.lock",
            "db/migrations/001_init.sql",
            "pyproject.toml",
            "tsconfig.json",
            ".github/workflows/ci.yml",
            "Makefile",
        ],
    )
    def test_shared(self, path: str) -> None:
        assert is_shared_path(normalize_owned_path(path)) is True

    @pytest.mark.parametrize("path", ["src/app.py", "docs/config.json", "src", "."])
    def test_not_shared(self, path: str) -> None:
        assert is_shared_path(normalize_owned_path(path)) is False

    def test_shared_files_move_to_integration_ownership(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/a.py", "package.json")),
            _raw_task("b", paths=("src/b.py",)),
            _raw_task("c", paths=("src/c.py",)),
        ]
        plan = build_plan("goal", tasks)
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert task_a.owned_paths == ["src/a.py"]
        assert any("package.json" in note and "integration" in note for note in plan.assumptions)


class TestTopoRepair:
    def test_clean_dag_orders_prerequisites_first(self) -> None:
        tasks = [_spec("c", deps=("b",)), _spec("b", deps=("a",)), _spec("a")]
        order, removed = topo_sort_with_repair(tasks)
        assert order == ["a", "b", "c"]
        assert removed == []

    def test_two_cycle_repaired_by_dropping_forward_edge(self) -> None:
        # Output order [a, b]: a's dep on the LATER task b is the suspect edge.
        tasks = [_spec("a", deps=("b",)), _spec("b", deps=("a",))]
        order, removed = topo_sort_with_repair(tasks)
        assert removed == [("a", "b")]
        assert order == ["a", "b"]

    def test_two_independent_cycles_fail_cleanly_after_one_repair(self) -> None:
        tasks = [
            _spec("a", deps=("c",)),
            _spec("b", deps=("c",)),
            _spec("c", deps=("a", "b")),
        ]
        with pytest.raises(SwarmPlanError, match="cycle"):
            topo_sort_with_repair(tasks)

    def test_build_plan_records_cycle_repair_and_updates_deps(self) -> None:
        tasks = [
            _raw_task("a", deps=("b",)),
            _raw_task("b", deps=("a",)),
            _raw_task("c"),
        ]
        plan = build_plan("goal", tasks)
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert "b" not in task_a.depends_on
        assert any("cycle repaired" in note for note in plan.assumptions)

    def test_unknown_and_self_deps_dropped_with_note(self) -> None:
        tasks = [
            _raw_task("a", deps=("a", "ghost")),
            _raw_task("b"),
            _raw_task("c"),
        ]
        plan = build_plan("goal", tasks)
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert task_a.depends_on == []
        assert any("ghost" in note for note in plan.assumptions)


class TestOwnershipOverlap:
    def test_later_task_depends_on_earlier(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/a.py",)),
            _raw_task("b", paths=("src",)),  # dir containing src/a.py
            _raw_task("c", paths=("lib",)),
        ]
        plan = build_plan("goal", tasks)
        task_b = next(t for t in plan.tasks if t.id == "b")
        task_a = next(t for t in plan.tasks if t.id == "a")
        assert "a" in task_b.depends_on  # later depends on earlier
        assert "b" not in task_a.depends_on  # never the reverse
        assert any("ownership overlap" in note for note in plan.assumptions)

    def test_already_related_pair_left_alone(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/x.py",)),
            _raw_task("b", deps=("a",), paths=("src/x.py",)),
            _raw_task("c", paths=("lib",)),
        ]
        plan = build_plan("goal", tasks)
        task_b = next(t for t in plan.tasks if t.id == "b")
        assert task_b.depends_on.count("a") == 1
        assert not any("ownership overlap" in note for note in plan.assumptions)


class TestPlanAssembly:
    def test_task_cap_and_reserved_and_duplicate_ids(self) -> None:
        with pytest.raises(SwarmPlanError, match="task cap"):
            build_plan("goal", [_raw_task(f"t{i}") for i in range(MAX_TASKS + 1)])
        with pytest.raises(SwarmPlanError, match="reserved"):
            build_plan("goal", [_raw_task("integration"), _raw_task("b"), _raw_task("c")])
        with pytest.raises(SwarmPlanError, match="duplicate"):
            build_plan("goal", [_raw_task("a"), _raw_task("a"), _raw_task("c")])

    def test_bootstrap_inserted_when_installs_needed(self) -> None:
        tasks = [_raw_task(t, paths=(f"src/{t}",)) for t in ("a", "b", "c", "d")]
        plan = build_plan(
            "goal",
            tasks,
            needs_install=True,
            install_command="npm ci",
            suite_command="npm test",
        )
        assert plan.tasks[0].id == BOOTSTRAP_TASK_ID
        assert plan.tasks[0].verify_command == ""
        assert "npm ci" in plan.tasks[0].description
        for worker_id in ("a", "b", "c", "d"):
            worker = next(t for t in plan.tasks if t.id == worker_id)
            assert BOOTSTRAP_TASK_ID in worker.depends_on

    def test_integration_appended_with_leaf_deps_and_suite_verify(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/a",)),
            _raw_task("b", deps=("a",), paths=("src/b",)),
            _raw_task("c", paths=("src/c",)),
        ]
        plan = build_plan("goal", tasks, suite_command="pytest -q")
        integration = plan.tasks[-1]
        assert integration.id == INTEGRATION_TASK_ID
        assert plan.integration_task_id == INTEGRATION_TASK_ID
        assert sorted(integration.depends_on) == ["b", "c"]  # leaves only
        assert integration.owned_paths == ["."]
        assert integration.verify_command == "pytest -q"

    def test_ratio_target_math(self) -> None:
        # Chain a→b→c (10 each) plus independent d (30): total 60, cp 30.
        specs = [
            _spec("a"),
            _spec("b", deps=("a",)),
            _spec("c", deps=("b",)),
            _spec("d", agent=30),
        ]
        assert critical_path_minutes(specs) == 30
        ratio, target_n = parallelism_stats(specs)
        assert ratio == pytest.approx(2.0)
        assert target_n == 2

        plan = build_plan(
            "goal",
            [
                _raw_task("a"),
                _raw_task("b", deps=("a",)),
                _raw_task("c", deps=("b",)),
                _raw_task("d", agent=30),
            ],
        )
        assert plan.mode == "swarm"
        assert plan.parallelism_ratio == pytest.approx(2.0)
        assert plan.target_n == 2

    def test_target_n_clamped_to_five(self) -> None:
        _, target_n = parallelism_stats([_spec(f"t{i}") for i in range(8)])
        assert target_n == 5

    def test_solo_when_two_or_fewer_tasks(self) -> None:
        plan = build_plan("goal", [_raw_task("a"), _raw_task("b")])
        assert plan.mode == "solo"
        assert plan.target_n == 1
        assert plan.integration_task_id is None
        assert [t.id for t in plan.tasks] == ["a", "b"]  # no auto tasks in solo

    def test_solo_when_ratio_below_threshold(self) -> None:
        plan = build_plan(
            "goal",
            [_raw_task("a"), _raw_task("b", deps=("a",)), _raw_task("c", deps=("b",))],
        )
        assert plan.parallelism_ratio == pytest.approx(1.0)
        assert plan.mode == "solo"
        assert plan.target_n == 1


# ---------------------------------------------------------------------------
# Pipeline (fake seams — deterministic, no model)
# ---------------------------------------------------------------------------


def _valid_response(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "goal": "planned goal",
        "assumptions": ["model assumed X"],
        "tasks": [
            _raw_task("a", paths=("src/a",)),
            _raw_task("b", paths=("src/b",)),
            _raw_task("c", paths=("src/c",)),
        ],
        "suite_command": "pytest -q",
    }
    payload.update(extra)
    return payload


class TestPlanSwarmPipeline:
    def test_happy_path_with_refined_spec_goal(self, tmp_path: Path) -> None:
        llm = RecordingLLM(_valid_response())
        plan = plan_swarm(
            "make a widget",
            str(tmp_path),
            planner_llm=llm,
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert plan.mode == "swarm"
        assert "model assumed X" in plan.assumptions
        assert plan.integration_task_id == INTEGRATION_TASK_ID
        assert len(llm.calls) == 1
        assert "Refined widget goal" in llm.calls[0]["prompt"]

    def test_clarify_questions_become_assumptions_never_a_wait(self, tmp_path: Path) -> None:
        llm = RecordingLLM(_valid_response())
        plan = plan_swarm(
            "make a widget",
            str(tmp_path),
            planner_llm=llm,
            clarify_llm=_clarify_questions,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert any("Which database" in note for note in plan.assumptions)
        assert "Which database" in llm.calls[0]["prompt"]  # surfaced to the planner too

    def test_invalid_then_valid_reprompts_once(self, tmp_path: Path) -> None:
        llm = RecordingLLM({"tasks": []}, _valid_response())
        plan = plan_swarm(
            "make a widget",
            str(tmp_path),
            planner_llm=llm,
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert len(llm.calls) == 2
        assert "rejected" in llm.calls[1]["prompt"]
        assert plan.mode == "swarm"

    def test_unavailable_twice_degrades_to_flat_solo_plan(self, tmp_path: Path) -> None:
        llm = RecordingLLM(None, None)
        plan = plan_swarm(
            "make a widget",
            str(tmp_path),
            planner_llm=llm,
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert len(llm.calls) == 2
        assert plan.mode == "solo"
        assert len(plan.tasks) == 1
        assert plan.integration_task_id is None
        assert any("degraded to flat solo plan" in note for note in plan.assumptions)

    def test_invalid_twice_degrades_to_flat_solo_plan(self, tmp_path: Path) -> None:
        llm = RecordingLLM({"tasks": []}, {"tasks": [_raw_task("a", paths=("/abs",))]})
        plan = plan_swarm(
            "make a widget",
            str(tmp_path),
            planner_llm=llm,
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert len(llm.calls) == 2
        assert plan.mode == "solo"
        assert any("degraded to flat solo plan" in note for note in plan.assumptions)

    def test_bundle_decomposition_returns_independent_plans(self, tmp_path: Path) -> None:
        response = {
            "goal": "two asks",
            "bundles": [
                {
                    "goal": "fix the API",
                    "tasks": [
                        _raw_task("a1", paths=("api/a",)),
                        _raw_task("a2", paths=("api/b",)),
                        _raw_task("a3", paths=("api/c",)),
                    ],
                    "suite_command": "pytest api",
                },
                {
                    "goal": "refresh the docs site",
                    "tasks": [_raw_task("d1", paths=("docs",))],
                },
            ],
        }
        plans = plan_swarm_bundles(
            "fix the API and also refresh the docs site",
            str(tmp_path),
            planner_llm=RecordingLLM(response),
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert [plan.goal for plan in plans] == ["fix the API", "refresh the docs site"]
        assert plans[0].mode == "swarm"
        assert plans[0].tasks[-1].verify_command == "pytest api"
        assert plans[1].mode == "solo"  # single task bundle

        primary = plan_swarm(
            "fix the API and also refresh the docs site",
            str(tmp_path),
            planner_llm=RecordingLLM(response),
            clarify_llm=_clarify_spec,
            recall_fn=lambda goal: "",
            playbook_path=tmp_path / "missing.json",
        )
        assert primary.goal == "fix the API"
        assert any("refresh the docs site" in note for note in primary.assumptions)

    def test_prior_lessons_flow_into_the_prompt(
        self, db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The optimizer playbook is DISARMED by default (PLAYBOOK_FEED_ENV) because
        # the objective function that writes it is inverted — see
        # tests/swarm/test_playbook_feed_guard.py. This test covers the delivery
        # mechanism, so it arms the feed explicitly rather than dropping the
        # assertion: the playbook must still reach the prompt when enabled, which
        # is what makes the guard a switch instead of a deletion.
        monkeypatch.setenv(PLAYBOOK_FEED_ENV, "1")
        dal = SwarmDal(db_path)
        try:
            run = dal.create_run(working_dir="/tmp/ws", goal="previous swarm goal", source="test")
            dal.set_run_status(run["id"], "running")
            dal.set_metrics(run["id"], {"speedup": 3.0})
            dal.set_run_status(run["id"], "completed")
            playbook = tmp_path / "learned.json"
            playbook.write_text('{"tip": "keep owned paths disjoint"}', encoding="utf-8")

            llm = RecordingLLM(_valid_response())
            plan_swarm(
                "make a widget",
                str(tmp_path),
                planner_llm=llm,
                clarify_llm=_clarify_spec,
                dal=dal,
                recall_fn=lambda goal: "RECALLED-SWARM-FACT",
                playbook_path=playbook,
            )
            prompt = llm.calls[0]["prompt"]
            assert "previous swarm goal" in prompt
            assert "speedup" in prompt
            assert "RECALLED-SWARM-FACT" in prompt
            assert "keep owned paths disjoint" in prompt
        finally:
            dal.close()

    def test_lessons_block_is_token_capped_and_never_raises(self, tmp_path: Path) -> None:
        playbook = tmp_path / "learned.json"
        playbook.write_text("x" * 30_000, encoding="utf-8")
        block = build_lessons_block("goal", recall_fn=lambda goal: "", playbook_path=playbook)
        assert len(block) <= LESSONS_TOKEN_CAP * 4

        def boom(goal: str) -> str:
            raise RuntimeError("recall down")

        assert (
            build_lessons_block("goal", recall_fn=boom, playbook_path=tmp_path / "missing.json")
            == ""
        )


# ---------------------------------------------------------------------------
# Transactional provisioning
# ---------------------------------------------------------------------------


class TestProvisioning:
    def _plan(self) -> SwarmPlan:
        return build_plan(
            "ship the swarm feature",
            [_raw_task(t, paths=(f"src/{t}",)) for t in ("a", "b", "c", "d")],
            needs_install=True,
            install_command="npm ci",
            suite_command="git diff --check",
        )

    def test_provision_writes_run_cards_deps_atomically(self, db_path: str, tmp_path: Path) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            plan = self._plan()
            workspace = tmp_path / "ws"
            workspace.mkdir()
            result = provision_run(plan, dal=dal, working_dir=str(workspace), budget_usd_max=4.0)
            run = result["run"]
            assert run["status"] == "planning"
            assert run["target_concurrency"] == plan.target_n
            payload = json.loads(run["plan_json"])
            assert payload["plan_hash"] == result["plan_hash"] == plan_fingerprint(plan)
            assert payload["version"] == 1
            assert len(payload["tasks"]) == 6  # bootstrap + 4 workers + integration

            root = collab.get_board_task(result["root_card_id"])
            assert run["board_task_id"] == result["root_card_id"]
            assert root["status"] == "in_progress"
            assert root["swarm_run_id"] == run["id"]
            assert root["lane"] is None

            assert set(result["card_ids"]) == {
                BOOTSTRAP_TASK_ID,
                "a",
                "b",
                "c",
                "d",
                INTEGRATION_TASK_ID,
            }
            for task_key, card_id in result["card_ids"].items():
                card = collab.get_board_task(card_id)
                assert card["status"] == "open"
                assert card["lane"] is None
                assert card["swarm_run_id"] == run["id"]
                swarm_json = dal.get_swarm_json(card_id)
                assert swarm_json["task_key"] == task_key
                assert swarm_json["plan_hash"] == result["plan_hash"]
                assert swarm_json["plan_version"] == 1
            integration_json = dal.get_swarm_json(result["card_ids"][INTEGRATION_TASK_ID])
            assert integration_json["integration"] is True
            assert integration_json["verify_command"] == "git diff --check"

            # 4 worker→bootstrap edges + 4 integration→leaf edges.
            assert len(dal.deps_for_run(run["id"])) == 8
            # Scheduler-ready: exactly the bootstrap task is eligible.
            eligible = dal.eligible_tasks(run["id"])
            assert [t["id"] for t in eligible] == [result["card_ids"][BOOTSTRAP_TASK_ID]]

            plan_md = workspace / "PLAN.md"
            assert plan_md.is_file()
            assert "## Task DAG" in plan_md.read_text(encoding="utf-8")
            assert list(workspace.glob(".PLAN.md.tmp-*")) == []
        finally:
            dal.close()

    def test_provision_stamps_category_taxonomy(self, db_path: str, tmp_path: Path) -> None:
        """FB4+ taxonomy: per-task category wins; the root-goal category fans
        out to tasks without one (and to the root + integration cards).
        Category is metadata — lane stays NULL on every card."""
        from omniagentos.longhaul.store import LonghaulStore

        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        longhaul = LonghaulStore(db_path)
        try:
            plan = build_plan(
                "ship the categorized feature",
                [
                    _raw_task("a", paths=("src/a",), category="Backend"),
                    _raw_task("b", paths=("src/b",)),
                    _raw_task("c", paths=("src/c",)),
                    _raw_task("d", paths=("src/d",)),
                ],
                category="Payments",
            )
            assert plan.category == "Payments"
            assert plan.tasks[0].category == "Backend"

            workspace = tmp_path / "ws-cat"
            workspace.mkdir()
            result = provision_run(plan, dal=dal, working_dir=str(workspace), write_plan_doc=False)

            backend = longhaul.get_category("backend")
            payments = longhaul.get_category("payments")
            assert backend is not None and payments is not None

            root = collab.get_board_task(result["root_card_id"])
            assert root["category_id"] == payments["id"]

            expectations = {
                "a": backend["id"],
                "b": payments["id"],
                "c": payments["id"],
                "d": payments["id"],
                INTEGRATION_TASK_ID: payments["id"],
            }
            for task_key, expected in expectations.items():
                card = collab.get_board_task(result["card_ids"][task_key])
                assert card["category_id"] == expected, task_key
                assert card["lane"] is None
        finally:
            dal.close()
            longhaul.close()

    def test_provision_without_categories_stays_uncategorized(
        self, db_path: str, tmp_path: Path
    ) -> None:
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            plan = self._plan()
            workspace = tmp_path / "ws-nocat"
            workspace.mkdir()
            result = provision_run(plan, dal=dal, working_dir=str(workspace), write_plan_doc=False)
            assert collab.get_board_task(result["root_card_id"])["category_id"] is None
            for card_id in result["card_ids"].values():
                assert collab.get_board_task(card_id)["category_id"] is None
        finally:
            dal.close()

    def test_crash_mid_provision_leaves_nothing(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            poisoned_cards = [
                {"id": "btk_dup", "title": "one", "swarm_json": {}},
                {"id": "btk_dup", "title": "two", "swarm_json": {}},  # PK collision
            ]
            with pytest.raises(sqlite3.IntegrityError):
                dal.provision_run(
                    run={"working_dir": "/tmp/ws", "goal": "boom", "plan": {}},
                    root_card={"id": "btk_root", "title": "root", "status": "in_progress"},
                    cards=poisoned_cards,
                    edges=[],
                )
            counts = {
                table: dal._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in ("swarm_runs", "board_tasks", "swarm_deps")
            }
            assert counts == {"swarm_runs": 0, "board_tasks": 0, "swarm_deps": 0}
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# Category resolver hygiene (planner output is untrusted)
# ---------------------------------------------------------------------------


class TestCategoryResolverHygiene:
    """F2: labels trimmed to 60 chars, empty-slug labels skipped, at most 8
    NEW categories per run (excess → uncategorized, logged; pre-existing
    categories always resolve and never count against the cap)."""

    def test_label_trimmed_to_60_chars(self, db_path: str) -> None:
        from omniagentos.longhaul.store import LonghaulStore
        from omniagentos.swarm.planner import (
            CATEGORY_LABEL_MAX_CHARS,
            _category_resolver,
        )

        resolve = _category_resolver(db_path)
        long_label = "Backend Platform " * 10  # far past 60 chars
        cat_id = resolve(long_label)
        assert cat_id is not None
        store = LonghaulStore(db_path)
        try:
            cats = store.list_categories()
            assert len(cats) == 1
            assert cats[0]["id"] == cat_id
            assert len(cats[0]["name"]) <= CATEGORY_LABEL_MAX_CHARS
        finally:
            store.close()
        # The pre-trimmed form resolves to the same category (no duplicate).
        assert resolve(long_label.strip()[:CATEGORY_LABEL_MAX_CHARS]) == cat_id

    def test_empty_slug_label_skipped(self, db_path: str) -> None:
        from omniagentos.longhaul.store import LonghaulStore
        from omniagentos.swarm.planner import _category_resolver

        resolve = _category_resolver(db_path)
        assert resolve("!!! ???") is None  # slugifies to nothing
        assert resolve("   ") is None
        assert resolve(None) is None
        store = LonghaulStore(db_path)
        try:
            assert store.list_categories() == []
        finally:
            store.close()

    def test_new_category_cap_excess_uncategorized_and_logged(
        self, db_path: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from omniagentos.longhaul.store import LonghaulStore
        from omniagentos.swarm.planner import (
            MAX_NEW_CATEGORIES_PER_RUN,
            _category_resolver,
        )

        store = LonghaulStore(db_path)
        try:
            pre = store.create_category("Existing")
            resolve = _category_resolver(db_path)
            with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.planner"):
                ids = [resolve(f"New {i}") for i in range(MAX_NEW_CATEGORIES_PER_RUN + 2)]
                assert all(ids[:MAX_NEW_CATEGORIES_PER_RUN])
                assert ids[MAX_NEW_CATEGORIES_PER_RUN:] == [None, None]
                # Pre-existing categories resolve even past the cap and never
                # counted against it.
                assert resolve("Existing") == pre["id"]
            assert "category cap" in caplog.text
            # Existing + exactly MAX_NEW new rows — the excess never landed.
            assert len(store.list_categories()) == 1 + MAX_NEW_CATEGORIES_PER_RUN
        finally:
            store.close()


# ---------------------------------------------------------------------------
# PLAN.md render (golden) + atomic write
# ---------------------------------------------------------------------------


def _golden_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="Ship the widget",
        tasks=[
            SwarmTaskSpec(
                id="api",
                title="API",
                depends_on=[],
                complexity="standard",
                risk_class="none",
                est_manual_minutes=30,
                est_agent_minutes=10,
                owned_paths=["src/api"],
                acceptance="API responds",
                verify_command="pytest tests/api",
            ),
            SwarmTaskSpec(
                id="ui",
                title="UI",
                depends_on=["api"],
                complexity="standard",
                risk_class="none",
                est_manual_minutes=30,
                est_agent_minutes=10,
                owned_paths=["src/ui"],
                acceptance="UI renders",
            ),
        ],
        assumptions=["Assumed Postgres"],
        parallelism_ratio=1.0,
        integration_task_id=None,
        mode="solo",
        version=1,
        target_n=1,
    )


class TestPlanMd:
    def test_render_golden(self) -> None:
        plan = _golden_plan()
        digest = plan_fingerprint(plan)[:12]
        expected = f"""# Swarm Plan — Ship the widget

> Derived from `swarm_runs.plan_json` v1 (hash `{digest}`). Machine truth lives in the database; the coordinator regenerates this file. Workers: verify the hash in your brief matches, and never edit this file.

## Goal

Ship the widget

## Assumptions

- Assumed Postgres

## Task DAG

| Task | Title | Depends on | Complexity | Risk | Est (agent min) | Status |
| --- | --- | --- | --- | --- | --- | --- |
| api | API | — | standard | none | 10 | done |
| ui | UI | api | standard | none | 10 | open |

## Interface Contracts

### api
- Acceptance: API responds
- Verify: `pytest tests/api`

### ui
- Acceptance: UI renders
- Verify: (none)

## File Ownership Map

| Task | Owned paths |
| --- | --- |
| api | `src/api` |
| ui | `src/ui` |

## Status

- Mode: solo
- Parallelism ratio: 1.0
- Target concurrency: 1
- Tasks done: 1/2
"""
        assert render_plan_md(plan, {"api": "done"}) == expected

    def test_write_plan_md_atomic(self, tmp_path: Path) -> None:
        plan = _golden_plan()
        target = write_plan_md(plan, tmp_path, {"api": "done"})
        assert target == tmp_path / "PLAN.md"
        assert target.read_text(encoding="utf-8") == render_plan_md(plan, {"api": "done"})
        assert list(tmp_path.glob(".PLAN.md.tmp-*")) == []


# ---------------------------------------------------------------------------
# Plan hash stability
# ---------------------------------------------------------------------------


class TestHashStability:
    def test_identical_plans_hash_identically(self) -> None:
        tasks = [
            _raw_task("a", paths=("src/a",)),
            _raw_task("b", paths=("src/b",)),
            _raw_task("c", paths=("src/c",)),
        ]
        first = build_plan("goal", tasks, suite_command="pytest -q")
        second = build_plan("goal", tasks, suite_command="pytest -q")
        assert plan_fingerprint(first) == plan_fingerprint(second)

    def test_any_material_change_changes_the_hash(self) -> None:
        base = _golden_plan()
        assert plan_fingerprint(base) == plan_fingerprint(_golden_plan())

        renamed = _golden_plan()
        renamed.goal = "Ship the gadget"
        assert plan_fingerprint(renamed) != plan_fingerprint(base)

        re_estimated = _golden_plan()
        re_estimated.tasks[0].est_agent_minutes = 11
        assert plan_fingerprint(re_estimated) != plan_fingerprint(base)

        versioned = _golden_plan()
        versioned.version = 2
        assert plan_fingerprint(versioned) != plan_fingerprint(base)


class TestOwnedPathResolution:
    """P0.1 — a syntactically valid but wrongly-scoped owned path must bind to
    the real package, not silently produce a no-op branch.

    Today's regression: a task declared `swarm/` while the package is
    `omniagentos/swarm/`; every edit was then judged out-of-scope, reverted and
    committed as reverted, and the suite passed against the unchanged tree.
    """

    def test_short_package_name_auto_qualifies(self, tmp_path) -> None:
        from omniagentos.swarm.planner import resolve_owned_path

        (tmp_path / "omniagentos" / "swarm").mkdir(parents=True)
        resolved, note = resolve_owned_path("swarm", tmp_path)
        assert resolved == "omniagentos/swarm"
        assert note and "auto-qualified" in note

    def test_ambiguous_short_name_is_rejected(self, tmp_path) -> None:
        import pytest as _pytest

        from omniagentos.swarm.planner import SwarmPlanError, resolve_owned_path

        (tmp_path / "omniagentos" / "swarm").mkdir(parents=True)
        (tmp_path / "vendor" / "swarm").mkdir(parents=True)
        with _pytest.raises(SwarmPlanError, match="ambiguous"):
            resolve_owned_path("swarm", tmp_path)

    def test_existing_path_is_unchanged(self, tmp_path) -> None:
        from omniagentos.swarm.planner import resolve_owned_path

        (tmp_path / "omniagentos" / "swarm").mkdir(parents=True)
        assert resolve_owned_path("omniagentos/swarm", tmp_path) == ("omniagentos/swarm", None)

    def test_new_file_under_existing_dir_is_allowed(self, tmp_path) -> None:
        from omniagentos.swarm.planner import resolve_owned_path

        (tmp_path / "omniagentos").mkdir()
        assert resolve_owned_path("omniagentos/brand_new.py", tmp_path) == (
            "omniagentos/brand_new.py",
            None,
        )

    def test_wholly_new_tree_is_allowed(self, tmp_path) -> None:
        from omniagentos.swarm.planner import resolve_owned_path

        assert resolve_owned_path("src/a", tmp_path) == ("src/a", None)

    def test_vendor_dirs_never_win_the_match(self, tmp_path) -> None:
        from omniagentos.swarm.planner import resolve_owned_path

        (tmp_path / "node_modules" / "swarm").mkdir(parents=True)
        (tmp_path / "omniagentos" / "swarm").mkdir(parents=True)
        resolved, _ = resolve_owned_path("swarm", tmp_path)
        assert resolved == "omniagentos/swarm"


class TestPytestConfigSurfaceDenylist:
    """P0.4 / v4-Q2 — pytest reads config from more than conftest.py and
    pytest.ini; a worker owning any of them can neuter its own judge."""

    def test_all_pytest_config_surfaces_are_shared(self) -> None:
        from omniagentos.swarm.planner import is_shared_path

        for name in ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "setup.py"):
            assert is_shared_path(f"omniagentos/{name}") is True, name

    def test_pyproject_is_shared(self) -> None:
        from omniagentos.swarm.planner import is_shared_path

        assert is_shared_path("pyproject.toml") is True

    def test_ordinary_module_is_not_shared(self) -> None:
        from omniagentos.swarm.planner import is_shared_path

        assert is_shared_path("omniagentos/swarm/spawn.py") is False


class TestShadowedModuleDetection:
    """P0.3 — an edit to a module shadowed by a same-named package has zero
    production effect; the mechanical gate must catch it."""

    def test_shadowed_flat_module_is_rejected(self, tmp_path, monkeypatch) -> None:
        import sys

        from omniagentos.swarm.scheduler import assert_touched_modules_importable

        pkg = tmp_path / "shadowpkg"
        (pkg / "campaign").mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "campaign" / "__init__.py").write_text("X = 'package'", encoding="utf-8")
        (pkg / "campaign.py").write_text("X = 'flat'", encoding="utf-8")

        monkeypatch.syspath_prepend(str(tmp_path))
        for name in [k for k in sys.modules if k.startswith("shadowpkg")]:
            del sys.modules[name]

        ok, detail = assert_touched_modules_importable(
            str(tmp_path), ["shadowpkg/campaign.py"]
        )
        assert ok is False
        assert "shadowed" in detail

    def test_unshadowed_module_passes(self, tmp_path, monkeypatch) -> None:
        import sys

        from omniagentos.swarm.scheduler import assert_touched_modules_importable

        pkg = tmp_path / "cleanpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text("X = 1", encoding="utf-8")

        monkeypatch.syspath_prepend(str(tmp_path))
        for name in [k for k in sys.modules if k.startswith("cleanpkg")]:
            del sys.modules[name]

        assert assert_touched_modules_importable(str(tmp_path), ["cleanpkg/mod.py"]) == (True, "")

    def test_non_python_and_scripts_are_ignored(self, tmp_path) -> None:
        from omniagentos.swarm.scheduler import assert_touched_modules_importable

        assert assert_touched_modules_importable(
            str(tmp_path), ["README.md", "configs/x.yaml"]
        ) == (True, "")


class TestDisjointOwnedPathContainment:
    """A parent and its child are NOT disjoint — set equality said they were,
    which would call a genuinely overlapping pair safe to run concurrently."""

    def test_parent_and_child_paths_are_not_disjoint(self) -> None:
        from omniagentos.swarm.planner import _check_disjoint_owned_paths

        tasks = [
            {"id": "a", "owned_paths": ["src"], "depends_on": []},
            {"id": "b", "owned_paths": ["src/a.py"], "depends_on": []},
        ]
        assert _check_disjoint_owned_paths(tasks) is False

    def test_genuinely_disjoint_paths_pass(self) -> None:
        from omniagentos.swarm.planner import _check_disjoint_owned_paths

        tasks = [
            {"id": "a", "owned_paths": ["src/a.py"], "depends_on": []},
            {"id": "b", "owned_paths": ["src/b.py"], "depends_on": []},
        ]
        assert _check_disjoint_owned_paths(tasks) is True
