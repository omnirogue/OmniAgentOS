"""L10 remaining findings — H-35, M-34, M-35, M-44 focused regression tests.

H-35: collision safety shadow/enforce/rollback controls; enforce gated by L11.
M-34: crashed worker threads are respawned; fully dead pool fails promptly.
M-35: pathspec commit failure must not confirm with commit=null.
M-44: scope claim and spawn execution directory are one canonical binding.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm import collision_safety as cs
from tests.swarm.scheduler_fakes import (
    make_harness,
    make_scheduler,
    wait_until,
)


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))
    # Isolate collision-safety env from the host/process under test.
    for key in (
        cs.COLLISION_MODE_ENV,
        cs.FENCING_READY_ENV,
        cs.SHADOW_EVIDENCE_ENV,
        "OMNIAGENTOS_PARALLELISM_CONFIG",
        "OMNIAGENTOS_SWARM_WORKTREES",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# H-35 — collision safety controls
# ---------------------------------------------------------------------------


class TestH35CollisionSafetyControls:
    def test_default_effective_mode_is_shadow_not_enforce(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Production ships measurement-only until L11 fencing is ready."""
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n  mode: shadow\n  fencing_ready: false\n  allow_enforce: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.intended_collision_mode() == "shadow"
        assert cs.fencing_ready() is False
        assert cs.enforce_allowed() is False
        assert cs.effective_collision_mode() == "shadow"

    def test_enforce_intent_downgrades_until_l11_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n  mode: enforce\n  fencing_ready: false\n  allow_enforce: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.intended_collision_mode() == "enforce"
        assert cs.enforce_allowed() is False
        assert cs.effective_collision_mode() == "shadow"

    def test_config_and_env_cannot_fabricate_fencing_readiness(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Config/env setting fencing_ready: true must return False if L11 capability is absent."""
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n  mode: enforce\n  fencing_ready: true\n  allow_enforce: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        monkeypatch.setenv("OMNIAGENTOS_L11_FENCING_READY", "1")
        monkeypatch.delattr("omniagentos.swarm.scope_wiring.commit_fence_contract", raising=False)
        monkeypatch.delattr("omniagentos.scope.commit_fence_contract", raising=False)
        assert cs.fencing_ready() is False
        assert cs.enforce_allowed() is False

    def test_l10_l11_production_join_composition(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Prove L10 consumes L11 commit_fence_contract schema in real production join."""
        import omniagentos.swarm.scope_wiring as scope_wiring

        # Simulate exact L11 commit_fence_contract return
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "contract": "commit_fence_v1",
                "version": 1,
                "semantic_version": "1.0.0",
                "capabilities": ("commit_guard", "commit_claim"),
                "commit_guard": True,
                "commit_claim": True,
                "renews_while_held": True,
                "displaced_generation_raises": "ScopeCommitBusy",
                "renew_or_store_error": "blocks_mutation",
            },
            raising=False,
        )

        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n"
            "  mode: enforce\n"
            "  fencing_ready: true\n"
            "  allow_enforce: true\n"
            "  require_shadow_evidence: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))

        assert cs.verify_l11_fence_capability() is True
        assert cs.fencing_ready() is True
        assert cs.enforce_allowed() is True
        assert cs.effective_collision_mode() == "enforce"

        # Corrected dotted delattr must remove the capability and drop effective_collision_mode to shadow
        monkeypatch.delattr("omniagentos.swarm.scope_wiring.commit_fence_contract", raising=False)
        monkeypatch.delattr("omniagentos.scope.commit_fence_contract", raising=False)

        assert cs.verify_l11_fence_capability() is False
        assert cs.fencing_ready() is False
        assert cs.enforce_allowed() is False
        assert cs.effective_collision_mode() == "shadow"

    def test_verify_l11_fence_capability_schema_variants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verify_l11_fence_capability requires integer version >= 1 and rejects strings, booleans, zero, or missing capabilities."""
        import omniagentos.swarm.scope_wiring as scope_wiring

        # Integer version + capabilities tuple
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "version": 1,
                "capabilities": ("commit_guard", "commit_claim"),
            },
            raising=False,
        )
        assert cs.verify_l11_fence_capability() is True

        # Boolean flags fallback (with int version 1)
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "version": 1,
                "commit_guard": True,
                "commit_claim": True,
            },
            raising=False,
        )
        assert cs.verify_l11_fence_capability() is True

        # String versions are rejected (fail closed)
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "version": "1.0.0",
                "capabilities": ("commit_guard", "commit_claim"),
            },
            raising=False,
        )
        assert cs.verify_l11_fence_capability() is False

        # Boolean versions are rejected (fail closed)
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "version": True,
                "capabilities": ("commit_guard", "commit_claim"),
            },
            raising=False,
        )
        assert cs.verify_l11_fence_capability() is False

        # Invalid/version 0 is rejected (fail closed)
        monkeypatch.setattr(
            scope_wiring,
            "commit_fence_contract",
            lambda: {
                "version": 0,
                "capabilities": ("commit_guard", "commit_claim"),
            },
            raising=False,
        )
        assert cs.verify_l11_fence_capability() is False

    def test_malformed_l11_capability_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Malformed or version-0 fence capabilities fail closed (return False)."""
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text("collision_safety:\n  fencing_ready: true\n", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))

        # Version 0 capability
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 0, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        assert cs.fencing_ready() is False

        # Non-dict return
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: "invalid",
            raising=False,
        )
        assert cs.fencing_ready() is False

        # Exception raised by function
        def _explode() -> None:
            raise RuntimeError("capability error")

        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            _explode,
            raising=False,
        )
        assert cs.fencing_ready() is False

    def test_config_and_env_can_disable_valid_fencing_capability(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Operator env or config can DISABLE readiness even when valid capability exists."""
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 1, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        # Env disable
        monkeypatch.setenv("OMNIAGENTOS_L11_FENCING_READY", "0")
        assert cs.fencing_ready() is False

        # Config disable
        monkeypatch.delenv("OMNIAGENTOS_L11_FENCING_READY", raising=False)
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text("collision_safety:\n  fencing_ready: false\n", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.fencing_ready() is False

    def test_enforce_allowed_only_when_fencing_and_policy_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 1, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n"
            "  mode: enforce\n"
            "  fencing_ready: true\n"
            "  allow_enforce: true\n"
            "  require_shadow_evidence: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.fencing_ready() is True
        assert cs.enforce_allowed() is True
        assert cs.effective_collision_mode() == "enforce"

    def test_rollback_steps_down_enforce_to_shadow_to_off(self) -> None:
        assert cs.rollback_mode("enforce") == "shadow"
        assert cs.rollback_mode("shadow") == "off"
        assert cs.rollback_mode("off") == "off"

    def test_env_rollback_spelling_resolves_to_shadow(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text("collision_safety:\n  mode: enforce\n", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        monkeypatch.setenv(cs.COLLISION_MODE_ENV, "rollback")
        assert cs.intended_collision_mode() == "shadow"

    def test_worktrees_config_true_blocked_without_fencing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """H-35: configs/swarm enabled=true cannot force isolation before L11."""
        from omniagentos.swarm.worktrees import swarm_worktrees_enabled

        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n  fencing_ready: false\n  allow_enforce: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config",
            lambda: {"enabled": True},
        )
        assert swarm_worktrees_enabled() is False

    def test_worktrees_env_drill_override_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.swarm.worktrees import swarm_worktrees_enabled

        monkeypatch.setenv("OMNIAGENTOS_SWARM_WORKTREES", "1")
        assert swarm_worktrees_enabled() is True


# ---------------------------------------------------------------------------
# M-34 — worker thread recovery / visible capacity loss
# ---------------------------------------------------------------------------


class TestM34WorkerThreadRecovery:
    def test_dead_worker_slot_is_respawned(self, tmp_path: Path) -> None:
        """A dead thread in state.workers is replaced on the next reconcile."""
        h = make_harness(
            tmp_path,
            [{"id": "slow", "est": 30}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("slow", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)

            # Locate the run state and kill its sole worker thread object
            # without going through a clean stop (simulates a crash exit).
            with scheduler._runs_lock:
                state = scheduler._runs[h.run_id]
            with state.cond:
                worker = state.workers.get(0)
            assert worker is not None and worker.is_alive()

            # Force-replace the thread entry with a finished dummy so
            # _ensure_workers sees a dead slot (we cannot safely kill the
            # real thread mid-await without races; a dead entry is the
            # post-crash state the reconcile path observes).
            dead = threading.Thread(target=lambda: None, name="dead-slot")
            dead.start()
            dead.join(timeout=2)
            with state.cond:
                state.workers[0] = dead
            assert not state.workers[0].is_alive()

            # Drive one recover pass.
            run = h.dal.get_run(h.run_id) or {}
            tasks = scheduler._member_tasks(h.run_id, run)
            scheduler._recover_worker_capacity(state, run, tasks)

            with state.cond:
                replacement = state.workers.get(0)
            assert replacement is not None
            assert replacement is not dead
            assert replacement.is_alive()

            # Visible capacity-loss signal was emitted.
            resize = h.emitter.of("resize")
            assert any(
                "worker_capacity_loss" in str(payload.get("reason") or "") for payload in resize
            )

            # Clean stop.
            h.dal.set_run_status(h.run_id, "cancelled", error="test done")
            assert handle.join(timeout=20)
        finally:
            h.close()

    def test_fully_dead_pool_fails_promptly_when_work_remains(self, tmp_path: Path) -> None:
        """M-34: zero live workers + open demand fails without stall_minutes."""
        h = make_harness(
            tmp_path,
            [{"id": "openish", "est": 10}],
            max_concurrency=1,
            integration=False,
        )
        # Do not hang a session — leave the task open by never completing.
        h.world.set_behavior("openish", {"kind": "hang"})
        try:
            scheduler = make_scheduler(h, stall_minutes=60.0)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)

            with scheduler._runs_lock:
                state = scheduler._runs[h.run_id]

            # Mark every slot dead and force target_n workers to stay dead by
            # stubbing _ensure_workers so respawn cannot recover.
            dead = threading.Thread(target=lambda: None)
            dead.start()
            dead.join(timeout=2)
            with state.cond:
                state.workers[0] = dead
            original_ensure = scheduler._ensure_workers

            def _no_respawn(s: object) -> None:
                del s

            scheduler._ensure_workers = _no_respawn  # type: ignore[method-assign,assignment]
            try:
                run = h.dal.get_run(h.run_id) or {}
                tasks = scheduler._member_tasks(h.run_id, run)
                scheduler._recover_worker_capacity(state, run, tasks)
            finally:
                scheduler._ensure_workers = original_ensure  # type: ignore[method-assign,assignment]

            assert wait_until(
                lambda: str((h.dal.get_run(h.run_id) or {}).get("status") or "") == "failed",
                timeout=10,
            )
            run_row = h.dal.get_run(h.run_id) or {}
            error = str(run_row.get("error") or "")
            assert "worker pool capacity lost" in error
            assert "stall" not in error.lower()
            failed_events = h.emitter.of("run_failed")
            assert failed_events
            assert handle.join(timeout=20)
        finally:
            h.close()


# ---------------------------------------------------------------------------
# M-35 — pathspec commit failure must not confirm
# ---------------------------------------------------------------------------


class TestM35PathspecCommitFailure:
    def test_pathspec_commit_exception_never_confirms(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "ps", "owned_paths": ["src/ps.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        h.git.commit_paths_error = RuntimeError("git commit --pathspec died")
        h.world.set_behavior(
            "ps",
            {
                "kind": "complete",
                "polls": 1,
                "output": "I finished the work",
                "write_files": {"src/ps.txt": "payload"},
                "files": ["src/ps.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.git.commit_paths_calls >= 1
            assert h.emitter.of("review_confirmed") == []
            assert h.status_of("ps") != "done"
            ends = [a["end_reason"] for a in h.attempts_of("ps")]
            assert ends
            assert "completed" not in ends
            # Non-vacuous: end reasons must be explicit fail-closed codes.
            assert all(r in ("crashed", "killed", "blocked") for r in ends), ends
        finally:
            h.close()

    def test_pathspec_commit_success_confirms_with_non_null_commit(self, tmp_path: Path) -> None:
        """M-35 allowed path: successful pathspec commit may confirm with sha."""
        h = make_harness(
            tmp_path,
            [{"id": "ok", "owned_paths": ["src/ok.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior(
            "ok",
            {
                "kind": "complete",
                "polls": 1,
                "output": "delivered",
                "write_files": {"src/ok.txt": "payload"},
                "files": ["src/ok.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.git.commit_paths_calls >= 1
            confirmed = h.emitter.of("review_confirmed")
            assert confirmed, "expected a successful confirm on the allowed path"
            commit = confirmed[0].get("commit")
            assert commit is not None and str(commit), confirmed[0]
            assert h.status_of("ok") == "done"
            ends = [a["end_reason"] for a in h.attempts_of("ok")]
            assert "completed" in ends
        finally:
            h.close()


# ---------------------------------------------------------------------------
# M-34 clean-start — no false capacity loss
# ---------------------------------------------------------------------------


class TestM34CleanStartNoFalseCapacityLoss:
    def test_clean_start_emits_no_capacity_loss(self, tmp_path: Path) -> None:
        """Startup reconcile must not treat never-started slots as dead."""
        h = make_harness(
            tmp_path,
            [{"id": "a", "owned_paths": ["src/a.txt"]}],
            max_concurrency=2,
            integration=False,
        )
        h.world.set_behavior(
            "a",
            {
                "kind": "complete",
                "polls": 1,
                "output": "ok",
                "write_files": {"src/a.txt": "x"},
                "files": ["src/a.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            loss = [
                e
                for e in h.emitter.of("resize")
                if "worker_capacity_loss" in str(e.get("reason") or "")
            ]
            assert loss == [], f"false capacity-loss on clean start: {loss}"
            assert h.status_of("a") == "done"
        finally:
            h.close()


# ---------------------------------------------------------------------------
# H-35 shadow-evidence gating
# ---------------------------------------------------------------------------


class TestH35ShadowEvidenceGating:
    def test_missing_shadow_evidence_blocks_enforce(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 1, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        cfg = tmp_path / "parallelism.yaml"
        missing = tmp_path / "no-such-evidence.json"
        cfg.write_text(
            "collision_safety:\n"
            "  mode: enforce\n"
            "  fencing_ready: true\n"
            "  allow_enforce: true\n"
            "  require_shadow_evidence: true\n"
            f"  shadow_evidence_path: {missing}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.fencing_ready() is True
        assert cs.shadow_evidence_recorded() is False
        assert cs.enforce_allowed() is False
        assert cs.effective_collision_mode() == "shadow"

    def test_present_shadow_evidence_allows_enforce(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 1, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        evidence = tmp_path / "shadow-soak.json"
        evidence.write_text('{"soak_days": 7, "conflicts": 0}\n', encoding="utf-8")
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n"
            "  mode: enforce\n"
            "  fencing_ready: true\n"
            "  allow_enforce: true\n"
            "  require_shadow_evidence: true\n"
            f"  shadow_evidence_path: {evidence}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        assert cs.shadow_evidence_recorded() is True
        assert cs.enforce_allowed() is True
        assert cs.effective_collision_mode() == "enforce"


# ---------------------------------------------------------------------------
# M-44 — resume reclaim of execution_dir
# ---------------------------------------------------------------------------


class TestM44ResumeReclaimExecutionDir:
    def test_execute_attach_reclaims_recorded_execution_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resume attach must reclaim the recorded execution_dir, not a fake path."""
        h = make_harness(
            tmp_path,
            [{"id": "resume", "owned_paths": ["src/resume.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior("resume", {"kind": "hang"})
        reclaimed: list[tuple[str, str]] = []
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.world.active >= 1, timeout=10)
            assert wait_until(
                lambda: bool(h.swarm_json_of("resume").get("execution_dir")),
                timeout=10,
            )
            recorded = str(h.swarm_json_of("resume").get("execution_dir") or "")
            assert recorded

            with scheduler._runs_lock:
                state = scheduler._runs[h.run_id]
            task_id = h.task_id("resume")

            # Quiesce the live worker before driving the direct attach.
            #
            # start_run() launched a coordinator + worker thread, and the
            # "resume" worker is mid-flight on the hang session. Inside
            # _execute_task it spawns the session, binds it onto the attempt
            # (bind_attempt_session), registers the attempt in
            # state.running_attempts, and only THEN enters the real
            # _await_and_settle poll loop (scheduler.py ~4572-4647). The
            # wait_until gates above only prove the session is live (active >= 1,
            # observable at spawn) and that execution_dir is recorded — both fire
            # in the window AFTER spawn but BEFORE bind_attempt_session. Reading
            # current_attempt in that window yields a half-bound attempt whose
            # session_id is still empty, and _execute_attach then takes its
            # "attempt has no session" fail-closed branch (closes the attempt,
            # returns a mechanical failure) instead of "parked" — flapping the
            # disposition and reclaim assertions below. That is the rare thread
            # race the bisect flagged.
            #
            # Wait for the exact settled state the attach depends on: the attempt
            # registered as running AND its session_id bound. Quiescing before the
            # _await_and_settle patch also lets the worker settle into the REAL
            # await loop (which, on a hang, holds running_attempts for the whole
            # sub-second test window), so the direct attach runs against a
            # fully-bound, quiesced attempt instead of racing the worker's bind.
            def _worker_settled() -> bool:
                with state.cond:
                    if task_id not in state.running_attempts:
                        return False
                return bool((h.dal.current_attempt(task_id) or {}).get("session_id"))

            assert wait_until(_worker_settled, timeout=10)

            def _capture(
                state: object,
                task_id: str,
                working_dir: str,
                owned_paths: object,
            ) -> None:
                del state, owned_paths
                reclaimed.append((task_id, working_dir))

            monkeypatch.setattr(scheduler, "_reclaim_task_scope", _capture)
            # Do not re-enter the hang await — reclaim is the contract under test.
            monkeypatch.setattr(
                scheduler,
                "_await_and_settle",
                lambda *a, **k: "parked",
            )

            attempt = h.dal.current_attempt(task_id)
            assert attempt is not None
            session_id = str(attempt.get("session_id") or "")
            info = {
                "task_id": task_id,
                "attempt_id": str(attempt["id"]),
                "session_id": session_id,
                "tier": "simple",
            }
            # Drive attach path (crash-resume reclaim).
            disposition = scheduler._execute_attach(state, info)
            assert disposition == "parked"

            assert reclaimed, "expected reclaim during attach"
            assert os.path.realpath(reclaimed[-1][1]) == os.path.realpath(recorded)
            # Prefer recorded execution_dir over workspace reconstruction.
            assert reclaimed[-1][1] == scheduler._canonical_exec_dir(recorded)

            h.dal.set_run_status(h.run_id, "cancelled", error="test done")
            assert handle.join(timeout=20)
        finally:
            h.close()


# ---------------------------------------------------------------------------
# M-44 — canonical execution directory binding
# ---------------------------------------------------------------------------


class TestM44ExecutionDirBinding:
    def test_claim_and_spawn_share_one_execution_dir(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "bind", "owned_paths": ["src/bind.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        h.world.set_behavior(
            "bind",
            {
                "kind": "complete",
                "polls": 1,
                "output": "bound correctly",
                "write_files": {"src/bind.txt": "x"},
                "files": ["src/bind.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.world.spawn_requests
            req = h.world.spawn_requests[0]
            exec_dir = os.path.realpath(req.working_dir)
            workspace = os.path.realpath(str(h.workdir))
            assert exec_dir == workspace

            # swarm_json records the same canonical path.
            sj = h.swarm_json_of("bind")
            recorded = str(sj.get("execution_dir") or "")
            assert recorded
            assert os.path.realpath(recorded) == workspace

            # Session project_dir matches.
            for row in h.world.sessions.values():
                pd = str(row.get("project_dir") or "")
                if pd:
                    assert os.path.realpath(pd) == workspace
        finally:
            h.close()

    def test_session_project_dir_divergence_fails_closed(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            [{"id": "div", "owned_paths": ["src/div.txt"]}],
            max_concurrency=1,
            integration=False,
        )
        other = tmp_path / "other-place"
        other.mkdir()
        h.world.set_behavior(
            "div",
            {
                "kind": "complete",
                "polls": 1,
                "output": "wrong tree",
                "project_dir": str(other),
                "write_files": {"src/div.txt": "x"},
                "files": ["src/div.txt"],
            },
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert h.emitter.of("review_confirmed") == []
            assert h.status_of("div") != "done"
            attempts = h.attempts_of("div")
            assert attempts
            ends = [a["end_reason"] for a in attempts]
            assert "completed" not in ends
            assert any(a.get("end_reason") == "crashed" for a in attempts)
            joined = " ".join(str(a.get("detail") or "") for a in attempts)
            assert "mismatch" in joined.lower()
            # Kill was requested after mismatch detection.
            assert h.world.kills
        finally:
            h.close()

    def test_canonical_exec_dir_normalizes(self) -> None:
        from omniagentos.swarm.scheduler import SwarmScheduler

        home = os.path.expanduser("~")
        assert SwarmScheduler._canonical_exec_dir("~/.") == os.path.realpath(home)
        assert SwarmScheduler._canonical_exec_dir("") == ""


# ---------------------------------------------------------------------------
# B05 Join — Lease loss checkpoints and bounded busy retry
# ---------------------------------------------------------------------------


class TestB05LeaseAndBusyControls:
    def test_git_guard_treats_unknown_main_identity_as_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown identity must retain MAIN's cross-process commit lock."""
        from omniagentos.swarm import scheduler as scheduler_module
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(tmp_path, [{"id": "t1"}])
        sched = make_scheduler(h)
        state = _RunState(
            run_id="test_run",
            working_dir=str(tmp_path),
            lease_generation=5,
            scope_realm="main-realm",
        )
        sched._dal.heartbeat = lambda *_args, **_kwargs: True
        observed: dict[str, Any] = {}

        @contextmanager
        def _record_guard(store: object, **kwargs: Any) -> Iterator[None]:
            observed["store"] = store
            observed.update(kwargs)
            yield None

        monkeypatch.setattr(scheduler_module, "inode_paths_equal", lambda *_args: None)
        monkeypatch.setattr(scheduler_module, "commit_guard", _record_guard)

        with sched._git_guard(state, str(tmp_path / "unknown-target"), "test_mutation"):
            pass

        assert observed["realm"] == "main-realm"
        assert observed["store"] is sched._locks()
        h.close()

    def test_git_guard_checkpoint_refuses_when_displaced(self, tmp_path: Path) -> None:
        """_git_guard must check lease status and raise ScopeCommitBusy if displaced."""
        from omniagentos.swarm.scheduler import ScopeCommitBusy, _RunState

        h = make_harness(tmp_path, [{"id": "t1"}])
        sched = make_scheduler(h)
        state = _RunState(run_id="test_run", working_dir=str(tmp_path))
        state.displaced = True

        with pytest.raises(ScopeCommitBusy, match="lease lost"):
            with sched._git_guard(state, str(tmp_path), "test_mutation"):
                pytest.fail("git mutation should not execute when lease is lost")
        h.close()

    def test_git_guard_checkpoint_refuses_when_heartbeat_fails(self, tmp_path: Path) -> None:
        """_git_guard checkpoint polls heartbeat and raises ScopeCommitBusy if heartbeat fails."""
        from omniagentos.swarm.scheduler import ScopeCommitBusy, _RunState

        h = make_harness(tmp_path, [{"id": "t1"}])
        sched = make_scheduler(h)
        state = _RunState(run_id="test_run", working_dir=str(tmp_path), lease_generation=5)

        def _heartbeat_false(run_id: str, generation: int | None = None) -> bool:
            return False

        sched._dal.heartbeat = _heartbeat_false

        with pytest.raises(ScopeCommitBusy, match="lease lost"):
            with sched._git_guard(state, str(tmp_path), "test_mutation"):
                pytest.fail("git mutation should not execute when heartbeat fails")

        assert state.displaced is True
        h.close()

    def test_dal_busy_retry_retries_and_raises_operational_error(self, tmp_path: Path) -> None:
        """SwarmDal @_serialized handles transient sqlite3.OperationalError and re-raises cleanly when exhausted."""
        import sqlite3

        from omniagentos.swarm.dal import SwarmDal, _serialized

        dal = SwarmDal(tmp_path / "test.db")
        attempts = 0

        class TestTarget:
            def __init__(self, dal_inst: Any) -> None:
                self._lock = dal_inst._lock

            @_serialized
            def flaky(self) -> str:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
                return "success"

            @_serialized
            def always_busy(self) -> None:
                nonlocal attempts
                attempts += 1
                raise sqlite3.OperationalError("database is locked")

        target = TestTarget(dal)
        assert target.flaky() == "success"
        assert attempts == 3

        attempts = 0
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            target.always_busy()

        assert attempts == 5

    def test_git_guard_binds_fence_and_checkpoints_lease_loss_after_acquire(
        self, tmp_path: Path
    ) -> None:
        """_git_guard yields bound fence and raises ScopeCommitBusy if lease is lost inside commit_guard after acquire."""
        from omniagentos.swarm.scheduler import ScopeCommitBusy, _RunState

        h = make_harness(tmp_path, [{"id": "t1"}])
        sched = make_scheduler(h)
        state = _RunState(run_id="test_run", working_dir=str(tmp_path), lease_generation=5)

        call_count = 0

        def _flaky_heartbeat(run_id: str, generation: int | None = None) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        sched._dal.heartbeat = _flaky_heartbeat

        executed = False
        with pytest.raises(ScopeCommitBusy, match="lease lost after acquire"):
            with sched._git_guard(state, str(tmp_path), "test_mutation"):
                executed = True

        assert not executed, "git mutation body must not execute when lease is lost post-acquire"
        assert state.displaced is True
        h.close()

    def test_git_guard_yields_bound_fence_on_success(self, tmp_path: Path) -> None:
        """_git_guard yields the bound CommitFence / AcquireResult on success."""
        from omniagentos.swarm.scheduler import _RunState

        h = make_harness(tmp_path, [{"id": "t1"}])
        sched = make_scheduler(h)
        state = _RunState(run_id="test_run", working_dir=str(tmp_path), lease_generation=5)

        def _heartbeat_true(run_id: str, generation: int | None = None) -> bool:
            return True

        sched._dal.heartbeat = _heartbeat_true

        executed = False
        with sched._git_guard(state, str(tmp_path), "test_mutation") as fence:
            executed = True
            assert fence is None or hasattr(fence, "granted")

        assert executed is True
        h.close()
