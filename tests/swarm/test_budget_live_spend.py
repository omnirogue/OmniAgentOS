"""Dollar caps read the live attempt/session ledger, not the lagging run cache."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.notifications import service as notification_service
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.scheduler import _RunState
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY
from tests.swarm.scheduler_fakes import Harness, make_harness, make_scheduler


def _record_session_attempt(
    harness: Harness,
    *,
    task_key: str,
    session_id: str,
    cost_usd: float | None,
    usage_source: str,
    terminal: bool = True,
) -> SessionsDal:
    sessions = SessionsDal(harness.db_path)
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(harness.workdir),
            "provider": "codex" if usage_source == SOURCE_TOKENS_ONLY else "claude",
            "state": "completed" if terminal else "running",
            "model": "gpt-5.6-sol" if usage_source == SOURCE_TOKENS_ONLY else "opus",
            # Reproduce the creation-time seed still present in older callers.
            "cost_usd": 0.0,
        }
    )
    sessions.record_session_usage(
        session_id,
        cost_usd=cost_usd,
        input_tokens=18_000,
        output_tokens=3_000,
        wall_ms=30_000,
        usage_source=usage_source,
    )
    attempt = harness.dal.open_attempt(
        harness.run_id,
        harness.task_id(task_key),
        provider="codex" if usage_source == SOURCE_TOKENS_ONLY else "claude",
        source="test",
        model="gpt-5.6-sol" if usage_source == SOURCE_TOKENS_ONLY else "opus",
        session_id=session_id,
    )
    if terminal:
        harness.dal.close_attempt(str(attempt["id"]), "completed")
        harness.collab.update_board_task(harness.task_id(task_key), {"status": "done"})
    return sessions


def _execute_next(harness: Harness, task_key: str) -> tuple[str, _RunState]:
    scheduler = make_scheduler(harness)
    harness.dal.set_run_status(harness.run_id, "running")
    state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
    return scheduler._execute_task(state, 0, harness.task_row(task_key)), state


def _budget_signal(state: _RunState) -> dict[str, Any]:
    kind, payload = state.signals.get_nowait()
    assert kind == "budget"
    assert isinstance(payload, dict)
    return payload


def test_pre_spawn_gate_blocks_on_live_session_spend_when_run_cost_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live $6.33 ledger blocks a $4 spawn even while run.cost_usd is $0."""
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path,
        [{"id": "spent"}, {"id": "next"}],
        integration=False,
        budget=4.0,
        max_concurrency=1,
    )
    sessions = _record_session_attempt(
        harness,
        task_key="spent",
        session_id="ses_priced",
        cost_usd=6.3336,
        usage_source=SOURCE_CLI_REPORT,
        terminal=False,
    )
    try:
        assert harness.dal.get_run(harness.run_id)["cost_usd"] == 0.0

        disposition, state = _execute_next(harness, "next")

        assert disposition == "requeue"
        assert harness.world.spawn_order == []
        signal = _budget_signal(state)
        assert signal["reason"] == "budget_cap_reached"
        assert signal["known_cost_usd"] == pytest.approx(6.3336)
        assert signal["unknown_cost_sessions"] == 0
    finally:
        sessions.close()
        harness.close()


def test_unknown_session_cost_makes_total_unknown_instead_of_zero(
    tmp_path: Path,
) -> None:
    """A priced total is NULL while any token-bearing session lacks a dollar price."""
    harness = make_harness(
        tmp_path,
        [{"id": "priced"}, {"id": "unpriced"}],
        integration=False,
        budget=4.0,
    )
    priced = _record_session_attempt(
        harness,
        task_key="priced",
        session_id="ses_known",
        cost_usd=1.25,
        usage_source=SOURCE_CLI_REPORT,
    )
    unpriced = _record_session_attempt(
        harness,
        task_key="unpriced",
        session_id="ses_unknown",
        cost_usd=None,
        usage_source=SOURCE_TOKENS_ONLY,
    )
    try:
        row = unpriced.get_session("ses_unknown")
        assert row is not None
        assert row["cost_usd"] is None

        spend = harness.dal.budget_spend(harness.run_id)

        assert spend.known_cost_usd == pytest.approx(1.25)
        assert spend.unknown_cost_sessions == 1
        # Policy contract: an incomplete dollar total is not compared as $1.25
        # or $0.00. It is explicitly unknown and therefore unenforceable.
        assert spend.cost_usd is None
    finally:
        priced.close()
        unpriced.close()
        harness.close()


def test_running_session_without_final_price_is_not_yet_unknown(
    tmp_path: Path,
) -> None:
    """An in-flight session becomes unknown only if it terminalizes unpriced."""
    harness = make_harness(
        tmp_path,
        [{"id": "running"}],
        integration=False,
        budget=4.0,
    )
    sessions = _record_session_attempt(
        harness,
        task_key="running",
        session_id="ses_running_unpriced",
        cost_usd=None,
        usage_source=SOURCE_TOKENS_ONLY,
        terminal=False,
    )
    try:
        spend = harness.dal.budget_spend(harness.run_id)

        assert spend.known_cost_usd == 0.0
        assert spend.unknown_cost_sessions == 0
        assert spend.cost_usd == 0.0
    finally:
        sessions.close()
        harness.close()


def test_block_mode_refuses_spawn_for_cli_only_unknown_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCK fails closed when every session is unpriced; unknown never means free."""
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path,
        [{"id": "cli"}, {"id": "next"}],
        integration=False,
        budget=4.0,
        max_concurrency=1,
    )
    sessions = _record_session_attempt(
        harness,
        task_key="cli",
        session_id="ses_cli_only",
        cost_usd=None,
        usage_source=SOURCE_TOKENS_ONLY,
    )
    try:
        # Reproduce the measured live legacy row exactly: the source proves the
        # provider supplied tokens only, but an old creation seed survived as
        # 0.0. The gate must honor the source marker rather than counterfeit a
        # free run.
        sessions._connection.execute(
            "UPDATE sessions SET cost_usd = 0.0 WHERE id = ?",
            ("ses_cli_only",),
        )
        legacy = sessions.get_session("ses_cli_only")
        assert legacy is not None
        assert legacy["usage_source"] == SOURCE_TOKENS_ONLY
        assert legacy["cost_usd"] == 0.0

        disposition, state = _execute_next(harness, "next")

        assert disposition == "requeue"
        assert harness.world.spawn_order == []
        signal = _budget_signal(state)
        assert signal["reason"] == "cost_unknown"
        assert signal["known_cost_usd"] == 0.0
        assert signal["cost_usd"] is None
        assert signal["unknown_cost_sessions"] == 1
    finally:
        sessions.close()
        harness.close()


def test_advisory_mode_surfaces_unknown_spend_but_still_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADVISORY keeps its existing proceed policy while making the hole loud."""
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    harness = make_harness(
        tmp_path,
        [{"id": "cli"}, {"id": "next"}],
        integration=False,
        budget=4.0,
        max_concurrency=1,
    )
    sessions = _record_session_attempt(
        harness,
        task_key="cli",
        session_id="ses_cli_advisory",
        cost_usd=None,
        usage_source=SOURCE_TOKENS_ONLY,
    )
    try:
        monkeypatch.setenv("OMNIAGENTOS_DB", harness.db_path)
        monkeypatch.setattr(notification_service, "_push", lambda *args, **kwargs: None)
        scheduler = make_scheduler(harness)
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert harness.world.spawn_order == ["next"]
        warnings = harness.emitter.of("budget_unenforceable")
        assert len(warnings) == 1
        assert warnings[0]["reason"] == "cost_unknown"
        assert warnings[0]["unknown_cost_sessions"] == 1

        notifications = [
            row
            for row in NotificationsDal(harness.db_path).list()
            if row["ref_type"] == "run" and row["ref_id"] == harness.run_id
        ]
        assert len(notifications) == 1
        assert (
            "budget unenforceable: cost unknown for 1 session" in notifications[0]["title"].lower()
        )
        completed = harness.emitter.of("run_completed")[0]
        assert completed["budget_unenforceable"] is True
        assert completed["budget_exhausted"] is False
        assert completed["cost_usd"] is None
    finally:
        sessions.close()
        harness.close()
