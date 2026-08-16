"""M4 regression coverage: project caps, fair shares, and unpriced spend."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.scheduler import _RunState
from omniagentos.swarm.usage_capture import SOURCE_TOKENS_ONLY
from tests.swarm.scheduler_fakes import Harness, make_harness, make_scheduler


def _project(harness: Harness, project_id: str, budget_usd: float | None) -> None:
    now = utc_now_iso()
    harness.dal._connection.execute(
        "INSERT INTO projects (id, name, root_dirs_json, vault_subfolder, budget_usd, "
        "allowed_tools_json, allowed_dirs_json, created_at) VALUES (?, ?, '[]', '', ?, '[]', '[]', ?)",
        (project_id, project_id, budget_usd, now),
    )
    harness.dal._connection.commit()


def _assign_project(harness: Harness, project_id: str) -> None:
    harness.dal._connection.execute(
        "UPDATE swarm_runs SET project_id = ? WHERE id = ?", (project_id, harness.run_id)
    )
    harness.dal._connection.commit()


def _unpriced_terminal_attempt(harness: Harness, task_key: str) -> SessionsDal:
    sessions = SessionsDal(harness.db_path)
    session_id = "ses_m4_unpriced"
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(harness.workdir),
            "provider": "codex",
            "state": "completed",
            "model": "gpt-5.6-sol",
            "cost_usd": None,
        }
    )
    sessions.record_session_usage(
        session_id,
        cost_usd=None,
        input_tokens=10,
        output_tokens=5,
        wall_ms=1,
        usage_source=SOURCE_TOKENS_ONLY,
    )
    attempt = harness.dal.open_attempt(
        harness.run_id,
        harness.task_id(task_key),
        provider="codex",
        source="test",
        model="gpt-5.6-sol",
        session_id=session_id,
    )
    harness.dal.close_attempt(str(attempt["id"]), "completed")
    return sessions


def test_project_cap_blocks_only_its_project_in_block_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(tmp_path, [{"id": "spent"}, {"id": "next"}], integration=False)
    try:
        _project(harness, "proj_capped", 4.0)
        _assign_project(harness, "proj_capped")
        harness.dal.add_cost(harness.run_id, 4.0)
        scheduler = make_scheduler(harness)
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))

        disposition = scheduler._execute_task(state, 0, harness.task_row("next"))

        assert disposition == "requeue"
        assert harness.world.spawn_order == []
        kind, issue = state.signals.get_nowait()
        assert kind == "budget"
        assert issue["cap_scope"] == "project"
        assert issue["reason"] == "project_budget_cap_reached"
        assert issue["project_id"] == "proj_capped"

        _project(harness, "proj_open", 4.0)
        open_run = harness.dal.create_run(
            working_dir=str(harness.workdir), goal="open", project_id="proj_open", status="running"
        )
        assert scheduler._budget_issue(open_run) is None
    finally:
        harness.close()


def test_fair_share_is_deterministic_and_excludes_capped_projects(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, [{"id": "a"}], integration=False)
    try:
        _project(harness, "proj_capped", 5.0)
        _project(harness, "proj_beta", 10.0)
        _project(harness, "proj_alpha", None)
        _assign_project(harness, "proj_capped")
        harness.dal.add_cost(harness.run_id, 5.0)
        for project_id in ("proj_beta", "proj_alpha"):
            harness.dal.create_run(
                working_dir=str(harness.workdir),
                goal=project_id,
                project_id=project_id,
                status="running",
            )
        now = utc_now_iso()
        harness.dal._connection.execute(
            "INSERT INTO budgets (id, scope_type, scope_id, cost_usd_max, used_cost_usd, updated_at) "
            "VALUES ('global', 'global', '', 100.0, 40.0, ?)",
            (now,),
        )
        harness.dal._connection.commit()

        assert harness.dal.active_projects() == ["proj_alpha", "proj_beta", "proj_capped"]
        assert harness.dal.fair_share_allocation() == {
            "proj_alpha": pytest.approx(30.0),
            "proj_beta": pytest.approx(30.0),
        }
    finally:
        harness.close()


def test_unpriced_project_spend_is_never_free_and_blocks_in_block_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(tmp_path, [{"id": "priced"}, {"id": "next"}], integration=False)
    sessions: SessionsDal | None = None
    try:
        _project(harness, "proj_unpriced", 4.0)
        _assign_project(harness, "proj_unpriced")
        sessions = _unpriced_terminal_attempt(harness, "priced")
        spend = harness.dal.project_budget_spend("proj_unpriced")
        assert spend.known_cost_usd == 0.0
        assert spend.cost_usd is None
        assert spend.unknown_cost_sessions == 1

        scheduler = make_scheduler(harness)
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        assert scheduler._execute_task(state, 0, harness.task_row("next")) == "requeue"
        _, issue = state.signals.get_nowait()
        assert issue["cap_scope"] == "project"
        assert issue["reason"] == "cost_unknown"
        assert issue["cost_usd"] is None
    finally:
        if sessions is not None:
            sessions.close()
        harness.close()


def test_project_cap_is_advisory_when_block_mode_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    harness = make_harness(tmp_path, [{"id": "spent"}, {"id": "next"}], integration=False)
    try:
        _project(harness, "proj_advisory", 1.0)
        _assign_project(harness, "proj_advisory")
        harness.dal.add_cost(harness.run_id, 1.0)
        scheduler = make_scheduler(harness)
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))

        assert scheduler._execute_task(state, 0, harness.task_row("next")) != "requeue"
        kind, issue = state.signals.get_nowait()
        assert kind == "budget"
        assert issue["cap_scope"] == "project"
        assert harness.world.spawn_order == ["next"]
    finally:
        harness.close()
