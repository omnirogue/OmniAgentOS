"""Isolated scenario runner for deterministic end-to-end swarm simulations."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import board_files
from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.collab.store import CollabStore
from omniagentos.sessions import token
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.activity import StoreSwarmEmitter
from omniagentos.swarm.contracts import ACTION_RESIZE, ACTION_TASK_BLOCKED
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.provider_exec import SUPPORTED_PROVIDERS, ProviderSessionRunner
from omniagentos.swarm.scheduler import (
    RouteDecision,
    SwarmReviewOutcome,
    SwarmRunHandle,
    SwarmScheduler,
    resume_stale_swarms,
)
from omniagentos.swarm.spawn import UnifiedSpawner, swarm_terminal_classifier
from tests.simharness.stub_provider import AttemptResult, ScriptedProcessFactory, StubAdapter
from tests.swarm.scheduler_fakes import FakeGit, FakeLimits, FakeWorktrees

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def sqlite_row_counts(path: Path) -> dict[str, Any]:
    """Read-only table counts; a missing operator DB remains missing."""
    if not path.exists():
        return {"exists": False, "tables": {}}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        for name in names:
            quoted = name.replace('"', '""')
            counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
        return {"exists": True, "tables": counts}
    finally:
        connection.close()


def tree_manifest(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Cheap file manifest for proving repo-anchored runtime roots stayed inert."""
    if not root.exists():
        return ()
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file()
        )
    )


class CannedRouter:
    """A deterministic ladder whose top successor is intentionally Fable.

    The current scheduler never supplies the ``max`` rung because
    ``_escalate("complex")`` clamps to ``complex``.  Once that wiring is fixed,
    this real routing seam returns a different model without changing the test.
    """

    _MODELS = {
        "simple": "stub-simple",
        "standard": "stub-standard",
        "complex": "gpt-5.6-sol",
        "max": "claude-fable-5",
    }

    def route(self, task: dict[str, Any], tier: str) -> RouteDecision:
        title = str(task.get("title") or "").lower()
        provider = next(
            (
                candidate
                for candidate in sorted(SUPPORTED_PROVIDERS)
                if title == f"provider {candidate}"
            ),
            "codex",
        )
        model = (
            f"stub-{provider}"
            if provider != "codex" or title == "provider codex"
            else self._MODELS.get(tier, "claude-fable-5")
        )
        return RouteDecision(
            provider=provider,
            model=model,
            tier=tier,
            account_id=f"sim_{provider}",
            effort="low",
        )


class OutputReviewer:
    """Treat a canned ``REVIEW_DENY`` provider response as semantic failure."""

    def review(
        self,
        *,
        task: dict[str, Any],
        swarm_json: dict[str, Any],
        session: dict[str, Any],
        verify_output: str,
        flags: Any,
    ) -> SwarmReviewOutcome:
        del task, swarm_json, verify_output, flags
        output = str(session.get("output_text") or "")
        verdict: Literal["confirm", "deny"] = (
            "deny" if output.startswith("REVIEW_DENY") else "confirm"
        )
        return SwarmReviewOutcome(
            verdict=verdict,
            feedback=f"stub reviewer saw {output}",
            reviewer="deterministic-stub",
        )


class CapturingScheduler(SwarmScheduler):
    """Real scheduler with handles exposed only for bounded test joins."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.handles: dict[str, SwarmRunHandle] = {}

    def start_run(self, run_id: str, *, block: bool = False) -> SwarmRunHandle | None:
        handle = super().start_run(run_id, block=block)
        if handle is not None:
            self.handles[run_id] = handle
        return handle

    def resume_swarm(self, run_id: str, *, block: bool = False) -> SwarmRunHandle | None:
        handle = super().resume_swarm(run_id, block=block)
        if handle is not None:
            self.handles[run_id] = handle
        return handle


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    run_id: str
    status: str
    target_concurrency: int
    max_interval_overlap: int
    attempts: tuple[dict[str, Any], ...]
    provider_models: tuple[str, ...]
    network_attempts: int
    scripts_remaining: int

    def normalized(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "target_concurrency": self.target_concurrency,
            "max_interval_overlap": self.max_interval_overlap,
            "provider_models": list(self.provider_models),
            "attempts": list(self.attempts),
            "network_attempts": self.network_attempts,
            "scripts_remaining": self.scripts_remaining,
        }


class SimulationCampaign:
    """One isolated API→coordinator→provider-exec campaign."""

    def __init__(
        self,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        scenario: str,
        worktrees_enabled: bool = False,
        timeout_minutes: Mapping[str, float] | None = None,
        stall_minutes: float = 1.0,
        retry_cap: int | None = None,
        splitter: Any | None = None,
        target_cap: int = 2,
        budget_enforcement_block: bool = False,
        manifest_operator_roots: bool = False,
    ) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.scenario = scenario
        self.worktrees_enabled = worktrees_enabled
        self.timeout_minutes = dict(
            timeout_minutes
            or {
                "simple": 1.0,
                "standard": 1.0,
                "complex": 1.0,
                "max": 1.0,
            }
        )
        self.stall_minutes = stall_minutes
        self.retry_cap = retry_cap
        self._splitter = splitter
        self.target_cap = target_cap
        self.budget_enforcement_block = budget_enforcement_block
        self.repo_root = Path(__file__).resolve().parents[2]
        ambient = os.environ.get("OMNIAGENTOS_DB")
        self.operator_db = (
            Path(ambient).expanduser().resolve()
            if ambient
            else (self.repo_root / "var" / "omniagentos.db").resolve()
        )
        self.operator_before = sqlite_row_counts(self.operator_db)
        self.operator_after: dict[str, Any] | None = None
        self.operator_roots = tuple(
            (self.repo_root / name).resolve() for name in ("var", "ledger", "vault")
        )
        # Manifest walks over the LIVE var/ledger/vault trees cost ~87µs/file;
        # a grown var/ (1.3M+ files) eats the entire 180s pytest-timeout twice
        # over — and a live tree mutated by daemons mid-test yields false diffs
        # anyway. Only the operator-db-isolation scenario asserts this proof,
        # so it is opt-in per campaign.
        self.manifest_operator_roots = manifest_operator_roots
        self.operator_roots_before = (
            {str(root): tree_manifest(root) for root in self.operator_roots}
            if manifest_operator_roots
            else None
        )
        self.operator_roots_after: dict[str, tuple[tuple[str, int, int], ...]] | None = None
        self.break_mode = os.environ.get("SIMHARNESS_BREAK", "").strip()

        self.db_path = self.root / "control-plane.db"
        if self.break_mode == "isolation":
            # Revert probe only: deliberately wire the campaign to the ambient
            # sentinel DB so the isolation assertion must detect row changes.
            self.db_path = self.operator_db
        self.var_dir = self.root / self.scenario / "var"
        self.ledger_dir = self.root / self.scenario / "ledger"
        self.vault_dir = self.root / self.scenario / "vault"
        self.workspace = self.root / self.scenario / "workspace"
        self.provider_config = self.root / self.scenario / "provider-config"
        self.network_calls: list[str] = []
        self.process_factory = ScriptedProcessFactory()
        self.worktrees = FakeWorktrees(self.root / "worktrees")
        self._planner_tasks: list[dict[str, Any]] = []
        self._saved_overrides: dict[Any, Any] = {}
        self._saved_default_schedulers: dict[str, SwarmScheduler] = {}

    def __enter__(self) -> SimulationCampaign:
        for directory in (
            self.root / self.scenario,
            self.var_dir,
            self.ledger_dir,
            self.vault_dir,
            self.workspace,
            self.provider_config,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        isolated_env = {
            "OMNIAGENTOS_SIM_MODE": "1",
            "OMNIAGENTOS_SIM_CAMPAIGN": self.scenario,
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": str(self.root / self.scenario),
            "OMNIAGENTOS_SIM_ROOT": str(self.root),
            "OMNIAGENTOS_DB": str(self.db_path),
            "OMNIAGENTOS_VAR": str(self.var_dir),
            "OMNIAGENTOS_VAR_DIR": str(self.var_dir),
            "OMNIAGENTOS_LEDGER_DIR": str(self.ledger_dir),
            "OMNIAGENTOS_VAULT_DIR": str(self.vault_dir),
            "OMNIAGENTOS_SWARM_EXECUTE": "1",
            "OMNIAGENTOS_SWARM_TARGET_CAP": str(self.target_cap),
            "CODEX_HOME": str(self.provider_config / "codex"),
            # Pin the formation config: scenario expectations (e.g. which
            # reviewer lineage is legal against the stub implementers) must not
            # shift when the operator retunes configs/formations.yaml.
            "OMNIAGENTOS_FORMATIONS_PATH": str(
                Path(__file__).resolve().parent / "formations_fixture.yaml"
            ),
            # STATED BOTH WAYS, never inherited. This used to set "block" only
            # when the campaign asked for it and leave the other side ambient,
            # so a launch-env shell (which exports block since 2026-08-04) made
            # every advisory-premise scenario run under hard admission blocking:
            # the recovery attempt was never admitted, timeout-escalate never
            # terminalized inside its 15s join, and malformed-json ended
            # `failed` with its second scripted response unconsumed. Those two
            # nodes are in the counterfeit corpus's must_fail union, so the
            # merge gate refused candidates for a defect in its own instrument
            # for ~8 runs. A campaign's enforcement posture is part of the
            # scenario definition; see tests/simharness/test_env_premises.py.
            "OMNIAGENTOS_BUDGET_ENFORCEMENT": (
                "block" if self.budget_enforcement_block else "advisory"
            ),
        }
        for name, value in isolated_env.items():
            self.monkeypatch.setenv(name, value)

        # The production planner deliberately floors automatic swarms at two
        # slots, but deterministic campaigns also need to exercise a true
        # single-slot admission boundary.  The campaign's ``target_cap`` is an
        # injected test control, so bind the planner function directly instead
        # of relying on the production environment-variable floor.
        self.monkeypatch.setattr(
            "omniagentos.swarm.planner._target_cap",
            lambda: max(1, self.target_cap),
        )

        def deny_network(sock: socket.socket, address: Any) -> None:
            del sock
            self.network_calls.append(repr(address))
            raise AssertionError(f"network disabled in simulation harness: {address!r}")

        self.monkeypatch.setattr(socket.socket, "connect", deny_network)
        # wrap_available must be True since 2f5b6fa3 (fail-closed sandbox):
        # provider_exec now REFUSES to spawn when the outer wrap is unproven,
        # so a False pin turns every simulated spawn into a crash and no
        # scripted response is ever consumed. Mirrors the autouse
        # _sandbox_wrap_available_by_default fixture in test_provider_exec.
        # No real Seatbelt engages in-sim: StubAdapter overrides
        # _sandboxed_launch with an identity wrap.
        self.monkeypatch.setattr(
            "omniagentos.runner.sandbox.wrap_available",
            lambda command, workspace_dir: True,
        )
        self.monkeypatch.setattr(
            board_files,
            "_approved_workspace_roots",
            lambda: [str((self.root / self.scenario).resolve())],
        )
        self.monkeypatch.setattr(token, "TOKEN_PATH", self.var_dir / "secrets" / "sessions-token")
        self.auth_headers = {"X-Session-Token": token.load_or_create_token()}

        self.collab = CollabStore(str(self.db_path))
        self.store = self.collab._store
        self.dal = SwarmDal(str(self.db_path))
        self.sessions = SessionsDal(str(self.db_path))
        for provider in sorted(SUPPORTED_PROVIDERS):
            config_dir = self.provider_config / provider
            config_dir.mkdir(parents=True, exist_ok=True)
            self.dal._connection.execute(
                "INSERT INTO claude_accounts "
                "(id, label, auth_type, config_dir, provider, enabled, is_default, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, 'config_dir', ?, ?, 1, 0, 'ok', ?, ?)",
                (
                    f"sim_{provider}",
                    f"Simulation {provider}",
                    str(config_dir),
                    provider,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        self.dal._connection.commit()

        provider_runner = ProviderSessionRunner(
            self.sessions,
            db_path=str(self.db_path),
            process_factory=self.process_factory,
            adapter_resolver=StubAdapter,
            poll_interval=0.01,
            kill_grace=0.0,
            getpgid=lambda pid: pid,
            killpg=self.process_factory.killpg,
            pgid_liveness=self.process_factory.pgid_alive,
            command_reader=lambda pid: "",
            reserve_account=lambda *args, **kwargs: None,
            convert_reservation=lambda *args, **kwargs: True,
            report_outcome=lambda *args, **kwargs: None,
        )
        self.provider_runner = provider_runner
        spawner = UnifiedSpawner(
            db_path=str(self.db_path),
            provider_runner=provider_runner,
            swarm_dal=self.dal,
            sessions_dal=self.sessions,
            convert_reservation=lambda reservation_id, session_id: True,
            release_reservation=lambda reservation_id: True,
            var_root=self.var_dir / "swarm",
        )
        scheduler_kwargs: dict[str, Any] = {
            "dal": self.dal,
            "collab": self.collab,
            "emitter": StoreSwarmEmitter(self.store),
            "spawner": spawner,
            "session_store": self.sessions,
            "router": CannedRouter(),
            "reviewer": OutputReviewer(),
            "splitter": self._splitter
            if self._splitter is not None
            else (lambda task, swarm_json: None),
            "verifier": lambda task, swarm_json, working_dir: (True, "stub verify passed"),
            "git": FakeGit(),
            "limits": FakeLimits(capacity=100),
            "classifier": swarm_terminal_classifier,
            "worktrees": self.worktrees,
            "worktrees_enabled": self.worktrees_enabled,
            "worker_subtask_requests": False,
            "heartbeat_seconds": 0.02,
            "fallback_poll_seconds": 0.02,
            "worker_poll_seconds": 0.01,
            "await_poll_seconds": 0.01,
            "stall_minutes": self.stall_minutes,
            "timeout_minutes": self.timeout_minutes,
            "db_path": str(self.db_path),
            "summary_writer": lambda run_id: None,
        }
        if self.retry_cap is not None:
            scheduler_kwargs["retry_cap"] = self.retry_cap
        self.scheduler = CapturingScheduler(**scheduler_kwargs)
        # ``target_cap`` must constrain the live coordinator as well as the
        # plan.  Otherwise _recompute_target() can widen a nominal one-slot
        # campaign after planning, letting multiple workers cross a budget
        # admission gate before the first attempt settles.
        configured_target_cap = max(1, self.target_cap)
        scheduler_run_cap = self.scheduler._run_cap
        self.scheduler._run_cap = (  # type: ignore[method-assign]
            lambda run: min(scheduler_run_cap(run), configured_target_cap)
        )
        if self.break_mode == "concurrency":
            self.scheduler._run_cap = lambda run: 1  # type: ignore[method-assign]
        if self.break_mode == "usage":
            self.provider_runner._record_usage = (  # type: ignore[method-assign]
                lambda session_id, parsed, wall_ms: None
            )
        if self.break_mode == "terminal":
            original_set_status = self.dal.set_run_status

            def refuse_terminal(run_id: str, status: str, *args: Any, **kwargs: Any) -> Any:
                if status in TERMINAL_RUN_STATUSES:
                    return True
                return original_set_status(run_id, status, *args, **kwargs)

            self.dal.set_run_status = refuse_terminal  # type: ignore[method-assign]

        self._saved_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_store] = lambda: self.store
        app.dependency_overrides[swarm_routes.get_swarm_dal] = lambda: self.dal
        app.dependency_overrides[swarm_routes.get_swarm_planner_llm] = lambda: self._planner_llm
        app.dependency_overrides[swarm_routes.get_swarm_clarify_llm] = lambda: self._clarify_llm
        app.dependency_overrides[swarm_routes.get_swarm_recall_fn] = lambda: self._recall

        from omniagentos.swarm import scheduler as scheduler_module

        # activate_run_if_enabled → _default_scheduler() with no args resolves
        # via default_db_path() (OMNIAGENTOS_DB) then _canonical_scheduler_db_path.
        # Register under that same key so the campaign captures the run.
        self._saved_default_schedulers = dict(scheduler_module._DEFAULT_SCHEDULERS)
        registry_key = scheduler_module._canonical_scheduler_db_path(str(self.db_path))
        with scheduler_module._DEFAULT_SCHEDULER_LOCK:
            scheduler_module._DEFAULT_SCHEDULERS[registry_key] = self.scheduler
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://simharness",
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        from omniagentos.swarm import scheduler as scheduler_module

        self.scheduler.shutdown()
        _run(self.client.aclose())
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._saved_overrides)
        # Restore the per-db registry EXACTLY (snapshot replace, not key-delete).
        with scheduler_module._DEFAULT_SCHEDULER_LOCK:
            scheduler_module._DEFAULT_SCHEDULERS.clear()
            scheduler_module._DEFAULT_SCHEDULERS.update(self._saved_default_schedulers)
        self.dal.close()
        self.operator_after = sqlite_row_counts(self.operator_db)
        if self.manifest_operator_roots:
            self.operator_roots_after = {
                str(root): tree_manifest(root) for root in self.operator_roots
            }

    @staticmethod
    def _clarify_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "mode": "spec",
            "spec": {
                "title": "Deterministic simulation",
                "description": "Exercise the real swarm coordinator.",
                "acceptance_criteria": [],
            },
        }

    @staticmethod
    def _recall(goal: str) -> str:
        del goal
        return ""

    def _planner_llm(
        self,
        prompt: str,
        schema: dict[str, Any],
        effort: str,
    ) -> dict[str, Any]:
        del prompt, schema, effort
        return {
            "goal": f"{self.scenario} goal",
            "assumptions": [],
            "tasks": list(self._planner_tasks),
            "suite_command": "",
        }

    def dispatch(
        self,
        tasks: Sequence[Mapping[str, Any]],
        results: Sequence[AttemptResult],
        *,
        barrier_size: int = 0,
        activate: bool = True,
        budget_usd_max: float | None = None,
        re_script: bool = True,
    ) -> str:
        self._planner_tasks = [dict(task) for task in tasks]
        effective_barrier = 0 if self.break_mode == "concurrency" else barrier_size
        if re_script:
            self.process_factory.script(list(results), barrier_size=effective_barrier)
        os.environ["OMNIAGENTOS_SWARM_EXECUTE"] = "1" if activate else "0"
        payload: dict[str, Any] = {
            "brief": f"Run deterministic scenario {self.scenario}",
            "working_dir": str(self.workspace),
        }
        if budget_usd_max is not None:
            payload["budget_usd_max"] = budget_usd_max
        response = _run(
            self.client.post(
                "/api/swarm?sync=1",
                headers=self.auth_headers,
                json=payload,
            )
        )
        assert response.status_code == 202, (
            f"POST /api/swarm failed: status={response.status_code} body={response.text}"
        )
        return str(response.json()["swarm_run_id"])

    def join(self, run_id: str, timeout: float = 8.0) -> str:
        handle = self.scheduler.handles.get(run_id)
        assert handle is not None, f"activation hook did not start run {run_id}"
        assert handle.join(timeout=timeout), f"bounded coordinator timeout for run {run_id}"
        row = self.dal.get_run(run_id)
        assert row is not None
        return str(row["status"])

    def start(self, run_id: str) -> None:
        """Activate a previously provisioned (activate=False) run on this campaign scheduler."""
        os.environ["OMNIAGENTOS_SWARM_EXECUTE"] = "1"
        handle = self.scheduler.start_run(run_id)
        assert handle is not None, f"start_run refused for {run_id}"

    def cancel(self, run_id: str) -> dict[str, Any]:
        response = _run(
            self.client.post(
                f"/api/swarm/{run_id}/cancel",
                headers=self.auth_headers,
            )
        )
        assert response.status_code == 200, (
            f"POST /api/swarm/{{id}}/cancel failed: status={response.status_code} "
            f"body={response.text}"
        )
        return dict(response.json())

    def task_statuses(self, run_id: str) -> dict[str, str]:
        """Map planner task_key → board status for member tasks (root excluded)."""
        run = self.dal.get_run(run_id)
        assert run is not None
        root_id = str(run.get("board_task_id") or "")
        statuses: dict[str, str] = {}
        for task in self.dal.tasks_for_run(run_id):
            task_id = str(task["id"])
            if task_id == root_id:
                continue
            swarm_json = self.dal.get_swarm_json(task_id) or {}
            key = str(swarm_json.get("task_key") or task_id)
            statuses[key] = str(task["status"])
        return statuses

    def swarm_json_for(self, run_id: str, task_key: str) -> dict[str, Any]:
        for task in self.dal.tasks_for_run(run_id):
            swarm_json = self.dal.get_swarm_json(str(task["id"])) or {}
            if str(swarm_json.get("task_key") or "") == task_key:
                return dict(swarm_json)
        raise AssertionError(f"no task with key {task_key!r} in run {run_id}")

    def adopt_stale(self, run_id: str) -> dict[str, Any]:
        assert self.dal.try_activate_run(run_id)
        self.dal._connection.execute(
            "UPDATE swarm_runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (run_id,),
        )
        if self.break_mode == "adoption":
            self.dal.adopt_run = lambda run_id, stale_minutes: False  # type: ignore[method-assign]
        return resume_stale_swarms(
            scheduler=self.scheduler,
            dal=self.dal,
            reconcile_orphans=lambda: {},
            stale_minutes=2.0,
            db_path=str(self.db_path),
        )

    def result(self, run_id: str) -> ScenarioResult:
        run = self.dal.get_run(run_id)
        assert run is not None
        task_keys: dict[str, str] = {}
        for task in self.dal.tasks_for_run(run_id):
            swarm_json = self.dal.get_swarm_json(str(task["id"])) or {}
            task_keys[str(task["id"])] = str(swarm_json.get("task_key") or task["id"])

        attempts: list[dict[str, Any]] = []
        for attempt in self.dal.attempts_with_usage(run_id):
            attempts.append(
                {
                    "task_key": task_keys.get(
                        str(attempt["board_task_id"]),
                        str(attempt["board_task_id"]),
                    ),
                    "seq": int(attempt["seq"]),
                    "provider": str(attempt["provider"]),
                    "model": str(attempt["model"]),
                    "tier": str(attempt["tier"]),
                    "end_reason": attempt.get("end_reason"),
                    "cost_usd": attempt.get("cost_usd"),
                    "input_tokens": attempt.get("input_tokens"),
                    "output_tokens": attempt.get("output_tokens"),
                    "usage_source": attempt.get("usage_source"),
                }
            )
        attempts.sort(key=lambda row: (row["task_key"], row["seq"]))
        provider_models = tuple(
            sorted(str(interval.model) for interval in self.process_factory.intervals)
        )
        return ScenarioResult(
            scenario=self.scenario,
            run_id=run_id,
            status=str(run["status"]),
            target_concurrency=int(run["target_concurrency"]),
            max_interval_overlap=self.process_factory.strict_max_overlap(),
            attempts=tuple(attempts),
            provider_models=provider_models,
            network_attempts=len(self.network_calls),
            scripts_remaining=self.process_factory.remaining,
        )

    def resize_targets(self, run_id: str) -> list[int]:
        """Admission target levels from ACTION_RESIZE swarm.event rows.

        Uses ``SwarmDal.events_for_run`` (same reader as summary/metrics). Returns
        the seed width (first event's ``from``) followed by every ``to``, so seed
        over-admission remains visible even when the first recompute shrinks.
        An empty list means no resize signal was recorded.
        """
        events = self.dal.events_for_run(run_id, actions=(ACTION_RESIZE,))
        if not events:
            return []
        levels: list[int] = []
        first_payload = events[0].get("payload") or {}
        if first_payload.get("from") is not None:
            levels.append(int(first_payload["from"]))
        for event in events:
            payload = event.get("payload") or {}
            if payload.get("to") is not None:
                levels.append(int(payload["to"]))
        return levels

    def budget_cap_blocked_task_ids(self, run_id: str) -> list[str]:
        """Board task ids blocked by budget via ACTION_TASK_BLOCKED events.

        Only payloads with ``reason == "budget cap reached"`` count — that string
        is emitted solely by ``SwarmScheduler._block_remaining_for_budget``. Other
        ``task_blocked`` reasons (deps, retry cap, merge conflict) are ignored so
        the detector cannot be satisfied by an unrelated block.
        """
        events = self.dal.events_for_run(run_id, actions=(ACTION_TASK_BLOCKED,))
        task_ids: list[str] = []
        for event in events:
            payload = event.get("payload") or {}
            if str(payload.get("reason") or "") != "budget cap reached":
                continue
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                task_ids.append(task_id)
        return task_ids

    def task_key_by_id(self, run_id: str) -> dict[str, str]:
        """Map board task id → planner task_key for member tasks (root excluded)."""
        run = self.dal.get_run(run_id)
        assert run is not None
        root_id = str(run.get("board_task_id") or "")
        mapping: dict[str, str] = {}
        for task in self.dal.tasks_for_run(run_id):
            task_id = str(task["id"])
            if task_id == root_id:
                continue
            swarm_json = self.dal.get_swarm_json(task_id) or {}
            mapping[task_id] = str(swarm_json.get("task_key") or task_id)
        return mapping

    def pin_run_cap_to_plan(self, run_id: str) -> int:
        """Set ``max_concurrency`` equal to ``target_concurrency`` for ``run_id``.

        Scheduler recompute clamps to ``_run_cap`` (max_concurrency), while the
        plan ceiling under test is ``target_concurrency``. Binding them makes the
        plan the single admission ceiling so ACTION_RESIZE levels can be checked
        against the plan without demand-driven growth past it. Call after
        provision and before ``start`` / activate.
        """
        run = self.dal.get_run(run_id)
        assert run is not None, f"unknown run {run_id}"
        plan_target = int(run["target_concurrency"])
        self.dal._connection.execute(
            "UPDATE swarm_runs SET max_concurrency = ? WHERE id = ?",
            (plan_target, run_id),
        )
        self.dal._connection.commit()
        return plan_target

    def write_evidence(self, payload: Mapping[str, Any]) -> None:
        normalized = dict(payload)
        output = json.dumps(normalized, indent=2, sort_keys=True)
        destination_root = os.environ.get("SIMHARNESS_EVIDENCE_DIR", "").strip()
        if destination_root:
            destination = Path(destination_root).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            evidence_path = destination / f"{self.scenario}.json"
        else:
            evidence_path = self.root / "evidence.json"
        evidence_path.write_text(output + "\n", encoding="utf-8")
        print(f"SIMHARNESS_EVIDENCE {self.scenario} {json.dumps(normalized, sort_keys=True)}")

    @property
    def operator_unchanged(self) -> bool:
        return self.operator_after == self.operator_before

    @property
    def operator_roots_unchanged(self) -> bool:
        if not self.manifest_operator_roots:
            raise AssertionError(
                "operator_roots_unchanged requires manifest_operator_roots=True "
                "on the campaign — without the walks this proof would be "
                "trivially (falsely) green"
            )
        return self.operator_roots_after == self.operator_roots_before


def standard_tasks(*, complexity: str = "simple") -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "title": key.upper(),
            "description": f"implement {key}",
            "depends_on": [],
            "owned_paths": [f"src/{key}.py"],
            "complexity": complexity,
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": f"{key} complete",
            "verify_command": "",
        }
        for key in ("alpha", "beta", "gamma")
    ]


def dependency_tasks(*, swarm: bool = True) -> list[dict[str, Any]]:
    """Parent + dependent child [+ parallel sibling when swarm=True].

    With ``swarm=True`` three workers at equal estimates keep the plan out of
    solo mode (parallelism ratio ≥ 1.5) so build_plan appends integration.
    With ``swarm=False`` only parent→child (solo plan, no integration).
    """
    tasks = [
        {
            "id": "parent",
            "title": "Parent",
            "description": "upstream work",
            "depends_on": [],
            "owned_paths": ["src/parent.py"],
            "complexity": "simple",
            "est_agent_minutes": 30,
            "est_manual_minutes": 60,
            "acceptance": "parent complete",
            "verify_command": "",
        },
        {
            "id": "child",
            "title": "Child",
            "description": "depends on parent",
            "depends_on": ["parent"],
            "owned_paths": ["src/child.py"],
            "complexity": "simple",
            "est_agent_minutes": 30,
            "est_manual_minutes": 30,
            "acceptance": "child complete",
            "verify_command": "",
        },
    ]
    if swarm:
        tasks.append(
            {
                "id": "sibling",
                "title": "Sibling",
                "description": "independent work",
                "depends_on": [],
                "owned_paths": ["src/sibling.py"],
                "complexity": "simple",
                "est_agent_minutes": 30,
                "est_manual_minutes": 30,
                "acceptance": "sibling complete",
                "verify_command": "",
            }
        )
    return tasks


def single_task(*, task_id: str = "solo", complexity: str = "simple") -> list[dict[str, Any]]:
    return [
        {
            "id": task_id,
            "title": task_id.upper(),
            "description": f"implement {task_id}",
            "depends_on": [],
            "owned_paths": [f"src/{task_id}.py"],
            "complexity": complexity,
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": f"{task_id} complete",
            "verify_command": "",
        }
    ]


def provider_tasks() -> list[dict[str, Any]]:
    return [
        {
            "id": provider,
            "title": f"Provider {provider}",
            "description": f"exercise {provider} provider-exec",
            "depends_on": [],
            "owned_paths": [f"src/{provider}.py"],
            "complexity": "simple",
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": f"{provider} complete",
            "verify_command": "",
        }
        for provider in sorted(SUPPORTED_PROVIDERS)
    ]


def escalation_tasks() -> list[dict[str, Any]]:
    return [
        {
            "id": "coder",
            "title": "Coder",
            "description": "implement the core",
            "depends_on": [],
            "owned_paths": ["src/core.py"],
            "complexity": "complex",
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": "core complete",
            "verify_command": "",
        },
        {
            "id": "left",
            "title": "Left",
            "description": "consume core on the left",
            "depends_on": ["coder"],
            "owned_paths": ["src/left.py"],
            "complexity": "simple",
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": "left complete",
            "verify_command": "",
        },
        {
            "id": "right",
            "title": "Right",
            "description": "consume core on the right",
            "depends_on": ["coder"],
            "owned_paths": ["src/right.py"],
            "complexity": "simple",
            "est_agent_minutes": 10,
            "est_manual_minutes": 30,
            "acceptance": "right complete",
            "verify_command": "",
        },
    ]


__all__ = [
    "ScenarioResult",
    "SimulationCampaign",
    "TERMINAL_RUN_STATUSES",
    "dependency_tasks",
    "escalation_tasks",
    "provider_tasks",
    "single_task",
    "sqlite_row_counts",
    "standard_tasks",
    "tree_manifest",
]
