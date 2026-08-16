"""Orchestrator-decided EFFORT LEVELS, end to end (column via migration 049).

Covers the full chain the feature adds:

- config maps: ``router.effort_by_tier`` + ``router.effort_overrides``
  parsing (invalid tiers/efforts ignored, defaults from TIER_TO_MODE);
- SwarmRouter: every RouteDecision carries the decided effort (tier map,
  per-model override wins, risk-pin fallback candidate included);
- adapters: codex builds ``-c model_reasoning_effort="..."`` from the request
  (default "low" preserved), grok adds ``--reasoning-effort`` only when asked,
  kimi/gemini skip cleanly (no CLI knob — investigated live);
- spawner: ProviderSessionRunner threads the effort into the REAL codex argv
  (fake process factory assertion); UnifiedSpawner passes ``request.effort``;
- migration 049 applies onto a pre-049 database COPY (never a live db) and
  the new column round-trips through ``SwarmDal.open_attempt``;
- scheduler: the router-decided effort lands on the durable attempt row and
  the task_assigned event;
- summary: ``compute_metrics`` per-task efforts + the ledger manifest carries
  effort per attempt; optimizer: win-rate-by-effort playbook table +
  ``learned.json`` snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import omniagentos.runner.sandbox as runner_sandbox
from omniagentos.adapters.codex import CodexAdapter
from omniagentos.adapters.gemini import GeminiAdapter
from omniagentos.adapters.grok import GrokAdapter
from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import AgentInput, new_id, utc_now_iso
from omniagentos.db import migrate as migrate_module
from omniagentos.swarm import optimize, summary
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.router import (
    TIER_TO_MODE,
    effort_by_tier_map,
    effort_overrides_map,
)
from tests.support.db_template import migrated_db
from tests.swarm.scheduler_fakes import make_harness, make_scheduler
from tests.swarm.test_router import RANKINGS, _reservation, _task, make_router


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every DB touch in these tests must go through tmp fixtures — the
    migration/spawn paths here must NEVER see the live database."""
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


@pytest.fixture(autouse=True)
def _sandbox_wrap_available_for_fake_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model an available outer sandbox; every process in this module is fake."""
    monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner_sandbox, "wrap_available", lambda _argv, _workspace: True)


# ---------------------------------------------------------------------------
# config maps
# ---------------------------------------------------------------------------


class TestEffortConfigMaps:
    def test_defaults_mirror_tier_to_mode_hints(self) -> None:
        resolved = effort_by_tier_map({})
        assert resolved == {
            "simple": "low",
            "standard": "medium",
            "complex": "high",
        }
        for tier, effort in resolved.items():
            assert effort == TIER_TO_MODE[tier][2]

    def test_config_overrides_valid_entries_only(self) -> None:
        config = {
            "router": {
                "effort_by_tier": {
                    "simple": "HIGH",  # normalized
                    "standard": "warp",  # invalid effort: ignored
                    "sprint": "low",  # unknown tier: ignored
                }
            }
        }
        resolved = effort_by_tier_map(config)
        assert resolved["simple"] == "high"
        assert resolved["standard"] == "medium"  # default kept
        assert resolved["complex"] == "high"
        assert "sprint" not in resolved

    def test_overrides_map_normalizes_and_filters(self) -> None:
        config = {
            "router": {
                "effort_overrides": {
                    "GPT-5.6-Luna": "XHIGH",
                    "  ": "low",  # blank model: ignored
                    "grok-4.5": "bogus",  # invalid effort: ignored
                }
            }
        }
        assert effort_overrides_map(config) == {"gpt-5.6-luna": "xhigh"}

    def test_missing_router_section_degrades_to_defaults(self) -> None:
        assert effort_overrides_map({}) == {}
        assert effort_by_tier_map({"router": "nonsense"})["standard"] == "medium"


# ---------------------------------------------------------------------------
# router decision
# ---------------------------------------------------------------------------


class TestRouterEffortDecision:
    def test_decision_carries_tier_mapped_effort(self) -> None:
        router, limits, _ = make_router(
            effort_by_tier={"simple": "low", "standard": "medium", "complex": "high"},
            effort_overrides={},
        )
        limits.enabled = {"codex": 1}
        limits.reservations = {"codex": _reservation("codex")}
        for tier, expected in (("simple", "low"), ("standard", "medium"), ("complex", "high")):
            decision = router.route(_task({"complexity": tier}), tier)
            assert decision is not None
            assert decision.effort == expected, tier

    def test_per_model_override_beats_the_tier_map(self) -> None:
        router, limits, _ = make_router(
            effort_by_tier={"simple": "low", "standard": "medium", "complex": "high"},
            effort_overrides={"gpt-5.6-sol": "xhigh"},
        )
        limits.enabled = {"codex": 1}
        limits.reservations = {"codex": _reservation("codex")}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.provider == "codex"
        assert decision.model == "gpt-5.6-sol"
        assert decision.effort == "xhigh"

    def test_risk_pin_fallback_candidate_gets_effort_too(self) -> None:
        # No claude coder in the rankings: the pin falls back to the built-in
        # claude candidate — the decided effort must still ride the decision.
        router, _, _ = make_router(
            rankings={"sol-coder": RANKINGS["sol-coder"]},
            effort_by_tier={"simple": "low", "standard": "medium", "complex": "high"},
            effort_overrides={},
        )
        decision = router.route(_task({"risk_class": "deploy"}), "complex")
        assert decision is not None
        assert decision.provider == "claude"
        assert decision.effort == "high"

    def test_default_login_provider_decision_carries_effort(self) -> None:
        # Zero configured accounts: RouteDecision without a reservation still
        # carries the decided effort.
        router, limits, _ = make_router(
            effort_by_tier={"simple": "low", "standard": "medium", "complex": "high"},
            effort_overrides={},
        )
        limits.enabled = {}
        decision = router.route(_task(), "standard")
        assert decision is not None
        assert decision.reservation_id is None
        assert decision.effort == "medium"


# ---------------------------------------------------------------------------
# adapters: argv construction
# ---------------------------------------------------------------------------


def _agent_input(metadata: dict[str, Any] | None = None) -> AgentInput:
    return AgentInput(
        run_id="run1",
        task_id="task1",
        prompt="do the thing",
        working_dir="/tmp/ws",
        model=None,
        metadata=metadata or {},
    )


class TestAdapterEffortTokens:
    def test_codex_token_defaults_to_low(self) -> None:
        command = CodexAdapter()._command(_agent_input(), "p", None)
        assert 'model_reasoning_effort="low"' in command

    def test_codex_token_honors_requested_effort(self) -> None:
        command = CodexAdapter()._command(_agent_input({"reasoning_effort": "high"}), "p", None)
        index = command.index("-c")
        assert command[index + 1] == 'model_reasoning_effort="high"'
        assert 'model_reasoning_effort="low"' not in command

    def test_codex_rejects_out_of_vocabulary_effort(self) -> None:
        # Untrusted metadata cannot inject arbitrary argv content.
        command = CodexAdapter()._command(
            _agent_input({"reasoning_effort": 'high" -c evil="1'}), "p", None
        )
        assert 'model_reasoning_effort="low"' in command

    def test_grok_flag_only_when_requested(self) -> None:
        with_effort = GrokAdapter()._command(_agent_input({"reasoning_effort": "high"}), "p", None)
        index = with_effort.index("--reasoning-effort")
        assert with_effort[index + 1] == "high"

        without = GrokAdapter()._command(_agent_input(), "p", None)
        assert "--reasoning-effort" not in without

    def test_kimi_and_gemini_skip_cleanly(self) -> None:
        # Neither CLI has an effort knob (verified against --help): a
        # requested effort must leave their argv untouched.
        metadata = {"reasoning_effort": "high"}
        kimi_cmd = KimiAdapter()._command(_agent_input(metadata), "p", None)
        gemini_cmd = GeminiAdapter()._command(_agent_input(metadata), "p", None)
        for command in (kimi_cmd, gemini_cmd):
            assert "--reasoning-effort" not in command
            assert not any("reasoning_effort" in token for token in command)


# ---------------------------------------------------------------------------
# spawner: ProviderSessionRunner threads effort into the REAL codex argv
# ---------------------------------------------------------------------------


class _EffortFakeStream:
    def __init__(self) -> None:
        self._done = True

    def __iter__(self) -> _EffortFakeStream:
        return self

    def __next__(self) -> str:
        raise StopIteration


class _EffortFakeProcess:
    next_pid = 9000

    def __init__(self) -> None:
        self.pid = _EffortFakeProcess.next_pid
        _EffortFakeProcess.next_pid += 1
        self.stdout = _EffortFakeStream()
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


class _UnwrappedCodexAdapter(CodexAdapter):
    """The real codex argv without the OS-sandbox wrapper, so the process
    factory sees the exact CLI tokens."""

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        *,
        provider: str | None = None,
        provider_config_dir: str | None = None,
    ) -> list[str]:
        del working_dir, extra_write_roots, provider, provider_config_dir
        return command

    def _parse(self, stdout: str) -> Any:
        from omniagentos.adapters.common import ParsedResponse

        del stdout
        return ParsedResponse(text="ok", session_ref=None, payload={})


class TestSpawnerThreadsCodexEffort:
    def _runner_and_captures(self, tmp_path: Path):
        from omniagentos.swarm.provider_exec import ProviderSessionRunner

        db = str(tmp_path / "provider.db")
        db = migrated_db(CollabStore, db)
        captures: list[dict[str, Any]] = []

        def process_factory(command: list[str], **kwargs: Any) -> _EffortFakeProcess:
            captures.append({"command": list(command), **kwargs})
            return _EffortFakeProcess()

        runner = ProviderSessionRunner(
            db_path=db,
            process_factory=process_factory,
            adapter_resolver=lambda provider: _UnwrappedCodexAdapter(),
            poll_interval=0.05,
            reserve_account=lambda provider, **kwargs: None,
            convert_reservation=lambda *args, **kwargs: True,
            report_outcome=lambda *args, **kwargs: None,
        )
        return runner, captures

    def test_effort_lands_in_the_codex_argv(self, tmp_path: Path) -> None:
        runner, captures = self._runner_and_captures(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runner.spawn(
            provider="codex",
            model="gpt-5.6-luna",
            prompt="do the thing",
            working_dir=str(workspace),
            board_task_id="btk_1",
            swarm_run_id="swr_1",
            budget_usd_max=None,
            idle_minutes=5,
            risk_class="none",
            effort="high",
        )
        assert len(captures) == 1
        command = captures[0]["command"]
        index = command.index("-c")
        assert command[index + 1] == 'model_reasoning_effort="high"'

    def test_no_effort_keeps_the_low_default(self, tmp_path: Path) -> None:
        runner, captures = self._runner_and_captures(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runner.spawn(
            provider="codex",
            model="gpt-5.6-luna",
            prompt="do the thing",
            working_dir=str(workspace),
            board_task_id="btk_1",
            swarm_run_id="swr_1",
            budget_usd_max=None,
            idle_minutes=5,
            risk_class="none",
        )
        command = captures[0]["command"]
        assert 'model_reasoning_effort="low"' in command


class TestUnifiedSpawnerThreadsEffort:
    def test_request_prepin_loses_to_cbm_on_provider_runner(self, tmp_path: Path) -> None:
        """H-05 pre-pin-vs-CBM: router/scheduler pre-pin must not win when CBM allocates.

        Strengthens the prior "request.effort reaches runner" contract under the
        new precedence (CBM > request > default) without weakening the assertion
        that a concrete effort reaches the real provider adapter.
        """
        from omniagentos.swarm.scheduler import SpawnRequest
        from omniagentos.swarm.spawn import UnifiedSpawner

        calls: list[dict[str, Any]] = []

        class FakeRunner:
            def spawn(self, **kwargs: Any) -> str:
                calls.append(kwargs)
                return "ses_provider_1"

        class FakeSwarmDal:
            def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
                return []

            def get_swarm_json(self, task_id: str) -> dict[str, Any]:
                return {
                    "risk_class": "none",
                    "acceptance": "ok",
                    "novelty": "low",
                    "difficulty": "medium",  # CBM rung 1 → effort "low"
                }

            def set_swarm_json(self, task_id: str, swarm_json: dict[str, Any]) -> bool:
                return True

            def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
                return []

        spawner = UnifiedSpawner(
            provider_runner=FakeRunner(),
            swarm_dal=FakeSwarmDal(),
            convert_reservation=lambda *args: None,
            release_reservation=lambda *args: None,
            var_root=tmp_path / "var",
            db_path=str(tmp_path / "effort-prepin.db"),
        )
        session_id = spawner.spawn(
            SpawnRequest(
                run_id="swr_1",
                task_id="btk_1",
                task_key="t1",
                attempt_id="swa_1",
                working_dir=str(tmp_path),
                prompt="brief",
                provider="codex",
                model="gpt-5.6-luna",
                tier="standard",
                effort="medium",  # stale pre-pin — must lose to CBM
            )
        )
        assert session_id == "ses_provider_1"
        assert calls, "provider runner must receive a spawn call"
        # CBM medium-difficulty fast-first allocates reasoning_effort "low".
        assert calls[0]["effort"] == "low"
        assert calls[0]["effort"] != "medium"

    def test_request_effort_reaches_provider_when_cbm_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When CBM allocate is unavailable, request pre-pin still reaches the adapter."""
        from omniagentos.swarm.scheduler import SpawnRequest
        from omniagentos.swarm.spawn import UnifiedSpawner

        calls: list[dict[str, Any]] = []

        class FakeRunner:
            def spawn(self, **kwargs: Any) -> str:
                calls.append(kwargs)
                return "ses_provider_2"

        class FakeSwarmDal:
            def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
                return []

            def get_swarm_json(self, task_id: str) -> dict[str, Any]:
                return {}

            def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
                return []

        class BoomCBM:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("cbm unavailable")

        monkeypatch.setattr("omniagentos.cbm.service.CognitiveBudgetService", BoomCBM)

        spawner = UnifiedSpawner(
            provider_runner=FakeRunner(),
            swarm_dal=FakeSwarmDal(),
            convert_reservation=lambda *args: None,
            release_reservation=lambda *args: None,
            var_root=tmp_path / "var",
            db_path=str(tmp_path / "effort-fallback.db"),
        )
        session_id = spawner.spawn(
            SpawnRequest(
                run_id="swr_2",
                task_id="btk_2",
                task_key="t2",
                attempt_id="swa_2",
                working_dir=str(tmp_path),
                prompt="brief",
                provider="codex",
                model="gpt-5.6-luna",
                tier="standard",
                effort="medium",
            )
        )
        assert session_id == "ses_provider_2"
        assert calls[0]["effort"] == "medium"


# ---------------------------------------------------------------------------
# migration 049 (always on a tmp COPY — the autouse fixture pins the default
# db path into tmp, and every path below is under tmp_path)
# ---------------------------------------------------------------------------


class TestMigration049:
    def test_applies_onto_a_pre_049_database(self, tmp_path: Path) -> None:
        db = str(tmp_path / "copy.db")

        all_files = migrate_module._migration_files()
        pre_049 = [(version, path) for version, path in all_files if version < 49]
        assert any(version == 49 for version, _ in all_files)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(migrate_module, "_migration_files", lambda: pre_049)
            migrate_module.migrate(db)

        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(swarm_attempts)")
            }
            assert "effort" not in columns
            # A pre-049 attempt row that must survive the migration untouched.
            connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, provider, model, started_at) "
                "VALUES ('swa_old', 'swr_1', 'btk_1', 0, 'codex', 'gpt-5.6-luna', "
                "'2026-07-23T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()

        migrate_module.migrate(db)  # now includes 048

        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(swarm_attempts)")
            }
            assert "effort" in columns
            old = connection.execute(
                "SELECT effort FROM swarm_attempts WHERE id = 'swa_old'"
            ).fetchone()
            assert old["effort"] is None
            connection.execute("UPDATE swarm_attempts SET effort = 'high' WHERE id = 'swa_old'")
            connection.commit()
            assert (
                connection.execute(
                    "SELECT effort FROM swarm_attempts WHERE id = 'swa_old'"
                ).fetchone()["effort"]
                == "high"
            )
        finally:
            connection.close()

    def test_048_is_the_only_owner_of_its_version_number(self) -> None:
        versions = [version for version, _ in migrate_module._migration_files()]
        assert versions.count(48) == 1


# ---------------------------------------------------------------------------
# attempt row records effort (open_attempt)
# ---------------------------------------------------------------------------


class TestAttemptRecordsEffort:
    def test_open_attempt_persists_and_reads_back_effort(self, tmp_path: Path) -> None:
        db = migrated_db(CollabStore, tmp_path / "attempts.db")  # shared schema
        dal = SwarmDal(db)
        try:
            run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
            now = utc_now_iso()
            task_id = new_id("btk")
            dal._connection.execute(
                "INSERT INTO board_tasks "
                "(id, title, description, required_expertise_json, priority, status, "
                "claim_version, created_at, updated_at, swarm_run_id, swarm_json) "
                "VALUES (?, 't', '', '[]', 'normal', 'open', 0, ?, ?, ?, '{}')",
                (task_id, now, now, run_id),
            )
            attempt = dal.open_attempt(
                run_id,
                task_id,
                provider="codex",
                model="gpt-5.6-luna",
                tier="standard",
                effort="high", source="test")
            assert attempt["effort"] == "high"
            live = dal.current_attempt(task_id)
            assert live is not None and live["effort"] == "high"
            assert dal.list_attempts(task_id)[0]["effort"] == "high"

            # Default: no effort recorded (claude/session-default providers).
            dal.close_attempt(str(attempt["id"]), "completed")
            second = dal.open_attempt(run_id, task_id, provider="claude", model="sonnet", source="test")
            assert second["effort"] is None
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# scheduler wiring: decision → durable attempt row + event
# ---------------------------------------------------------------------------


class TestSchedulerRecordsEffort:
    def test_router_decided_effort_lands_on_the_attempt_row(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "a"}])
        h.router.effort = "high"
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            attempts = h.attempts_of("a")
            assert len(attempts) == 1
            assert attempts[0]["effort"] == "high"

            request = h.world.spawn_requests[0]
            assert request.effort == "high"

            assigned = h.emitter.of("task_assigned")
            assert assigned and assigned[0]["effort"] == "high"
        finally:
            h.close()


# ---------------------------------------------------------------------------
# summary: per-task efforts + ledger manifest per attempt
# ---------------------------------------------------------------------------


def _insert_attempt(
    dal: SwarmDal,
    run_id: str,
    board_task_id: str,
    seq: int,
    *,
    end_reason: str | None,
    effort: str | None,
    started_at: str = "2026-07-23T10:00:00Z",
    ended_at: str | None = "2026-07-23T10:05:00Z",
    provider: str = "codex",
    model: str = "gpt-5.6-luna",
) -> str:
    attempt_id = new_id("swa")
    dal._connection.execute(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, tier, "
        "account_id, effort, started_at, ended_at, end_reason, detail) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, 'standard', NULL, ?, ?, ?, ?, '')",
        (
            attempt_id,
            run_id,
            board_task_id,
            seq,
            provider,
            model,
            effort,
            started_at,
            ended_at,
            end_reason,
        ),
    )
    return attempt_id


def _finish_run(dal: SwarmDal, run_id: str, *, status: str = "completed") -> None:
    dal._connection.execute(
        "UPDATE swarm_runs SET started_at = ?, finished_at = ?, status = ? WHERE id = ?",
        ("2026-07-23T10:00:00Z", "2026-07-23T10:30:00Z", status, run_id),
    )


class TestSummarySurfacesEffort:
    def test_compute_metrics_reports_per_task_efforts(self, tmp_path: Path) -> None:
        db = migrated_db(CollabStore, tmp_path / "summary.db")
        dal = SwarmDal(db)
        try:
            run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
            _finish_run(dal, run_id)
            _insert_attempt(dal, run_id, "btk_a", 0, end_reason="timeout", effort="medium")
            _insert_attempt(dal, run_id, "btk_a", 1, end_reason="completed", effort="high")
            _insert_attempt(dal, run_id, "btk_b", 0, end_reason="completed", effort=None)

            metrics = summary.compute_metrics(run_id, dal)
            assert metrics["task_efforts"]["btk_a"] == ["medium", "high"]
            assert metrics["task_efforts"]["btk_b"] == [None]
        finally:
            dal.close()

    def test_ledger_manifest_carries_effort_per_attempt(self, tmp_path: Path) -> None:
        db = migrated_db(CollabStore, tmp_path / "summary.db")
        dal = SwarmDal(db)
        ledger_dir = tmp_path / "ledger"
        try:
            run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
            _finish_run(dal, run_id)
            _insert_attempt(dal, run_id, "btk_a", 0, end_reason="completed", effort="high")

            metrics = summary.write_summary(
                run_id,
                dal=dal,
                vault_dir=str(tmp_path / "vault"),
                ledger_dir=str(ledger_dir),
                fable_runner=lambda *args, **kwargs: None,
            )
            manifest_path = metrics.get("ledger_manifest_path")
            assert manifest_path and Path(manifest_path).is_file()

            lines = [
                json.loads(line)
                for line in Path(manifest_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ours = [line for line in lines if line.get("run_id") == run_id]
            assert len(ours) == 1
            manifest = ours[0]
            assert manifest["discipline"] == "swarm"
            assert manifest["harness"]["harness"] == "swarm"
            attempts = manifest["extra"]["attempts"]
            assert attempts[0]["effort"] == "high"
            assert attempts[0]["provider"] == "codex"
            assert attempts[0]["end_reason"] == "completed"

            # Idempotent per run id: a second summary pass never double-appends.
            summary.write_summary(
                run_id,
                dal=dal,
                vault_dir=str(tmp_path / "vault"),
                ledger_dir=str(ledger_dir),
                fable_runner=lambda *args, **kwargs: None,
            )
            lines_after = [
                json.loads(line)
                for line in Path(manifest_path).read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("run_id") == run_id
            ]
            assert len(lines_after) == 1
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# optimizer: win rate by effort (playbook + learned.json)
# ---------------------------------------------------------------------------


class TestOptimizerSurfacesEffort:
    def test_win_rate_by_effort_in_playbook_and_learned(self, tmp_path: Path) -> None:
        db = migrated_db(CollabStore, tmp_path / "optimize.db")
        dal = SwarmDal(db)
        try:
            run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g", source="test")["id"])
            _finish_run(dal, run_id)
            _insert_attempt(dal, run_id, "btk_a", 0, end_reason="completed", effort="high")
            _insert_attempt(dal, run_id, "btk_b", 0, end_reason="completed", effort="high")
            _insert_attempt(dal, run_id, "btk_c", 0, end_reason="timeout", effort="low")
            _insert_attempt(dal, run_id, "btk_d", 0, end_reason="completed", effort=None)
            # Infra noise is NOT effort evidence: excluded from the win rate.
            _insert_attempt(dal, run_id, "btk_e", 0, end_reason="crashed", effort="high")

            result = optimize.run_optimize(
                dal=dal,
                db_path=db,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "playbook.md"),
                fable_runner=lambda *args, **kwargs: None,
            )
            assert result.runs_analyzed == 1
            assert result.playbook_written and result.learned_written

            playbook = (tmp_path / "playbook.md").read_text(encoding="utf-8")
            assert "### Effort win rates" in playbook
            assert "| high | 2 | 2 | 100% |" in playbook
            assert "| low | 1 | 0 | 0% |" in playbook
            assert "| default | 1 | 1 | 100% |" in playbook

            learned = json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))
            assert learned["win_rate_by_effort"]["high"] == {
                "attempts": 2,
                "wins": 2,
                "win_rate": 1.0,
            }
            assert learned["win_rate_by_effort"]["low"]["win_rate"] == 0.0
        finally:
            dal.close()

    def test_cost_to_green_and_confidence_surface_in_playbook_and_learned(
        self, tmp_path: Path
    ) -> None:
        """Production path: optimizer must invoke costgreen, not leave it dead.

        A fully measured medium task and a tokens-only high task prove that
        cost_per_green unknown stays the word 'unknown' (not 0) and confidence
        bands are readable. Counterfeit: drop the playbook/learned wire and
        costgreen remains built-but-never-called.
        """
        from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY

        db = str(tmp_path / "optimize-ctg.db")
        CollabStore(db)
        dal = SwarmDal(db)
        try:
            run_id = str(dal.create_run(working_dir="/tmp/ws", goal="g")["id"])
            _finish_run(dal, run_id)

            measured_id = _insert_attempt(
                dal, run_id, "btk_measured", 0, end_reason="completed", effort="medium"
            )
            dal.record_attempt_usage(
                measured_id,
                effort="medium",
                cost_usd=4.0,
                input_tokens=100,
                output_tokens=50,
                wall_ms=2_000,
                usage_source=SOURCE_CLI_REPORT,
            )

            tokens_only_id = _insert_attempt(
                dal, run_id, "btk_tokens", 0, end_reason="completed", effort="high"
            )
            dal.record_attempt_usage(
                tokens_only_id,
                effort="high",
                # cost left NULL — tokens-only must never look free
                input_tokens=200,
                output_tokens=100,
                wall_ms=1_000,
                usage_source=SOURCE_TOKENS_ONLY,
            )

            result = optimize.run_optimize(
                dal=dal,
                db_path=db,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "playbook.md"),
                fable_runner=lambda *args, **kwargs: None,
            )
            assert result.playbook_written and result.learned_written

            playbook = (tmp_path / "playbook.md").read_text(encoding="utf-8")
            assert "### Cost-to-green by starting effort" in playbook
            assert "| medium |" in playbook
            assert "| high |" in playbook
            # Measured free-look counterfeit: tokens-only cost must say unknown.
            assert "unknown" in playbook
            assert "measured" in playbook or "sparse" in playbook

            learned = json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))
            by_effort = {row["effort"]: row for row in learned["cost_to_green_by_effort"]}
            medium = by_effort["medium"]
            assert medium["cost_per_green"] == 4.0
            assert medium["tokens_per_green"] == 150.0
            assert medium["confidence"] == "measured"

            high = by_effort["high"]
            assert high["cost_per_green"] is None  # not 0.0
            assert high["tokens_per_green"] == 300.0
            assert high["wall_ms_per_green"] == 1_000.0
            assert high["confidence"] == "sparse"
        finally:
            dal.close()
