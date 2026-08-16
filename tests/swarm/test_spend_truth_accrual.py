"""C4: measured tokens accrue toward dollar caps without manufacturing a cost.

Before this, a provider that reports tokens and no price (``codex exec --json``)
left ``sessions.cost_usd`` NULL — honest — and therefore contributed **nothing**
to any cap. ``known_cost_usd`` read $0 forever, so a codex-only fleet ran against
a ceiling it could never consume.

The fix is a SECOND column, never the same one. ``cost_usd`` keeps its invariant::

    NULL = unpriced / unknown        0.0 = the provider said this run was free

and ``sessions.cost_estimate_usd`` (migration 118) carries an upper-bound price
for the MEASURED tokens. Cap accrual reads the estimate; every truth-telling
surface keeps reading the honest NULL. These tests pin both halves: that the
estimate now stops a run, and that it never turns an unknown total into a number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.budget.token_pricing import QUALITY_ESTIMATED, QUALITY_EXACT
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.scheduler import _RunState
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY
from tests.swarm.scheduler_fakes import Harness, make_harness, make_scheduler

#: 18k prompt + 3k completion on these rates is exactly $0.18.
PRICED_MODEL = "gpt-5.6-sol"
UNPRICEABLE_MODEL = "some-unlisted-local-model"
INPUT_TOKENS = 18_000
OUTPUT_TOKENS = 3_000
EXPECTED_ESTIMATE = 0.18

_REGISTRY: dict[str, Any] = {
    "schema_version": 1,
    "updated_at": "2026-08-04T11:15:05Z",
    "models": [
        {
            "key": PRICED_MODEL,
            "pricing": {
                "prompt_usd_per_m": 5.0,
                "completion_usd_per_m": 30.0,
                "as_of": "2026-08-04T11:15:05Z",
                "source": "openrouter",
            },
        }
    ],
}


@pytest.fixture
def priced_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the pricing lookup at a fixed registry, exactly as production does."""
    registry_dir = tmp_path / "modelintel"
    registry_dir.mkdir()
    path = registry_dir / "registry.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_MODELINTEL_DIR", str(registry_dir))
    return path


def _session(
    harness: Harness,
    sessions: SessionsDal,
    *,
    session_id: str,
    model: str = PRICED_MODEL,
    cost_usd: float | None = None,
    usage_source: str = SOURCE_TOKENS_ONLY,
    terminal: bool = True,
) -> None:
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(harness.workdir),
            "provider": "codex",
            "state": "completed" if terminal else "running",
            "model": model,
            # The creation-time seed older callers still write.
            "cost_usd": 0.0,
        }
    )
    sessions.record_session_usage(
        session_id,
        cost_usd=cost_usd,
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
        wall_ms=30_000,
        usage_source=usage_source,
    )


def _attempt(harness: Harness, task_key: str, session_id: str, *, terminal: bool = True) -> None:
    attempt = harness.dal.open_attempt(
        harness.run_id,
        harness.task_id(task_key),
        provider="codex",
        source="test",
        model=PRICED_MODEL,
        session_id=session_id,
    )
    if terminal:
        harness.dal.close_attempt(str(attempt["id"]), "completed")
        harness.collab.update_board_task(harness.task_id(task_key), {"status": "done"})


def _budget_signal(state: _RunState) -> dict[str, Any]:
    kind, payload = state.signals.get_nowait()
    assert kind == "budget"
    assert isinstance(payload, dict)
    return payload


# --- the write half: an estimate beside the NULL, never inside it -------------


def test_tokens_only_gets_an_estimate_while_cost_usd_stays_null(
    tmp_path: Path, priced_registry: Path
) -> None:
    """The whole point: a price to accrue AND an untouched unpriced marker."""
    harness = make_harness(tmp_path / "h", [{"id": "a"}], integration=False, budget=4.0)
    sessions = SessionsDal(harness.db_path)
    try:
        _session(harness, sessions, session_id="ses_tokens_only")

        row = sessions.get_session("ses_tokens_only")
        assert row is not None
        # The invariant attempts 1 and 2 broke — still intact.
        assert row["cost_usd"] is None
        assert row["cost_usd"] != 0.0
        assert row["usage_source"] == SOURCE_TOKENS_ONLY
        # ...and the accrual number lives in its own column, with provenance.
        assert row["cost_estimate_usd"] == pytest.approx(EXPECTED_ESTIMATE)
        assert row["cost_estimate_source"] == "modelintel:gpt-5.6-sol@2026-08-04T11:15:05Z"
    finally:
        sessions.close()
        harness.close()


def test_explicit_zero_cost_is_not_overwritten_by_an_estimate(
    tmp_path: Path, priced_registry: Path
) -> None:
    """A genuine free run is already exact; estimating it would be a downgrade."""
    harness = make_harness(tmp_path / "h", [{"id": "a"}], integration=False, budget=4.0)
    sessions = SessionsDal(harness.db_path)
    try:
        _session(
            harness,
            sessions,
            session_id="ses_free",
            cost_usd=0.0,
            usage_source=SOURCE_CLI_REPORT,
        )

        row = sessions.get_session("ses_free")
        assert row is not None
        assert row["cost_usd"] == 0.0
        assert row["cost_estimate_usd"] is None
        assert row["cost_estimate_source"] is None
    finally:
        sessions.close()
        harness.close()


def test_priced_session_is_not_double_counted_by_an_estimate(
    tmp_path: Path, priced_registry: Path
) -> None:
    """An exactly-priced session contributes to known_cost_usd only."""
    harness = make_harness(tmp_path / "h", [{"id": "a"}], integration=False, budget=4.0)
    sessions = SessionsDal(harness.db_path)
    try:
        _session(
            harness,
            sessions,
            session_id="ses_priced",
            cost_usd=1.25,
            usage_source=SOURCE_CLI_REPORT,
        )
        _attempt(harness, "a", "ses_priced")

        spend = harness.dal.budget_spend(harness.run_id)

        assert spend.known_cost_usd == pytest.approx(1.25)
        assert spend.estimated_cost_usd == 0.0
        assert spend.accrued_cost_usd == pytest.approx(1.25)
    finally:
        sessions.close()
        harness.close()


def test_unpriceable_model_stays_unpriced_and_accrues_nothing(
    tmp_path: Path, priced_registry: Path
) -> None:
    """No published rate means no number at all — never a manufactured zero."""
    harness = make_harness(tmp_path / "h", [{"id": "a"}], integration=False, budget=4.0)
    sessions = SessionsDal(harness.db_path)
    try:
        _session(harness, sessions, session_id="ses_unlisted", model=UNPRICEABLE_MODEL)
        _attempt(harness, "a", "ses_unlisted")

        row = sessions.get_session("ses_unlisted")
        assert row is not None
        assert row["cost_usd"] is None
        assert row["cost_estimate_usd"] is None

        spend = harness.dal.budget_spend(harness.run_id)
        assert spend.estimated_cost_usd == 0.0
        assert spend.unknown_cost_sessions == 1
        assert spend.cost_usd is None
    finally:
        sessions.close()
        harness.close()


# --- the read half: accrual without turning unknown into a number ------------


def test_estimate_accrues_while_the_honest_total_stays_unknown(
    tmp_path: Path, priced_registry: Path
) -> None:
    """The two halves of C4 in one observation, on one spend record."""
    harness = make_harness(
        tmp_path / "h", [{"id": "priced"}, {"id": "unpriced"}], integration=False, budget=4.0
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _session(
            harness,
            sessions,
            session_id="ses_known",
            cost_usd=1.25,
            usage_source=SOURCE_CLI_REPORT,
        )
        _attempt(harness, "priced", "ses_known")
        _session(harness, sessions, session_id="ses_unknown")
        _attempt(harness, "unpriced", "ses_unknown")

        spend = harness.dal.budget_spend(harness.run_id)

        # Truth-telling surface: unchanged, still refuses to invent a total.
        assert spend.known_cost_usd == pytest.approx(1.25)
        assert spend.unknown_cost_sessions == 1
        assert spend.cost_usd is None
        # Cap-accrual surface: the measured tokens now count for something.
        assert spend.estimated_cost_usd == pytest.approx(EXPECTED_ESTIMATE)
        assert spend.estimated_cost_sessions == 1
        assert spend.accrued_cost_usd == pytest.approx(1.25 + EXPECTED_ESTIMATE)
    finally:
        sessions.close()
        harness.close()


def test_running_session_tokens_accrue_before_it_terminalizes(
    tmp_path: Path, priced_registry: Path
) -> None:
    """Tokens an in-flight session already burned are already spent."""
    harness = make_harness(tmp_path / "h", [{"id": "running"}], integration=False, budget=4.0)
    sessions = SessionsDal(harness.db_path)
    try:
        _session(harness, sessions, session_id="ses_running", terminal=False)
        _attempt(harness, "running", "ses_running", terminal=False)

        spend = harness.dal.budget_spend(harness.run_id)

        # Unchanged: a running session is not yet permanently unpriced.
        assert spend.unknown_cost_sessions == 0
        assert spend.cost_usd == 0.0
        # New: its measured tokens still accrue against the ceiling.
        assert spend.estimated_cost_usd == pytest.approx(EXPECTED_ESTIMATE)
        assert spend.accrued_cost_usd == pytest.approx(EXPECTED_ESTIMATE)
    finally:
        sessions.close()
        harness.close()


# --- the gate: a cap that unpriced spend can finally reach --------------------


def test_estimated_spend_reaches_a_run_cap_that_known_dollars_never_would(
    tmp_path: Path, priced_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed money hole.

    A running, unpriced session produces ``unknown_cost_sessions == 0`` and
    ``known_cost_usd == 0.0``, so before C4 the gate returned NO issue at all
    and the fleet kept spawning past its ceiling. The priced tokens now carry
    the breach — reported as ``estimated``, with the total still not a number.
    """
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path / "h",
        [{"id": "burner"}, {"id": "next"}],
        integration=False,
        budget=0.10,  # below the $0.18 the tokens are worth
        max_concurrency=1,
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _session(harness, sessions, session_id="ses_burner", terminal=False)
        _attempt(harness, "burner", "ses_burner", terminal=False)

        spend = harness.dal.budget_spend(harness.run_id)
        assert spend.known_cost_usd == 0.0
        assert spend.unknown_cost_sessions == 0

        scheduler = make_scheduler(harness)
        harness.dal.set_run_status(harness.run_id, "running")
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        disposition = scheduler._execute_task(state, 0, harness.task_row("next"))

        assert disposition == "requeue"
        assert harness.world.spawn_order == []
        signal = _budget_signal(state)
        assert signal["reason"] == "budget_cap_reached"
        assert signal["cost_quality"] == QUALITY_ESTIMATED
        assert signal["known_cost_usd"] == 0.0
        assert signal["estimated_cost_usd"] == pytest.approx(EXPECTED_ESTIMATE)
        assert signal["accrued_cost_usd"] == pytest.approx(EXPECTED_ESTIMATE)
        # Enforceable now — but still not a measured total.
        assert signal["cost_usd"] == 0.0 or signal["cost_usd"] is None
    finally:
        sessions.close()
        harness.close()


def test_exact_dollars_still_outrank_an_estimate_in_the_gate(
    tmp_path: Path, priced_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measured breach is still reported as measured, not downgraded."""
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path / "h",
        [{"id": "spent"}, {"id": "next"}],
        integration=False,
        budget=1.0,
        max_concurrency=1,
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _session(
            harness,
            sessions,
            session_id="ses_expensive",
            cost_usd=6.3336,
            usage_source=SOURCE_CLI_REPORT,
            terminal=False,
        )
        _attempt(harness, "spent", "ses_expensive", terminal=False)

        scheduler = make_scheduler(harness)
        harness.dal.set_run_status(harness.run_id, "running")
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        disposition = scheduler._execute_task(state, 0, harness.task_row("next"))

        assert disposition == "requeue"
        signal = _budget_signal(state)
        assert signal["reason"] == "budget_cap_reached"
        assert signal["cost_quality"] == QUALITY_EXACT
        assert signal["known_cost_usd"] == pytest.approx(6.3336)
    finally:
        sessions.close()
        harness.close()


def test_worker_headroom_is_reduced_by_accrued_token_spend(
    tmp_path: Path, priced_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accrual is real: the next worker's allowance shrinks by the estimate."""
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    harness = make_harness(
        tmp_path / "h",
        [{"id": "burner"}, {"id": "next"}],
        integration=False,
        budget=4.0,
        max_concurrency=2,
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _session(harness, sessions, session_id="ses_burner", terminal=False)
        _attempt(harness, "burner", "ses_burner", terminal=False)

        scheduler = make_scheduler(harness)
        harness.dal.set_run_status(harness.run_id, "running")
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        scheduler._execute_task(state, 0, harness.task_row("next"))

        assert harness.world.spawn_requests, "expected the advisory path to still spawn"
        request = harness.world.spawn_requests[-1]
        assert request.budget_usd_max == pytest.approx(4.0 - EXPECTED_ESTIMATE)
    finally:
        sessions.close()
        harness.close()


# --- M4 interlock: unknown is still unenforceable, never zero ----------------


def _project(harness: Harness, project_id: str, budget_usd: float | None) -> None:
    harness.dal._connection.execute(
        "INSERT INTO projects (id, name, root_dirs_json, vault_subfolder, budget_usd, "
        "allowed_tools_json, allowed_dirs_json, created_at) "
        "VALUES (?, ?, '[]', '', ?, '[]', '[]', ?)",
        (project_id, project_id, budget_usd, utc_now_iso()),
    )
    harness.dal._connection.execute(
        "UPDATE swarm_runs SET project_id = ? WHERE id = ?", (project_id, harness.run_id)
    )
    harness.dal._connection.commit()


def test_project_cap_accrues_the_estimate_without_making_unknown_spend_zero(
    tmp_path: Path, priced_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4's rule holds: unknown blocks because it is unenforceable, not free."""
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path / "h", [{"id": "burner"}, {"id": "next"}], integration=False, max_concurrency=1
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _project(harness, "proj_c4", 0.10)
        _session(harness, sessions, session_id="ses_proj_burner", terminal=False)
        _attempt(harness, "burner", "ses_proj_burner", terminal=False)

        spend = harness.dal.project_budget_spend("proj_c4")
        assert spend.known_cost_usd == 0.0
        assert spend.estimated_cost_usd == pytest.approx(EXPECTED_ESTIMATE)
        assert spend.accrued_cost_usd == pytest.approx(EXPECTED_ESTIMATE)

        scheduler = make_scheduler(harness)
        harness.dal.set_run_status(harness.run_id, "running")
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        disposition = scheduler._execute_task(state, 0, harness.task_row("next"))

        assert disposition == "requeue"
        signal = _budget_signal(state)
        assert signal["cap_scope"] == "project"
        assert signal["reason"] == "project_budget_cap_reached"
        assert signal["cost_quality"] == QUALITY_ESTIMATED
        assert signal["estimated_cost_usd"] == pytest.approx(EXPECTED_ESTIMATE)
    finally:
        sessions.close()
        harness.close()


def test_unpriceable_project_spend_is_still_blocked_as_unenforceable(
    tmp_path: Path, priced_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No estimate available: M4's fail-closed path is untouched by C4."""
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    harness = make_harness(
        tmp_path / "h", [{"id": "burner"}, {"id": "next"}], integration=False, max_concurrency=1
    )
    sessions = SessionsDal(harness.db_path)
    try:
        _project(harness, "proj_c4_unknown", 4.0)
        _session(harness, sessions, session_id="ses_proj_unlisted", model=UNPRICEABLE_MODEL)
        _attempt(harness, "burner", "ses_proj_unlisted")

        scheduler = make_scheduler(harness)
        harness.dal.set_run_status(harness.run_id, "running")
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        disposition = scheduler._execute_task(state, 0, harness.task_row("next"))

        assert disposition == "requeue"
        signal = _budget_signal(state)
        assert signal["reason"] == "cost_unknown"
        assert signal["cost_usd"] is None
        assert signal["unknown_cost_sessions"] == 1
        assert signal["estimated_cost_usd"] == 0.0
    finally:
        sessions.close()
        harness.close()
