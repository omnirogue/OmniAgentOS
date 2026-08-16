"""WP5a SwarmScheduler behavior suite: DAG execution, critical-path ordering,
mechanical retry / escalation / retry cap, ownership enforcement, budget cap,
git-checkout refusal, worker brief. Race-condition drills live in
``test_scheduler_races.py``."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.swarm.scheduler_fakes import (
    FakeGit,
    make_harness,
    make_scheduler,
    wait_until,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch in these tests must go through the harness fixtures."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


class TestDagExecution:
    def test_diamond_executes_in_dep_order_with_bounded_width(self, tmp_path: Path) -> None:
        """6-task diamond: a → (b,c,d) → e → integration. Dep order holds and
        the concurrent width never exceeds target_n (run cap 2)."""
        gate = threading.Event()
        h = make_harness(
            tmp_path,
            [
                {"id": "a"},
                {"id": "b", "depends_on": ["a"]},
                {"id": "c", "depends_on": ["a"]},
                {"id": "d", "depends_on": ["a"]},
                {"id": "e", "depends_on": ["b", "c", "d"]},
            ],
            max_concurrency=2,
            target_n=2,
        )
        for key in ("b", "c", "d"):
            h.world.set_behavior(key, {"kind": "gated", "gate": gate, "polls": 1})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None

            # Two of b/c/d must run CONCURRENTLY (width == target_n == 2) …
            assert wait_until(lambda: h.world.active == 2, timeout=10)
            assert h.world.high_water == 2
            gate.set()
            assert handle.join(timeout=20)

            # … and never more than 2 at once.
            assert h.world.high_water <= 2

            order = h.world.spawn_order
            assert order[0] == "a"
            assert set(order[1:4]) == {"b", "c", "d"}
            assert order[4] == "e"
            assert order[5] == "integration"

            for key in ("a", "b", "c", "d", "e", "integration"):
                assert h.status_of(key) == "done"
                assert len(h.attempts_of(key)) == 1
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "completed"
            assert run["heartbeat_at"] is not None

            actions = h.emitter.actions()
            for expected in (
                "run_started",
                "slot_opened",
                "task_assigned",
                "review_confirmed",
                "task_completed",
                "merge_started",
                "run_completed",
            ):
                assert expected in actions, expected
            assert h.emitter.of("run_completed")[0]["partial"] is False

            plan_md = (h.workdir / "PLAN.md").read_text(encoding="utf-8")
            assert "done" in plan_md
        finally:
            h.close()

    def test_critical_path_ordering(self, tmp_path: Path) -> None:
        """With one slot, the task heading the longest chain to integration is
        pulled first even though a short leaf was provisioned earlier."""
        h = make_harness(
            tmp_path,
            [
                {"id": "leaf", "est": 5},
                {"id": "chain", "est": 5},
                {"id": "chain2", "depends_on": ["chain"], "est": 50},
            ],
            max_concurrency=1,
            target_n=1,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert h.world.spawn_order == ["chain", "chain2", "leaf", "integration"]
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_dangling_integration_dependency_fails_explicitly(self, tmp_path: Path) -> None:
        """A missing dependency row is UNKNOWN, never runnable or an idle wait."""
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            max_concurrency=1,
        )
        integration_id = h.task_id("integration")
        h.dal._connection.execute(
            "INSERT INTO swarm_deps (swarm_run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
            (h.run_id, integration_id, "btk_dangling_dependency"),
        )
        h.dal._connection.commit()
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=10)

            run = h.dal.get_run(h.run_id)
            assert run is not None
            assert run["status"] == "failed"
            assert "unknown dependency" in str(run["error"]).lower()
            assert "btk_dangling_dependency" in str(run["error"])
            assert h.world.spawn_order == []
        finally:
            h.close()

    def test_unknown_dependency_ids_edges_first_avoids_split_race(self, tmp_path: Path) -> None:
        """Edge-first observation must not false-positive on a mid-read split.

        A split adds new task rows and rewires edges in one transaction while
        retaining the parent row. If the scheduler reads member rows first and
        edges second with a split between them, the post-split edge target is
        absent from the pre-split member map and is misreported as unknown.
        Edges-first observes a consistent before-or-after set. This test
        simulates the split on the first DAL read and binds the order.
        """
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            max_concurrency=1,
        )
        try:
            integration_id = h.task_id("integration")
            parent_id = h.task_id("a")
            # Post-split subtask id is not present until after the first read.
            subtask_id = "btk_split_child_subtask"

            flipped = {"n": 0}
            call_order: list[str] = []

            pre_edges = [
                {
                    "swarm_run_id": h.run_id,
                    "task_id": integration_id,
                    "depends_on_task_id": parent_id,
                }
            ]
            post_edges = [
                {
                    "swarm_run_id": h.run_id,
                    "task_id": integration_id,
                    "depends_on_task_id": subtask_id,
                }
            ]
            base_tasks = h.dal.tasks_for_run(h.run_id)
            post_tasks = list(base_tasks) + [
                {
                    "id": subtask_id,
                    "status": "open",
                    "swarm_json": json.dumps({"task_key": "split-child"}),
                }
            ]

            def deps_for_run(run_id: str):  # type: ignore[no-untyped-def]
                call_order.append("edges")
                result = post_edges if flipped["n"] else pre_edges
                flipped["n"] = 1  # split lands after the first observation
                del run_id
                return list(result)

            def tasks_for_run(run_id: str):  # type: ignore[no-untyped-def]
                call_order.append("rows")
                result = post_tasks if flipped["n"] else base_tasks
                flipped["n"] = 1
                del run_id
                return list(result)

            h.dal.deps_for_run = deps_for_run  # type: ignore[method-assign]
            h.dal.tasks_for_run = tasks_for_run  # type: ignore[method-assign]

            scheduler = make_scheduler(h)
            unknown = scheduler._unknown_dependency_ids(h.run_id)
            assert call_order[:2] == ["edges", "rows"], call_order
            # Edges-first: pre-split edge targets parent, which is still present
            # in the post-split member map → no false unknown.
            assert unknown == ()

            # Named counterfeit order: rows then edges with the same flip must
            # false-positive. Bind the production order by showing the reverse
            # yields the defect the edge-first safeguard exists to prevent.
            flipped["n"] = 0
            call_order.clear()

            def unknown_rows_first(run_id: str) -> tuple[str, ...]:
                # Counterfeit of the production body with rows before edges.
                status_by_id = {
                    str(task["id"]): str(task["status"]) for task in scheduler._member_tasks(run_id)
                }
                edges = h.dal.deps_for_run(run_id)
                dependency_ids = {
                    str(edge["depends_on_task_id"])
                    for edge in edges
                    if str(edge["task_id"]) in status_by_id
                }
                statuses = {
                    dependency_id: status_by_id.get(dependency_id, "unknown")
                    for dependency_id in dependency_ids
                }
                return tuple(
                    sorted(
                        dependency_id
                        for dependency_id, status in statuses.items()
                        if status == "unknown"
                    )
                )

            counterfeit_unknown = unknown_rows_first(h.run_id)
            assert call_order[:2] == ["rows", "edges"], call_order
            assert counterfeit_unknown == (subtask_id,)
        finally:
            h.close()

    def test_integration_ready_refuses_unknown_dependency_status(self, tmp_path: Path) -> None:
        """Bind `_integration_ready`'s missing-dep default/guard directly.

        The run-level `_unknown_dependency_ids` detector is covered elsewhere.
        This pins the separate readiness path: a dangling prerequisite must not
        default to ``"done"`` (or any terminal stand-in) and release integration
        when other deps are already terminal.
        """
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            max_concurrency=1,
        )
        try:
            a_id = h.task_id("a")
            integration_id = h.task_id("integration")
            # Mixed terminal + dangling: with the old get(dep, "done") default
            # and no unknown guard, statuses become [blocked, done] → all
            # terminal → integration incorrectly READY.
            h.collab.update_board_task(a_id, {"status": "blocked"})
            h.dal._connection.execute(
                "INSERT INTO swarm_deps (swarm_run_id, task_id, depends_on_task_id) "
                "VALUES (?, ?, ?)",
                (h.run_id, integration_id, "btk_dangling_integration_ready"),
            )
            h.dal._connection.commit()

            scheduler = make_scheduler(h)
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            run = h.dal.get_run(h.run_id)
            assert run is not None
            assert scheduler._integration_ready(state, run) is False
        finally:
            h.close()


class TestQualityGate:
    def test_mechanical_failure_gets_one_same_tier_retry(self, tmp_path: Path) -> None:
        """A failing verify_command gets ONE same-tier retry with the error fed
        back before any escalation — and consumes NO retry."""
        h = make_harness(tmp_path, [{"id": "m", "complexity": "simple"}], max_concurrency=1)
        calls: list[str] = []

        def verifier(task, swarm_json, working_dir):
            del task, working_dir
            if str(swarm_json.get("task_key")) != "m":
                return True, ""
            calls.append("m")
            if len(calls) == 1:
                return False, "SyntaxError: invalid syntax at line 3"
            return True, "ok"

        try:
            scheduler = make_scheduler(h, verifier=verifier)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            attempts = h.attempts_of("m")
            assert [a["end_reason"] for a in attempts] == ["review_denied", "completed"]
            assert attempts[0]["tier"] == attempts[1]["tier"] == "simple"  # same tier
            swarm_json = h.swarm_json_of("m")
            assert swarm_json.get("mechanical_retry_used") is True
            assert int(swarm_json.get("retries") or 0) == 0  # no retry consumed
            assert any(
                "SyntaxError" in str(entry.get("text"))
                for entry in swarm_json.get("feedback") or []
            )
            assert h.status_of("m") == "done"
            assert h.emitter.of("run_completed")[0]["partial"] is False
        finally:
            h.close()

    def test_retry_cap_blocks_and_propagates_integration_exempt(self, tmp_path: Path) -> None:
        """Persistent mechanical failure: one same-tier retry, then escalations
        consume the 2-retry cap → blocked; dependents block transitively; the
        integration task is exempt and runs over completed work; the summary is
        marked partial."""
        h = make_harness(
            tmp_path,
            [
                {"id": "bad", "complexity": "simple"},
                {"id": "child", "depends_on": ["bad"]},
                {"id": "good"},
            ],
            max_concurrency=1,
        )

        def verifier(task, swarm_json, working_dir):
            del task, working_dir
            if str(swarm_json.get("task_key")) == "bad":
                return False, "tests keep failing"
            return True, ""

        try:
            scheduler = make_scheduler(h, verifier=verifier)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            attempts = h.attempts_of("bad")
            # attempt 1 simple (mechanical retry, free), 2 simple → retry 1
            # escalates, 3 standard → retry 2 escalates, 4 complex → retry 3
            # exceeds the cap → blocked.
            assert [a["tier"] for a in attempts] == ["simple", "simple", "standard", "complex"]
            assert int(h.swarm_json_of("bad").get("retries") or 0) == 3
            assert h.status_of("bad") == "blocked"
            assert h.status_of("child") == "blocked"  # transitive propagation
            assert h.status_of("good") == "done"
            assert h.status_of("integration") == "done"  # exempt, ran over completed work
            assert h.dal.get_run(h.run_id)["status"] == "completed"
            assert h.emitter.of("run_completed")[0]["partial"] is True
            blocked_events = h.emitter.of("task_blocked")
            assert any(e.get("reason") == "retry_cap" for e in blocked_events)
            assert any("dependency" in str(e.get("reason")) for e in blocked_events)
        finally:
            h.close()

    def test_auth_failed_attempts_are_bounded_not_a_relay_loop(self, tmp_path: Path) -> None:
        """``auth_failed`` joined RELAY_END_REASONS, so every successor of an
        auth failure now inherits a continuation prompt. That is only safe if
        the number of successors is BOUNDED — otherwise a credential that stays
        broken (or an account rotation that keeps re-firing) would relay
        forever.

        It is bounded, and by a path that predates the relay: ``auth_failed``
        routes to ``_mechanical_failure`` (ONE free same-tier retry) and then to
        ``_consume_retry`` (the 2-retry cap with escalation), NOT to
        ``_handle_rate_limited``'s no-retry-consumed re-enqueue. Four attempts,
        then blocked. The relay changes what a successor is TOLD, never how
        many successors there are.
        """
        h = make_harness(tmp_path, [{"id": "auth", "complexity": "simple"}], max_concurrency=1)
        h.world.set_behavior("auth", {"kind": "fail", "error": "invalid api key"})
        try:
            scheduler = make_scheduler(h, classifier=lambda session: "auth_failed")
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            attempts = h.attempts_of("auth")
            assert [a["end_reason"] for a in attempts] == ["auth_failed"] * 4
            assert [a["tier"] for a in attempts] == ["simple", "simple", "standard", "complex"]
            assert int(h.swarm_json_of("auth").get("retries") or 0) == 3
            assert h.status_of("auth") == "blocked"
            assert any(e.get("reason") == "retry_cap" for e in h.emitter.of("task_blocked"))
        finally:
            h.close()

    def test_llm_deny_escalates_immediately_with_feedback(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "r", "complexity": "simple"}], max_concurrency=1)
        h.reviewer.set_script("r", "deny", "confirm")
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            attempts = h.attempts_of("r")
            assert [a["end_reason"] for a in attempts] == ["review_denied", "completed"]
            assert [a["tier"] for a in attempts] == ["simple", "standard"]  # immediate escalation
            assert int(h.swarm_json_of("r").get("retries") or 0) == 1
            assert h.emitter.of("review_denied")
            # The denial feedback is in the cascade trace AND the next brief.
            assert any(
                "scripted deny" in str(entry.get("text"))
                for entry in h.swarm_json_of("r").get("feedback") or []
            )
            second_brief = h.world.spawn_requests[1].prompt
            assert "scripted deny" in second_brief
            assert h.status_of("r") == "done"
        finally:
            h.close()


class TestOwnershipEnforcement:
    @staticmethod
    def _commit_fixture(repo: Path, *paths: str) -> None:
        subprocess.run(
            ("git", "-C", str(repo), "add", "--", *paths),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "fixture baseline",
            ),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_violation_reverts_from_snapshot_and_flags_review(self, tmp_path: Path) -> None:
        """Real git: out-of-scope changes are reverted from the pre-attempt
        snapshot and the review is flagged; the confirm commit is pathspec
        limited to the owned paths."""
        h = make_harness(
            tmp_path,
            [{"id": "vio", "owned_paths": ["src/vio.txt"]}],
            max_concurrency=1,
            real_git=True,
        )
        outside = h.workdir / "outside.txt"
        outside.write_text("original", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(h.workdir), "add", "--", "outside.txt"),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(h.workdir),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "fixture baseline",
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        h.world.set_behavior(
            "vio",
            {
                "kind": "complete",
                "polls": 1,
                "output": "did work",
                "write_files": {"src/vio.txt": "mine", "outside.txt": "trespass"},
                "files": ["src/vio.txt", "outside.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            # Reverted from the snapshot; owned work kept.
            assert outside.read_text(encoding="utf-8") == "original"
            assert (h.workdir / "src" / "vio.txt").read_text(encoding="utf-8") == "mine"

            vio_calls = [c for c in h.reviewer.calls if c["key"] == "vio"]
            assert vio_calls and any("ownership violation" in f for f in vio_calls[0]["flags"])
            assert h.status_of("vio") == "done"

            # The confirm commit for vio contains ONLY the owned path.
            log = subprocess.run(
                ("git", "-C", str(h.workdir), "log", "--name-only", "--pretty=%s"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            confirm_block = next(
                block for block in log.split("\n\n") if "task vio confirmed" in block
            )
            assert "src/vio.txt" in log
            assert "outside.txt" not in confirm_block
            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_fallback_revert_preserves_pre_attempt_operator_dirt(self, tmp_path: Path) -> None:
        """Fallback observation must not attribute ambient operator dirt to the worker."""
        h = make_harness(
            tmp_path,
            [{"id": "vio", "owned_paths": ["src/vio.txt"]}],
            integration=False,
            max_concurrency=1,
            real_git=True,
        )
        operator_file = h.workdir / "operator-notes.txt"
        operator_file.write_bytes(b"committed operator notes\n")
        self._commit_fixture(h.workdir, "operator-notes.txt")
        dirty_operator_bytes = b"operator work in progress \x00 stays byte-for-byte\n"
        operator_file.write_bytes(dirty_operator_bytes)

        worker_trespass = h.workdir / "worker-trespass.txt"
        h.world.set_behavior(
            "vio",
            {
                "kind": "complete",
                "polls": 1,
                "output": "did work",
                "write_files": {
                    "src/vio.txt": "owned work",
                    "worker-trespass.txt": "out of scope",
                },
                # No files report: exercise the changed_paths fallback.
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert operator_file.read_bytes() == dirty_operator_bytes
            assert not worker_trespass.exists()
            assert (h.workdir / "src" / "vio.txt").read_text(encoding="utf-8") == "owned work"
        finally:
            h.close()

    def test_fallback_revert_still_restores_path_clean_before_attempt(self, tmp_path: Path) -> None:
        """Ambient dirt must not disable enforcement for a worker-touched clean path."""
        h = make_harness(
            tmp_path,
            [{"id": "vio", "owned_paths": ["src/vio.txt"]}],
            integration=False,
            max_concurrency=1,
            real_git=True,
        )
        operator_file = h.workdir / "operator-notes.txt"
        clean_outside = h.workdir / "clean-outside.txt"
        operator_file.write_text("committed operator notes\n", encoding="utf-8")
        clean_outside.write_text("committed clean content\n", encoding="utf-8")
        self._commit_fixture(h.workdir, "operator-notes.txt", "clean-outside.txt")
        operator_file.write_text("operator work in progress\n", encoding="utf-8")

        h.world.set_behavior(
            "vio",
            {
                "kind": "complete",
                "polls": 1,
                "output": "did work",
                "write_files": {
                    "src/vio.txt": "owned work",
                    "clean-outside.txt": "worker trespass",
                },
                # No files report: exercise the changed_paths fallback.
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert clean_outside.read_text(encoding="utf-8") == "committed clean content\n"
            assert operator_file.read_text(encoding="utf-8") == "operator work in progress\n"
        finally:
            h.close()


class TestBudget:
    def test_budget_cap_stops_new_spawns_with_bounded_overshoot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Blocking is opt-in since 2026-07-24 (budgets are advisory by default,
        # so an overshoot never strands un-started tasks). This pins the escape
        # hatch: with enforcement ON, the bounded-overshoot contract is intact.
        monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
        # Distinct estimates make the critical-path ordering deterministic
        # (equal keys would tie-break on random card ids).
        h = make_harness(
            tmp_path,
            [
                {"id": "t1", "est": 40},
                {"id": "t2", "est": 30},
                {"id": "t3", "est": 20},
                {"id": "t4", "est": 10},
            ],
            max_concurrency=1,
            budget=1.0,
        )
        for key in ("t1", "t2", "t3", "t4"):
            h.world.set_behavior(key, {"kind": "complete", "polls": 1, "cost": 0.6})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            # t1 (0.6 < 1.0) and t2 (gate passed pre-breach) ran; the breach
            # became observable at t2's terminal → NOTHING else spawned.
            # Overshoot = 1 attempt on the single slot — exactly the bound.
            assert h.world.spawn_order == ["t1", "t2"]
            assert h.status_of("t1") == "done"
            assert h.status_of("t2") == "done"
            for key in ("t3", "t4", "integration"):
                assert h.status_of(key) == "blocked"
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "completed"
            assert float(run["cost_usd"]) == pytest.approx(1.2)
            payload = h.emitter.of("run_completed")[0]
            assert payload["partial"] is True
            assert payload["budget_exhausted"] is True
            assert any(
                e.get("reason") == "budget cap reached" for e in h.emitter.of("task_blocked")
            )
        finally:
            h.close()

    def test_budget_overshoot_is_advisory_by_default(self, tmp_path: Path, monkeypatch) -> None:
        """Default posture: the whole plan still runs, and NOTHING is blocked.

        the operator, 2026-07-24: budgets must not be hard blockers. The run goes over and
        is reported as such (``budget_overshot``), but every task still gets to
        execute instead of being marked blocked for a cost line.
        """
        monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
        h = make_harness(
            tmp_path,
            [
                {"id": "t1", "est": 40},
                {"id": "t2", "est": 30},
                {"id": "t3", "est": 20},
                {"id": "t4", "est": 10},
            ],
            max_concurrency=1,
            budget=1.0,
            # No integration task: this test is about whether the PLANNED work is
            # allowed to finish, not about the merge phase.
            integration=False,
        )
        for key in ("t1", "t2", "t3", "t4"):
            h.world.set_behavior(key, {"kind": "complete", "polls": 1, "cost": 0.6})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            for key in ("t1", "t2", "t3", "t4"):
                assert h.status_of(key) == "done", f"{key} was blocked by the budget"
            assert not h.emitter.of("task_blocked"), "a budget overshoot blocked tasks"
            payload = h.emitter.of("run_completed")[0]
            assert payload["budget_exhausted"] is False
            assert payload["budget_overshot"] is True, "the overshoot must still be reported"
        finally:
            h.close()


class TestStallGuard:
    def test_nothing_started_nothing_running_fails_with_diagnosis(self, tmp_path: Path) -> None:
        """Eligible work that no slot can ever start (claims never land) must
        fail the run loudly after stall_minutes — never a silent hang."""
        h = make_harness(tmp_path, [{"id": "stuck"}], max_concurrency=1, fake_clock=True)
        try:
            scheduler = make_scheduler(h)
            # Simulate "no slot could start anything" (e.g. claims lost forever).
            scheduler._try_claim = lambda state, index, worker_id: None  # type: ignore[method-assign]
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.emitter.of("run_started"), timeout=10)
            h.clock.advance(31 * 60)  # past the 30-minute stall window
            assert wait_until(
                lambda: (h.dal.get_run(h.run_id) or {}).get("status") == "failed", timeout=10
            )
            run = h.dal.get_run(h.run_id)
            assert "stalled" in str(run["error"])
            failed = h.emitter.of("run_failed")
            assert failed and "stalled" in failed[0]["diagnosis"]
            assert handle.join(timeout=10)
        finally:
            h.close()


class TestActivationFlag:
    def test_flag_default_off_and_routes_through_default_scheduler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from omniagentos.swarm import scheduler as scheduler_mod

        monkeypatch.delenv("OMNIAGENTOS_SWARM_EXECUTE", raising=False)
        assert scheduler_mod.swarm_execute_enabled() is False
        # Default OFF: a pure no-op — merge safety for POST /api/swarm.
        assert scheduler_mod.activate_run_if_enabled("swr_never") is False

        monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
        assert scheduler_mod.swarm_execute_enabled() is True
        calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_default_scheduler",
            lambda: SimpleNamespace(start_run=lambda run_id: calls.append(run_id)),
        )
        scheduler_mod.activate_run_if_enabled("swr_flagged")
        assert calls == ["swr_flagged"]


class TestConfirmCommitLockSafety:
    """M2b: the coordinator's CONFIRM-path ``commit_paths`` tolerates a stale
    ``index.lock`` with a bounded retry, and the raising-commit path can never
    destroy reviewer-CONFIRMED work (it survives on the task branch)."""

    def test_commit_paths_retries_through_transient_index_lock(self, tmp_path: Path) -> None:
        """The bounded ladder outlives a lock that clears mid-retry.

        The lock used to be cleared by a ``threading.Timer(0.15)`` racing a
        ~0.3s retry budget, so the margin was a single scheduling slot on the
        last attempt: under gate-concurrent load the unlink lands too late,
        ``commit_paths`` returns None, and a CORRECT retry ladder reads as a
        candidate defect on whatever innocent train is riding. The lock is now
        released by the ladder itself — a counting hook on ``_git`` unlinks it
        as the third ``add`` attempt begins — so this asserts the RETRY
        BEHAVIOUR (that it re-attempts and eventually stages) instead of the
        host's scheduling luck. No wall clock is involved: the sleep is zeroed
        and the barrier is the attempt count.
        """
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        (repo / "f.txt").write_text("work", encoding="utf-8")
        lock = repo / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")

        release_on_attempt = 3
        add_attempts: list[str] = []

        class FastRetryGit(SubprocessSwarmGit):
            _LOCK_RETRY_ATTEMPTS = 3
            # Zero: the barrier below is deterministic, so the ladder needs no
            # wall-clock window to be right (and the test costs no real time).
            _LOCK_RETRY_SLEEP = 0.0

            def _git(  # type: ignore[override]
                self,
                working_dir: str,
                *args: str,
                check: bool = True,
                env: Mapping[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                if args[:1] == ("add",):
                    add_attempts.append(" ".join(args))
                    if len(add_attempts) >= release_on_attempt:
                        lock.unlink(missing_ok=True)
                return super()._git(working_dir, *args, check=check, env=env)

        sha = FastRetryGit().commit_paths(str(repo), ["f.txt"], "locked then ok")
        # The first two attempts saw the lock and failed; the ladder retried.
        assert len(add_attempts) == release_on_attempt
        assert sha
        show = subprocess.run(
            ("git", "-C", str(repo), "show", "HEAD:f.txt"),
            capture_output=True,
            text=True,
            check=True,
        )
        assert show.stdout == "work"

    def test_commit_paths_fails_soft_on_permanent_lock(self, tmp_path: Path) -> None:
        """A lock that outlives the bounded budget makes staging fail — the
        commit returns None (nothing half-committed, nothing lost; the
        confirm-path clean-check salvages the work instead)."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        class FastRetryGit(SubprocessSwarmGit):
            _LOCK_RETRY_ATTEMPTS = 2
            _LOCK_RETRY_SLEEP = 0.01

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        (repo / "f.txt").write_text("work", encoding="utf-8")
        (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
        assert FastRetryGit().commit_paths(str(repo), ["f.txt"], "never lands") is None
        # The working-tree file is untouched — still salvageable.
        assert (repo / "f.txt").read_text(encoding="utf-8") == "work"

    def test_coordinator_git_ops_bypass_failing_hooks(self, tmp_path: Path) -> None:
        """m9: SubprocessSwarmGit snapshot/commit_paths carry
        ``-c core.hooksPath=`` like the worktrees module — a repo whose
        pre-commit hook fails must not break coordinator plumbing commits
        (and a worker-planted hook must never fire inside them)."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        canary = tmp_path / "hook-fired.txt"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {canary}\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        git = SubprocessSwarmGit()
        (repo / "PLAN.md").write_text("snapshot", encoding="utf-8")
        assert git.snapshot(str(repo), "snapshot past hook", ["PLAN.md"])
        (repo / "c.txt").write_text("confirm", encoding="utf-8")
        assert git.commit_paths(str(repo), ["c.txt"], "confirm past hook")
        # The hook never even ran (disabled, not merely tolerated).
        assert not canary.exists()

    def test_raising_commit_paths_never_destroys_confirmed_work(self, tmp_path: Path) -> None:
        """M2c end-to-end on REAL git worktrees: every coordinator commit
        inside the task worktree raises, yet the reviewer-confirmed file
        SURVIVES on the task branch (salvage-committed before any removal)
        and the run still terminalizes."""
        import os

        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from omniagentos.swarm.worktrees import SubprocessSwarmWorktrees

        h = make_harness(
            tmp_path,
            [{"id": "a", "owned_paths": ["src/a.txt"]}],
            max_concurrency=1,
            real_git=True,
        )
        h.world.set_behavior(
            "a",
            {
                "kind": "complete",
                "files": ["src/a.txt"],
                "write_files": {"src/a.txt": "confirmed work\n"},
            },
        )

        class WorktreeCommitRaises(SubprocessSwarmGit):
            def __init__(self, main_dir: str) -> None:
                self._main_dir = os.path.realpath(main_dir)

            def commit_paths(self, working_dir, paths, message):  # type: ignore[override]
                if os.path.realpath(str(working_dir)) != self._main_dir:
                    raise RuntimeError("simulated index.lock wedge in the worktree")
                return super().commit_paths(working_dir, paths, message)

        try:
            worktrees = SubprocessSwarmWorktrees(
                var_root=tmp_path / "var" / "swarm", dep_link_dirs=()
            )
            scheduler = make_scheduler(
                h,
                worktrees=worktrees,
                worktrees_enabled=True,
                git=WorktreeCommitRaises(str(h.workdir)),
            )
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            run = h.dal.get_run(h.run_id)
            assert run["status"] in ("completed", "failed")
            # THE fix: the confirmed work survives on the task branch.
            show = subprocess.run(
                (
                    "git",
                    "-C",
                    str(h.workdir),
                    "show",
                    f"refs/heads/swarm/{h.run_id}/a:src/a.txt",
                ),
                capture_output=True,
                text=True,
            )
            assert show.returncode == 0, show.stderr
            assert show.stdout == "confirmed work\n"
        finally:
            h.close()


class TestSnapshotScopedStaging:
    """Pre-attempt snapshots commit coordinator-owned files, never ambient dirt."""

    @staticmethod
    def _commit_fixture(repo: Path, *paths: str) -> None:
        subprocess.run(
            ("git", "-C", str(repo), "add", "--", *paths),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "fixture baseline",
            ),
            capture_output=True,
            text=True,
            check=True,
        )

    @staticmethod
    def _show_stat(repo: Path, sha: str) -> str:
        return subprocess.run(
            ("git", "-C", str(repo), "show", "--stat", "--format=", sha),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    @staticmethod
    def _show_names(repo: Path, sha: str) -> list[str]:
        return subprocess.run(
            ("git", "-C", str(repo), "show", "--name-only", "--format=", sha),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

    @staticmethod
    def _head(repo: Path) -> str:
        return subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_run_start_operator_plan_dirt_is_not_committed(self, tmp_path: Path) -> None:
        """A path outside this run's scope stays dirty and out of history."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        plan.write_text("initial plan\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")

        plan.write_text("operator's uncommitted plan\n", encoding="utf-8")
        git = SubprocessSwarmGit()
        # This run wrote no coordinator file, so its scope is empty.
        sha = git.snapshot(str(repo), "exclude run-start operator dirt", [])

        assert "PLAN.md" not in self._show_names(repo, sha)
        assert "PLAN.md" in git.changed_paths(str(repo))
        assert plan.read_text(encoding="utf-8") == "operator's uncommitted plan\n"

    def test_plan_written_after_clean_run_start_is_committed(self, tmp_path: Path) -> None:
        """A coordinator write inside this run's scope does reach the base."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        plan.write_text("initial plan\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")

        git = SubprocessSwarmGit()
        plan.write_text("this run's coordinator write\n", encoding="utf-8")

        sha = git.snapshot(str(repo), "commit this run's coordinator write", ["PLAN.md"])

        assert self._show_names(repo, sha) == ["PLAN.md"]
        assert "PLAN.md" not in git.changed_paths(str(repo))

    def test_all_coordinator_files_excluded_still_creates_branch_base_commit(
        self, tmp_path: Path
    ) -> None:
        """An empty scope still produces the base commit worktrees fork from."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        plan.write_text("initial plan\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")
        plan.write_text("operator's uncommitted plan\n", encoding="utf-8")
        before = self._head(repo)

        sha = SubprocessSwarmGit().snapshot(
            str(repo),
            "empty branch base with coordinator files excluded",
            [],
        )

        assert sha
        assert sha == self._head(repo)
        assert sha != before

    def test_coordinator_dispatch_hands_off_run_scope(self, tmp_path: Path) -> None:
        """Production path: _execute_task must hand the run scope to snapshot().

        Dropping ``sorted(coordinator_delta)`` from the ``snapshot()`` call
        must fail this test.
        """
        h = make_harness(
            tmp_path,
            [{"id": "t"}],
            max_concurrency=1,
            integration=False,
        )
        assert isinstance(h.git, FakeGit)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert h.git.snapshots, "dispatch must take a pre-attempt snapshot"
            assert h.git.snapshot_paths, "snapshot handoff must be recorded"
            # The provisioned PLAN.md is this run's own output, so it is in scope.
            assert h.git.snapshot_paths[0] == ["PLAN.md"]
        finally:
            h.close()

    def test_operator_plan_dirt_at_launch_is_never_in_dispatch_scope(self, tmp_path: Path) -> None:
        """Coordinator level: operator PLAN.md content is out of scope entirely.

        The FakeGit variant of the real-git binder below — it pins the same
        contract on the cheap path so a scope regression is caught even when
        the real-git test is deselected.
        """
        h = make_harness(
            tmp_path,
            [{"id": "t"}],
            max_concurrency=1,
            integration=False,
        )
        assert isinstance(h.git, FakeGit)
        # Overwrite the provisioned projection with operator content BEFORE
        # launch: it no longer matches anything this run generated.
        (h.workdir / "PLAN.md").write_text("operator draft\n", encoding="utf-8")
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert h.git.snapshots, "dispatch must take a pre-attempt snapshot"
            assert h.git.snapshot_paths[0] == []
        finally:
            h.close()

    def test_snapshot_commits_plan_without_dirty_tracked_sensitive_file(
        self, tmp_path: Path
    ) -> None:
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        sensitive = repo / "configs" / "accounts.yaml"
        sensitive.parent.mkdir()
        plan.write_text("initial plan\n", encoding="utf-8")
        sensitive.write_text("fixture baseline\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md", "configs/accounts.yaml")

        plan.write_text("coordinator update\n", encoding="utf-8")
        sensitive.write_text("fixture dirty value\n", encoding="utf-8")

        sha = SubprocessSwarmGit().snapshot(str(repo), "scoped snapshot", ["PLAN.md"])

        stat = self._show_stat(repo, sha)
        assert "PLAN.md" in stat
        assert "configs/accounts.yaml" not in stat

    def test_run_snapshot_commits_exact_provisioned_plan(self, tmp_path: Path) -> None:
        """The generated PLAN remains in the branch base for fresh worktrees."""
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            integration=False,
            max_concurrency=1,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.git.snapshot_paths
            assert h.git.snapshot_paths[0] == ["PLAN.md"]
        finally:
            h.close()

    def test_snapshot_excludes_differently_named_sensitive_file(self, tmp_path: Path) -> None:
        """A filename denylist cannot satisfy the positive-scope contract."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        sensitive = repo / "operator-data" / "novel-credential-material.txt"
        sensitive.parent.mkdir()
        plan.write_text("initial plan\n", encoding="utf-8")
        sensitive.write_text("fixture baseline\n", encoding="utf-8")
        self._commit_fixture(
            repo,
            "PLAN.md",
            "operator-data/novel-credential-material.txt",
        )

        plan.write_text("next coordinator update\n", encoding="utf-8")
        sensitive.write_text("fixture dirty value\n", encoding="utf-8")
        subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "add",
                "--",
                "operator-data/novel-credential-material.txt",
            ),
            capture_output=True,
            text=True,
            check=True,
        )

        sha = SubprocessSwarmGit().snapshot(str(repo), "scoped snapshot", ["PLAN.md"])

        stat = self._show_stat(repo, sha)
        assert "PLAN.md" in stat
        assert "operator-data/novel-credential-material.txt" not in stat

    def test_snapshot_creates_empty_branch_base_without_committing_ambient_index(
        self, tmp_path: Path
    ) -> None:
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        sensitive = repo / "operator-state.txt"
        sensitive.write_text("fixture baseline\n", encoding="utf-8")
        self._commit_fixture(repo, "operator-state.txt")
        before = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        sensitive.write_text("fixture dirty value\n", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(repo), "add", "--", "operator-state.txt"),
            capture_output=True,
            text=True,
            check=True,
        )

        sha = SubprocessSwarmGit().snapshot(str(repo), "empty scoped snapshot", [])

        assert sha != before
        assert self._show_stat(repo, sha) == ""
        staged = subprocess.run(
            ("git", "-C", str(repo), "diff", "--cached", "--name-only"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert staged == ["operator-state.txt"]

    def test_run_snapshot_preserves_dirty_operator_plan_and_still_creates_base(
        self, tmp_path: Path
    ) -> None:
        """Operator PLAN.md dirt stays uncommitted; the empty branch base remains."""
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            integration=False,
            max_concurrency=1,
            real_git=True,
        )
        plan = h.workdir / "PLAN.md"
        self._commit_fixture(h.workdir, "PLAN.md")
        before = subprocess.run(
            ("git", "-C", str(h.workdir), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        operator_text = "operator private PLAN draft must never enter history\n"
        plan.write_text(operator_text, encoding="utf-8")

        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            snapshot_sha = str(h.swarm_json_of("a")["snapshot_sha"])
            assert snapshot_sha != before
            parent = subprocess.run(
                ("git", "-C", str(h.workdir), "rev-parse", f"{snapshot_sha}^"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert parent == before
            snapshot_paths = subprocess.run(
                (
                    "git",
                    "-C",
                    str(h.workdir),
                    "show",
                    "--format=",
                    "--name-only",
                    snapshot_sha,
                ),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            assert snapshot_paths == []
            history_matches = subprocess.run(
                (
                    "git",
                    "-C",
                    str(h.workdir),
                    "log",
                    "--format=%H",
                    "-S",
                    operator_text.strip(),
                    "--",
                    "PLAN.md",
                ),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            assert history_matches == []
        finally:
            h.close()

    def test_snapshot_binds_approved_digest_not_later_working_tree_bytes(
        self, tmp_path: Path
    ) -> None:
        """TOCTOU: operator rewrite after digest approval must not be the blob.

        Mirrors the reviewer probe: eligibility selected PLAN.md for the
        approved digest, then the working tree was rewritten before staging.
        The snapshot may only materialize the approved bytes (or skip the path).
        """
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        ambient = repo / "ambient-staged.txt"
        approved = "run-written PLAN content this run produced\n"
        operator_edit = "operator edit after digest check\n"
        plan.write_text(approved, encoding="utf-8")
        ambient.write_text("ambient baseline\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md", "ambient-staged.txt")

        # Ambient staged dirt must survive digest-bound snapshot (temp index).
        ambient.write_text("ambient dirty staged\n", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(repo), "add", "--", "ambient-staged.txt"),
            capture_output=True,
            text=True,
            check=True,
        )

        # Simulate: eligibility saw ``approved``; operator rewrote before stage.
        approved_digest = hashlib.sha256(approved.encode("utf-8")).hexdigest()
        plan.write_text(operator_edit, encoding="utf-8")

        sha = SubprocessSwarmGit().snapshot(
            str(repo),
            "digest-bound snapshot",
            ["PLAN.md"],
            expected_digests={"PLAN.md": approved_digest},
        )

        snapshot_paths = subprocess.run(
            ("git", "-C", str(repo), "show", "--format=", "--name-only", sha),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert snapshot_paths == []
        # Working-tree operator edit must remain uncommitted.
        assert plan.read_text(encoding="utf-8") == operator_edit
        history_matches = subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "log",
                "--format=%H",
                "-S",
                operator_edit.strip(),
                "--",
                "PLAN.md",
            ),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert history_matches == []
        staged = subprocess.run(
            ("git", "-C", str(repo), "diff", "--cached", "--name-only"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert staged == ["ambient-staged.txt"]

    def test_snapshot_commits_when_working_tree_still_matches_approved_digest(
        self, tmp_path: Path
    ) -> None:
        """Matching digest still lands PLAN.md in the branch-base commit."""
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        baseline = "baseline plan\n"
        updated = "run-written PLAN content this run produced\n"
        plan.write_text(baseline, encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")
        plan.write_text(updated, encoding="utf-8")
        digest = hashlib.sha256(updated.encode("utf-8")).hexdigest()

        sha = SubprocessSwarmGit().snapshot(
            str(repo),
            "digest-bound snapshot",
            ["PLAN.md"],
            expected_digests={"PLAN.md": digest},
        )

        snapshot_paths = subprocess.run(
            ("git", "-C", str(repo), "show", "--format=", "--name-only", sha),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert snapshot_paths == ["PLAN.md"]
        committed = subprocess.run(
            ("git", "-C", str(repo), "show", f"{sha}:PLAN.md"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert committed == updated

    def test_snapshot_commits_staged_digest_not_post_stage_working_tree(
        self, tmp_path: Path
    ) -> None:
        """``git commit --only`` re-reads the tree; digest bind must not use it.

        After approved bytes are staged, an operator rewrite of the pathname must
        not become the committed blob — the index-bound content wins.
        """
        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        class RaceAfterStageGit(SubprocessSwarmGit):
            def _stage_exact_content(
                self,
                working_dir: str,
                path: str,
                content: bytes,
                *,
                retry_lock: bool,
                env: object = None,
                mode: str | None = None,
            ) -> str | None:
                blob = super()._stage_exact_content(
                    working_dir,
                    path,
                    content,
                    retry_lock=retry_lock,
                    env=env,  # type: ignore[arg-type]
                    mode=mode,
                )
                Path(working_dir, path).write_text(
                    "operator edit after stage before commit\n",
                    encoding="utf-8",
                )
                return blob

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        approved = "run-written PLAN content this run produced\n"
        plan.write_text("baseline plan\n", encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")
        plan.write_text(approved, encoding="utf-8")
        digest = hashlib.sha256(approved.encode("utf-8")).hexdigest()

        sha = RaceAfterStageGit().snapshot(
            str(repo),
            "digest-bound after-stage race",
            ["PLAN.md"],
            expected_digests={"PLAN.md": digest},
        )

        committed = subprocess.run(
            ("git", "-C", str(repo), "show", f"{sha}:PLAN.md"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert committed == approved
        assert "operator edit after stage before commit" not in committed

    def test_run_snapshot_passes_approved_digests_to_git(self, tmp_path: Path) -> None:
        """Call site must bind digests, not pathnames alone (FakeGit records them)."""
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            integration=False,
            max_concurrency=1,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.git.snapshot_paths
            assert h.git.snapshot_paths[0] == ["PLAN.md"]
            assert h.git.snapshot_digests
            digests = h.git.snapshot_digests[0]
            assert digests is not None
            assert "PLAN.md" in digests
            assert len(digests["PLAN.md"]) == 64  # sha256 hex
            assert h.git.snapshot_modes
            modes = h.git.snapshot_modes[0]
            assert modes is not None
            assert modes.get("PLAN.md") in ("100644", "100755")
        finally:
            h.close()

    def test_snapshot_binds_approved_mode_not_live_working_tree_mode(self, tmp_path: Path) -> None:
        """Operator-only chmod must not enter the digest-bound snapshot.

        Reviewer residual: content-only provenance left mode sourced from live
        path bits via ``_file_mode``, so ``chmod +x`` with an unchanged digest
        still produced ``100644 => 100755`` in the snapshot. Digest-bound paths
        must resolve mode from approved provenance or HEAD — never live bits —
        including when ``expected_modes`` is omitted (HEAD fallback).
        """
        import os
        import stat

        from omniagentos.swarm.scheduler import SubprocessSwarmGit
        from tests.swarm.scheduler_fakes import init_git_repo

        def _assert_no_mode_only_commit(repo: Path, sha: str, before: str) -> None:
            tree_line = subprocess.run(
                ("git", "-C", str(repo), "ls-tree", sha, "--", "PLAN.md"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert tree_line.startswith("100644 "), tree_line
            assert "100755" not in tree_line
            if sha != before:
                name_status = subprocess.run(
                    (
                        "git",
                        "-C",
                        str(repo),
                        "diff-tree",
                        "--no-commit-id",
                        "--name-status",
                        "-r",
                        sha,
                    ),
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert "PLAN.md" not in name_status
                raw_diff = subprocess.run(
                    ("git", "-C", str(repo), "show", "--raw", "--format=", sha),
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert "100644 100755" not in raw_diff
                assert "mode change" not in raw_diff.lower()

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        plan = repo / "PLAN.md"
        baseline = "baseline plan content\n"
        plan.write_text(baseline, encoding="utf-8")
        self._commit_fixture(repo, "PLAN.md")

        # Content still matches the approved digest; only mode changes.
        approved_digest = hashlib.sha256(baseline.encode("utf-8")).hexdigest()
        plan.chmod(plan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        assert os.access(plan, os.X_OK)

        before = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Residual probe: digests only — mode must fall back to HEAD, not live.
        sha_head_fallback = SubprocessSwarmGit().snapshot(
            str(repo),
            "mode-bound snapshot (HEAD fallback)",
            ["PLAN.md"],
            expected_digests={"PLAN.md": approved_digest},
        )
        _assert_no_mode_only_commit(repo, sha_head_fallback, before)

        # Explicit approved mode must also win over live path bits.
        plan.chmod(plan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        before2 = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        sha_approved = SubprocessSwarmGit().snapshot(
            str(repo),
            "mode-bound snapshot (explicit mode)",
            ["PLAN.md"],
            expected_digests={"PLAN.md": approved_digest},
            expected_modes={"PLAN.md": "100644"},
        )
        _assert_no_mode_only_commit(repo, sha_approved, before2)

        # Working-tree executable bit may remain; history must not absorb it.
        assert os.access(plan, os.X_OK)

        # Counterfeit: approved mode map poisoned to 100755 while content still
        # matches HEAD — stage path must pin HEAD mode, not the poisoned map.
        plan.chmod(plan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        before3 = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        sha_poisoned = SubprocessSwarmGit().snapshot(
            str(repo),
            "mode-bound snapshot (poisoned approved mode)",
            ["PLAN.md"],
            expected_digests={"PLAN.md": approved_digest},
            expected_modes={"PLAN.md": "100755"},
        )
        _assert_no_mode_only_commit(repo, sha_poisoned, before3)

    def test_run_snapshot_preserves_operator_mode_only_plan_dirt(self, tmp_path: Path) -> None:
        """Scheduler path: operator chmod after provision must not enter history.

        Seeded provisioned PLAN is content-eligible; live mode must not become
        the staged mode when content still matches HEAD (reviewer residual).
        """
        import stat

        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            integration=False,
            max_concurrency=1,
            real_git=True,
        )
        plan = h.workdir / "PLAN.md"
        assert plan.is_file()
        self._commit_fixture(h.workdir, "PLAN.md")
        before = subprocess.run(
            ("git", "-C", str(h.workdir), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        plan.chmod(plan.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            snapshot_sha = str(h.swarm_json_of("a")["snapshot_sha"])
            assert snapshot_sha != before
            raw_diff = subprocess.run(
                ("git", "-C", str(h.workdir), "show", "--raw", "--format=", snapshot_sha),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "100644 100755" not in raw_diff
            assert "mode change" not in raw_diff.lower()
            tree_line = subprocess.run(
                ("git", "-C", str(h.workdir), "ls-tree", snapshot_sha, "--", "PLAN.md"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert tree_line.startswith("100644 "), tree_line
            # PLAN.md may be rewritten later in the run (status projection);
            # the requirement is only that history never absorbed the mode-only
            # operator dirt from the pre-attempt snapshot.
        finally:
            h.close()

    def test_run_snapshot_excludes_operator_edit_between_delta_and_stage(
        self, tmp_path: Path
    ) -> None:
        """Scheduler-path TOCTOU (reviewer probe): edit PLAN.md after delta check.

        Wraps the real ``_pending_coordinator_delta`` so the operator rewrite
        lands after eligibility returns and before ``snapshot`` stages — the
        approved digest must still bind the committed blob (or the path must
        be omitted). Pathname-only staging fails this probe.
        """
        h = make_harness(
            tmp_path,
            [{"id": "a"}],
            integration=False,
            max_concurrency=1,
            real_git=True,
        )
        plan = h.workdir / "PLAN.md"
        assert plan.is_file()
        self._commit_fixture(h.workdir, "PLAN.md")
        operator_probe = "operator edit after digest check\n"
        before = subprocess.run(
            ("git", "-C", str(h.workdir), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        try:
            scheduler = make_scheduler(h, git=h.git)
            real_delta = scheduler._pending_coordinator_delta
            probe_hit = {"n": 0}

            def _delta_then_operator_edit(state):  # type: ignore[no-untyped-def]
                result = real_delta(state)
                if "PLAN.md" in result:
                    probe_hit["n"] += 1
                    plan.write_text(operator_probe, encoding="utf-8")
                return result

            scheduler._pending_coordinator_delta = _delta_then_operator_edit  # type: ignore[method-assign]
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            assert probe_hit["n"] >= 1, "probe did not fire on eligible PLAN.md"

            snapshot_sha = str(h.swarm_json_of("a")["snapshot_sha"])
            assert snapshot_sha != before
            snapshot_paths = subprocess.run(
                (
                    "git",
                    "-C",
                    str(h.workdir),
                    "show",
                    "--format=",
                    "--name-only",
                    snapshot_sha,
                ),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            # Skip path: operator rewrite after eligibility must not stage.
            # (Digest binding could also materialize approved bytes; either way
            # the operator probe text must not enter the commit.)
            if "PLAN.md" in snapshot_paths:
                committed = subprocess.run(
                    ("git", "-C", str(h.workdir), "show", f"{snapshot_sha}:PLAN.md"),
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert committed != operator_probe
            else:
                assert snapshot_paths == []
            history_matches = subprocess.run(
                (
                    "git",
                    "-C",
                    str(h.workdir),
                    "log",
                    "--format=%H",
                    "-S",
                    operator_probe.strip(),
                    "--",
                    "PLAN.md",
                ),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            assert history_matches == []
            # Working tree is free to change later (PLAN regeneration after
            # completion); the requirement is only that the operator probe
            # never entered git history via the snapshot.
        finally:
            h.close()


class TestWorkspaceRules:
    def test_non_git_workspace_is_refused(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "x"}], max_concurrency=1)
        h.git = FakeGit(checkout=False)
        try:
            scheduler = make_scheduler(h, git=h.git)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=10)
            run = h.dal.get_run(h.run_id)
            assert run["status"] == "failed"
            assert "git checkout" in str(run["error"])
            failed = h.emitter.of("run_failed")
            assert failed and "git checkout" in failed[0]["diagnosis"]
            assert h.world.spawn_order == []  # nothing ever spawned
        finally:
            h.close()

    def test_worker_brief_carries_plan_hash_and_git_prohibition(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "x", "owned_paths": ["src/x.py"]}], max_concurrency=1)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            brief = h.world.spawn_requests[0].prompt
            plan_hash = str(h.swarm_json_of("x")["plan_hash"])[:12]
            assert plan_hash in brief
            assert "NEVER run `git add`" in brief
            assert "src/x.py" in brief
            request = h.world.spawn_requests[0]
            assert request.idle_minutes > 0  # reaper alignment
            assert request.budget_usd_max is None  # no run budget set
        finally:
            h.close()

    def test_project_contract_enforce_stamps_migration_058_task_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
        h = make_harness(
            tmp_path,
            [
                {
                    "id": "content",
                    "title": "Write ad copy",
                    "description": "Create ad copy for the launch",
                }
            ],
            integration=False,
            max_concurrency=1,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            board_task = h.collab.get_board_task(h.task_id("content"))
            assert board_task is not None
            assert board_task["task_mode"] == "content"
        finally:
            h.close()

    def test_project_contract_off_leaves_task_mode_unstamped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", raising=False)
        h = make_harness(
            tmp_path,
            [{"id": "content", "title": "Write ad copy"}],
            integration=False,
            max_concurrency=1,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            board_task = h.collab.get_board_task(h.task_id("content"))
            assert board_task is not None
            assert board_task["task_mode"] is None
        finally:
            h.close()


    def test_worker_brief_resolves_archi_md_when_present(self, tmp_path: Path) -> None:
        """Brief should point to ARCHI.md (full architecture) when it exists,
        and also mention ARCHI.json as the machine-readable sibling."""
        h = make_harness(tmp_path, [{"id": "x", "owned_paths": ["src/x.py"]}], max_concurrency=1)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            brief = h.world.spawn_requests[0].prompt
            # Verify ARCHI.md path appears in the brief
            assert "ARCHI.md" in brief
            # Verify the machine-readable sibling is mentioned
            assert "ARCHI.json" in brief
            # Verify the stub is NOT used
            assert "ARCHITECTURE.md" not in brief
        finally:
            h.close()

    def test_worker_brief_falls_back_to_architecture_md_when_archi_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ARCHI.md is absent, the brief falls back to the ARCHITECTURE.md stub.

        The fallback is exercised by pointing the brief's document root at a
        checkout that genuinely lacks ARCHI.md. The previous spelling patched
        ``pathlib.Path.exists`` process-wide for the duration of a threaded
        scheduler run, so every ``.exists()`` in every thread — store, sqlite,
        worktree setup — went through the test's shim.
        """
        from omniagentos.swarm import scheduler as scheduler_mod

        fake_root = tmp_path / "checkout-without-archi"
        fake_root.mkdir()
        for name in ("AGENTS.md", "TESTING.md", "ARCHITECTURE.md", "DECISIONS.md"):
            (fake_root / name).write_text(f"# {name}\n", encoding="utf-8")
        assert not (fake_root / "ARCHI.md").exists()
        monkeypatch.setattr(scheduler_mod, "_house_document_root", lambda: fake_root)

        h = make_harness(tmp_path, [{"id": "x", "owned_paths": ["src/x.py"]}], max_concurrency=1)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            brief = h.world.spawn_requests[0].prompt
            assert "ARCHITECTURE.md" in brief
            # No ARCHI.md means no machine-readable sibling to advertise.
            assert "ARCHI.json" not in brief
        finally:
            h.close()

    def test_worker_brief_never_names_the_stub_while_archi_md_exists(
        self, tmp_path: Path
    ) -> None:
        """The counterfeit: pointing at the 19-line stub when the real document is there.

        This assertion used to sit inside ``if (h.workdir / "ARCHI.md").exists()``
        — a path in the harness's throwaway workdir that never contains ARCHI.md
        — so the body never ran and the test could not fail. The condition it
        actually cares about is a property of the CHECKOUT the brief points at,
        which this suite runs from, so it is asserted unconditionally.
        """
        assert (REPO_ROOT / "ARCHI.md").is_file(), (
            "this checkout has no ARCHI.md, so the counterfeit cannot be posed"
        )

        h = make_harness(tmp_path, [{"id": "x", "owned_paths": ["src/x.py"]}], max_concurrency=1)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)
            brief = h.world.spawn_requests[0].prompt
            assert "ARCHITECTURE.md" not in brief, (
                "the brief points at the ARCHITECTURE.md stub while ARCHI.md exists"
            )
            assert "ARCHI.md" in brief
        finally:
            h.close()


def test_swarm_reviewer_runs_in_task_workspace(tmp_path: Path) -> None:
    """The reviewer's AgentInput must carry the session's project_dir — without
    it the read-only codex reviewer cannot see the files it judges (live bug)."""
    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    captured = {}
    workspace = tmp_path / "ws-demo"
    workspace.mkdir()

    class _FakeAdapter:
        def run(self, agent_input):
            captured["working_dir"] = agent_input.working_dir

            class _Out:
                output_json = {"verdict": "confirm", "feedback": "ok"}

            return _Out()

    reviewer = CrossLineageSwarmReviewer(adapter=_FakeAdapter())
    outcome = reviewer.review(
        task={"id": "btk_x", "title": "t"},
        swarm_json={
            "implementer_model": "gpt-5.6-sol",
            "formation_reviewer": "opus",
        },
        session={"project_dir": str(workspace)},
        verify_output="",
        flags=[],
    )
    assert outcome.verdict == "confirm"
    assert captured["working_dir"] == str(workspace)


def test_swarm_reviewer_run_id_unique_per_invocation(tmp_path: Path) -> None:
    """Retried reviews must never overwrite an earlier transcript: the adapter
    run_id (which names the var/logs/<run_id>/ dir) must be unique across
    invocations for the SAME task (live forensics loss: seq-0/1 denial logs of
    swr_850835 were clobbered by the retry's identical run_id)."""
    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    run_ids: list[str] = []
    workspace = tmp_path / "ws"
    workspace.mkdir()

    class _FakeAdapter:
        def run(self, agent_input):
            run_ids.append(agent_input.run_id)

            class _Out:
                output_json = {"verdict": "confirm", "feedback": "ok"}

            return _Out()

    reviewer = CrossLineageSwarmReviewer(adapter=_FakeAdapter())
    for _ in range(2):
        reviewer.review(
            task={"id": "btk_same_task_id", "title": "t"},
            swarm_json={
                "implementer_model": "gpt-5.6-sol",
                "formation_reviewer": "opus",
            },
            session={"id": "ses_attempt1", "project_dir": str(workspace)},
            verify_output="",
            flags=[],
        )
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]
    for run_id in run_ids:
        assert run_id.startswith("swarm-review-btk_same_task_id"[:29])
        assert "ses_attempt1"[:12] in run_id


def test_swarm_reviewer_run_id_unique_across_process_restarts(tmp_path: Path) -> None:
    """F5 pin: the per-process invocation counter restarts at 1 after a
    crash-resume, so run_ids carry a short entropy component — two reviewer
    instances (simulating two coordinator processes) reviewing the SAME
    task/attempt must still produce distinct, filesystem-safe run_ids so the
    resumed process never overwrites the earlier process's transcript dir."""
    import re

    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    run_ids: list[str] = []
    workspace = tmp_path / "ws"
    workspace.mkdir()

    class _FakeAdapter:
        def run(self, agent_input):
            run_ids.append(agent_input.run_id)

            class _Out:
                output_json = {"verdict": "confirm", "feedback": "ok"}

            return _Out()

    for _ in range(2):  # fresh reviewer instance == fresh process counter
        CrossLineageSwarmReviewer(adapter=_FakeAdapter()).review(
            task={"id": "btk_same_task_id", "title": "t"},
            swarm_json={
                "implementer_model": "gpt-5.6-sol",
                "formation_reviewer": "opus",
            },
            session={"id": "ses_attempt1", "project_dir": str(workspace)},
            verify_output="",
            flags=[],
        )
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]
    for run_id in run_ids:
        # run_id names a var/logs/<run_id>/ dir — must stay filesystem-safe.
        assert re.fullmatch(r"[A-Za-z0-9_-]+", run_id)
        assert run_id.startswith("swarm-review-btk_same_task_id"[:29])


def test_swarm_reviewer_missing_workspace_is_infra_error_not_deny(
    tmp_path: Path,
) -> None:
    """A vanished workspace is an INFRASTRUCTURE failure: verdict='error'
    (reviewer retry / blocked-on-review), never a DENY that burns worker
    retries — and the adapter must not even be invoked."""
    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    calls: list[object] = []

    class _FakeAdapter:
        def run(self, agent_input):
            calls.append(agent_input)
            raise AssertionError("adapter must not run against a missing workspace")

    reviewer = CrossLineageSwarmReviewer(adapter=_FakeAdapter())
    outcome = reviewer.review(
        task={"id": "btk_x", "title": "t"},
        swarm_json={
            "implementer_model": "gpt-5.6-sol",
            "formation_reviewer": "opus",
        },
        session={"project_dir": str(tmp_path / "gone-workspace")},
        verify_output="",
        flags=[],
    )
    assert outcome.verdict == "error"
    assert "workspace missing" in outcome.feedback
    assert calls == []


class _CapturingReviewAdapter:
    """Adapter fake for CrossLineageSwarmReviewer: scripted results per call,
    capturing every AgentInput. A script entry of 'raise' raises (infra
    failure); 'confirm'/'deny' return that verdict."""

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.inputs: list[object] = []

    def run(self, agent_input):
        self.inputs.append(agent_input)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if step == "raise":
            raise RuntimeError("reviewer adapter infra down")

        class _Out:
            output_json = {"verdict": step, "feedback": f"scripted {step}"}

        return _Out()


def test_reviewer_infra_failure_retry_carries_identical_workspace(
    tmp_path: Path,
) -> None:
    """Reviewer infra failure retries the REVIEW only: the retried review must
    receive the byte-identical workspace (same working_dir from the same
    session row), consume zero worker retries, and get a DISTINCT run_id."""
    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    h = make_harness(tmp_path, [{"id": "rev"}], max_concurrency=1, integration=False)
    adapter = _CapturingReviewAdapter("raise", "confirm")
    orig_spawn = h.world.spawn

    def spawn_with_project_dir(request):
        session_id = orig_spawn(request)
        h.world.sessions[session_id]["project_dir"] = request.working_dir
        return session_id

    h.world.spawn = spawn_with_project_dir
    try:
        scheduler = make_scheduler(h, reviewer=CrossLineageSwarmReviewer(adapter=adapter))
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        # Reviewer called exactly twice (first infra failure, then confirm) …
        assert len(adapter.inputs) == 2
        first, second = adapter.inputs
        # … both against the byte-identical workspace …
        assert first.working_dir == second.working_dir == str(h.workdir)
        # … with distinct run_ids (no transcript overwrite) …
        assert first.run_id != second.run_id
        # … consuming ZERO worker retries, and the task completes.
        assert int(h.swarm_json_of("rev").get("retries") or 0) == 0
        assert h.status_of("rev") == "done"
        assert len(h.attempts_of("rev")) == 1
    finally:
        h.close()


def test_reviewer_double_infra_failure_blocks_not_denies(tmp_path: Path) -> None:
    """Through the REAL CrossLineageSwarmReviewer: two consecutive adapter infra
    failures close the attempt blocked (reason reviewer_infrastructure) — never
    review_denied, never a consumed retry."""
    from omniagentos.swarm.scheduler import CrossLineageSwarmReviewer

    h = make_harness(tmp_path, [{"id": "rev"}], max_concurrency=1, integration=False)
    adapter = _CapturingReviewAdapter("raise", "raise")
    orig_spawn = h.world.spawn

    def spawn_with_project_dir(request):
        session_id = orig_spawn(request)
        h.world.sessions[session_id]["project_dir"] = request.working_dir
        return session_id

    h.world.spawn = spawn_with_project_dir
    try:
        scheduler = make_scheduler(h, reviewer=CrossLineageSwarmReviewer(adapter=adapter))
        handle = scheduler.start_run(h.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert len(adapter.inputs) == 2
        assert h.status_of("rev") == "blocked"
        assert int(h.swarm_json_of("rev").get("retries") or 0) == 0
        attempts = h.attempts_of("rev")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "blocked"
        assert "reviewer infrastructure" in attempts[0]["detail"]
        blocked = h.emitter.of("task_blocked")
        assert any(e.get("reason") == "reviewer_infrastructure" for e in blocked)
        assert not h.emitter.of("review_denied")
    finally:
        h.close()


class TestUltraFableCap:
    """D10 UltraCode: ≤3 live claude attempts per ultra run, distinct
    accounts preferred, degrade-not-block."""

    def _scheduler_with_attempts(self, tmp_path, attempts):
        from tests.swarm.scheduler_fakes import make_harness, make_scheduler

        h = make_harness(tmp_path, [{"id": "a"}], integration=False)

        class StubDal:
            def __init__(self, inner):
                self._inner = inner

            def attempts_for_run(self, run_id):
                return attempts

            def __getattr__(self, name):
                return getattr(self._inner, name)

        scheduler = make_scheduler(h)
        scheduler._dal = StubDal(h.dal)
        return h, scheduler

    def _decision(self, account="acct_1", reservation="rsv_1"):
        from omniagentos.swarm.scheduler import RouteDecision

        return RouteDecision(
            provider="claude",
            model="fable",
            tier="complex",
            account_id=account,
            reservation_id=reservation,
        )

    def test_at_cap_releases_reservation_and_defers(self, tmp_path, monkeypatch):
        from omniagentos.routing import limit_state
        from omniagentos.swarm.scheduler import _RunState

        live = [
            {
                "board_task_id": f"btk_{i}",
                "provider": "claude",
                "end_reason": None,
                "account_id": f"acct_{i}",
            }
            for i in range(3)
        ]
        h, scheduler = self._scheduler_with_attempts(tmp_path, live)
        released = []
        monkeypatch.setattr(limit_state, "release_reservation", released.append)
        try:
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            result = scheduler._apply_ultra_fable_cap(state, "btk_new", self._decision())
            assert result is None  # requeue — cap reached
            assert released == ["rsv_1"]
        finally:
            h.close()

    def test_same_account_re_reserves_distinct(self, tmp_path, monkeypatch):
        from omniagentos.routing import limit_state
        from omniagentos.swarm.scheduler import _RunState

        live = [
            {
                "board_task_id": "btk_0",
                "provider": "claude",
                "end_reason": None,
                "account_id": "acct_1",
            },
        ]
        h, scheduler = self._scheduler_with_attempts(tmp_path, live)
        released = []
        monkeypatch.setattr(limit_state, "release_reservation", released.append)

        class FakeAccount:
            account_id = "acct_2"

        class FakeReservation:
            id = "rsv_distinct"
            account = FakeAccount()

        monkeypatch.setattr(
            limit_state,
            "reserve_distinct_accounts",
            lambda provider, n, exclude_account_ids: [FakeReservation()],
        )
        try:
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            result = scheduler._apply_ultra_fable_cap(
                state, "btk_new", self._decision(account="acct_1")
            )
            assert result is not None
            assert result.account_id == "acct_2"
            assert result.reservation_id == "rsv_distinct"
            assert released == ["rsv_1"]  # router's original reservation freed
        finally:
            h.close()

    def test_no_distinct_capacity_degrades_to_original(self, tmp_path, monkeypatch):
        from omniagentos.routing import limit_state
        from omniagentos.swarm.scheduler import _RunState

        live = [
            {
                "board_task_id": "btk_0",
                "provider": "claude",
                "end_reason": None,
                "account_id": "acct_1",
            },
        ]
        h, scheduler = self._scheduler_with_attempts(tmp_path, live)
        monkeypatch.setattr(limit_state, "reserve_distinct_accounts", lambda **kw: [])
        monkeypatch.setattr(
            limit_state,
            "reserve_distinct_accounts",
            lambda provider, n, exclude_account_ids: [],
        )
        try:
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            original = self._decision(account="acct_1")
            result = scheduler._apply_ultra_fable_cap(state, "btk_new", original)
            assert result is original  # degrade: never block
        finally:
            h.close()

    def test_distinct_account_route_passes_through(self, tmp_path):
        from omniagentos.swarm.scheduler import _RunState

        live = [
            {
                "board_task_id": "btk_0",
                "provider": "claude",
                "end_reason": None,
                "account_id": "acct_other",
            },
        ]
        h, scheduler = self._scheduler_with_attempts(tmp_path, live)
        try:
            state = _RunState(run_id=h.run_id, working_dir=str(h.workdir))
            original = self._decision(account="acct_1")
            result = scheduler._apply_ultra_fable_cap(state, "btk_new", original)
            assert result is original  # already distinct — untouched
        finally:
            h.close()
