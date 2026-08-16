"""The improvement dispatcher must consult the $200/UTC-day hard cap.

The guard in ``omniagentos/improve/budget.py`` was complete and UNWIRED: nothing
on the dispatch path called it, so the ceiling protected nothing. These tests pin
the seam (``tick`` -> ``CALL_RESERVED`` insert), both enforcement postures, and
the UTC-day rollover that re-arms the window.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from omniagentos import simgate
from omniagentos.db.store import SqliteStore
from omniagentos.improve import dispatcher as dispatcher_mod
from omniagentos.improve.budget import BudgetLedger
from omniagentos.improve.dispatcher import (
    IMPROVE_APPROVAL_ACTION_CLASS,
    IMPROVE_BUDGET_DB_ENV,
    IMPROVE_BUDGET_POOL,
    IMPROVE_DISPATCH_ESTIMATE_USD,
    ImproveTest,
    tick,
)
from tests.support.db_template import migrated_db

DISPATCH_LOGGER = "omniagentos.improve.dispatcher"
ON_ENV = {"OMNIAGENTOS_IMPROVE_MODE": "on"}
# One UTC day, in epoch seconds.
DAY = 86400.0
# An epoch that is exactly a UTC midnight, so "same day" arithmetic is obvious.
DAY_START = 1700000000.0 - (1700000000.0 % DAY)


class FakeClock:
    def __init__(self, initial_time: float = DAY_START + 3600.0) -> None:
        self._now = initial_time

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeRefs:
    """HEAD exists and is its own ancestor; no saga blocks the queue."""

    def head_sha(self) -> str:
        return "head"

    def commit_exists(self, sha: str) -> bool:
        return sha == "head"

    def is_ancestor(self, sha: str, descendant: str) -> bool:
        return sha == descendant


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db = migrated_db(SqliteStore, tmp_path / "improve.db")
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    # The improve lane only dispatches for real with a human approval on file.
    connection.execute(
        "INSERT INTO approvals (id, action_class, state, decided_by, expires_at, created_at, "
        "proposed_action) VALUES ('apr_budget', ?, 'approved', 'owner', NULL, "
        "'2026-07-27T00:00:00Z', '')",
        (IMPROVE_APPROVAL_ACTION_CLASS,),
    )
    yield connection
    connection.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifications() -> list[tuple[str, str, float]]:
    return []


@pytest.fixture
def ledger(
    tmp_path: Path, clock: FakeClock, notifications: list[tuple[str, str, float]]
) -> Generator[BudgetLedger, None, None]:
    """A ledger whose whole day fits exactly ONE dispatch.

    Sizing the ceiling to the dispatch estimate is what lets a two-tick test
    exercise "under cap" and "at cap" without simulating a $200 day.
    """
    instance = BudgetLedger(
        tmp_path / "budget.sqlite3",
        pool_caps={IMPROVE_BUDGET_POOL: IMPROVE_DISPATCH_ESTIMATE_USD},
        ceiling_usd=IMPROVE_DISPATCH_ESTIMATE_USD,
        clock=clock,
        notifier=lambda pool, kind, fraction: notifications.append((pool, kind, fraction)),
    )
    yield instance
    instance.close()


def _tick(
    connection: sqlite3.Connection,
    tmp_path: Path,
    *,
    test_id: str,
    now: str,
    ledger: BudgetLedger | None,
) -> dict[str, object]:
    return tick(
        connection,
        refs=FakeRefs(),
        inbox=tmp_path / "inbox",
        tests=[ImproveTest(test_id, frozenset({test_id}))],
        env=ON_ENV,
        now=now,
        ledger=ledger,
    )


def _saga_ids(connection: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in connection.execute("SELECT attempt_id FROM improve_saga").fetchall()]


def test_under_cap_dispatch_proceeds_and_charges_the_day(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
    ledger: BudgetLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")

    res = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=ledger)

    assert res["mode"] == "on"
    assert res["dispatched"] == ["t1"]
    assert res["budget_blocked"] == []
    assert res["budget_overshot"] is False
    assert res["budget_unenforceable"] is False
    assert _saga_ids(db_conn) == ["att_t1_2026-07-27T01:00:00Z"]

    # The admitted dispatch is SETTLED, not left as a hold that expires and
    # quietly forgets the day's spend.
    state = ledger.pool_state(IMPROVE_BUDGET_POOL)
    assert state.settled_usd == pytest.approx(IMPROVE_DISPATCH_ESTIMATE_USD)
    assert state.outstanding_usd == pytest.approx(0.0)


def test_at_cap_blocks_dispatch_and_writes_nothing(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
    ledger: BudgetLedger,
    notifications: list[tuple[str, str, float]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")

    first = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=ledger)
    assert first["dispatched"] == ["t1"]
    # Reaching the ceiling trips the ledger's own pause + notify path.
    assert ledger.paused is True
    assert ("workers", "pause", pytest.approx(1.0)) in notifications

    with caplog.at_level(logging.ERROR, logger=DISPATCH_LOGGER):
        second = _tick(db_conn, tmp_path, test_id="t2", now="2026-07-27T01:05:00Z", ledger=ledger)

    assert second["dispatched"] == []
    assert second["budget_blocked"] == ["t2"]
    assert second["budget_overshot"] is True
    assert second["budget_enforcement"] == "block"
    # Zero runs dispatched means zero rows: the refusal happens BEFORE the write.
    assert _saga_ids(db_conn) == ["att_t1_2026-07-27T01:00:00Z"]
    assert ledger.pool_state(IMPROVE_BUDGET_POOL).committed_usd == pytest.approx(
        IMPROVE_DISPATCH_ESTIMATE_USD
    )

    loud = [r.getMessage() for r in caplog.records if r.name == DISPATCH_LOGGER]
    assert any("REFUSED" in m and "t2" in m and "enforcement=block" in m for m in loud)


def test_at_cap_is_advisory_by_default_and_still_dispatches(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
    ledger: BudgetLedger,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The house default is advisory (``budget.policy``); wiring must not flip it."""
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)

    first = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=ledger)
    assert first["budget_enforcement"] == "advisory"
    assert first["dispatched"] == ["t1"]
    assert ledger.paused is True

    with caplog.at_level(logging.ERROR, logger=DISPATCH_LOGGER):
        second = _tick(db_conn, tmp_path, test_id="t2", now="2026-07-27T01:05:00Z", ledger=ledger)

    assert second["dispatched"] == ["t2"]
    assert second["budget_blocked"] == []
    assert second["budget_overshot"] is True
    assert len(_saga_ids(db_conn)) == 2

    loud = [r.getMessage() for r in caplog.records if r.name == DISPATCH_LOGGER]
    assert any("OVER CAP but PROCEEDING" in m and "enforcement=advisory" in m for m in loud)


def test_utc_day_rollover_reopens_the_window(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
    ledger: BudgetLedger,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")

    assert _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=ledger)[
        "dispatched"
    ] == ["t1"]
    assert _tick(db_conn, tmp_path, test_id="t2", now="2026-07-27T01:05:00Z", ledger=ledger)[
        "budget_blocked"
    ] == ["t2"]

    # Same clock, next UTC day: yesterday's spend and yesterday's pause both
    # fall out of the window with no explicit resume().
    clock.advance(DAY)
    assert ledger.paused is False

    third = _tick(db_conn, tmp_path, test_id="t3", now="2026-07-28T01:00:00Z", ledger=ledger)
    assert third["dispatched"] == ["t3"]
    assert third["budget_blocked"] == []
    assert third["budget_overshot"] is False
    assert "att_t3_2026-07-28T01:00:00Z" in _saga_ids(db_conn)
    # Only today's dispatch is committed against today's ceiling.
    assert ledger.pool_state(IMPROVE_BUDGET_POOL).committed_usd == pytest.approx(
        IMPROVE_DISPATCH_ESTIMATE_USD
    )


def test_shadow_mode_never_reserves(
    db_conn: sqlite3.Connection, tmp_path: Path, ledger: BudgetLedger
) -> None:
    """Shadow queues nothing, so it must not spend the day's budget."""
    res = tick(
        db_conn,
        refs=FakeRefs(),
        inbox=tmp_path / "inbox",
        tests=[ImproveTest("t1", frozenset({"t1"}))],
        env={"OMNIAGENTOS_IMPROVE_MODE": "shadow"},
        now="2026-07-27T01:00:00Z",
        ledger=ledger,
    )

    assert res["mode"] == "shadow"
    assert res["dispatched"] == ["t1"]
    assert _saga_ids(db_conn) == []
    assert ledger.pool_state(IMPROVE_BUDGET_POOL).committed_usd == pytest.approx(0.0)


def test_tick_arms_the_default_ledger_when_none_is_injected(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap must not depend on the caller remembering to pass a ledger."""
    ledger_db = tmp_path / "default" / "budget.sqlite3"
    monkeypatch.setenv(IMPROVE_BUDGET_DB_ENV, str(ledger_db))
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")

    res = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=None)

    assert res["dispatched"] == ["t1"]
    assert res["budget_unenforceable"] is False
    assert ledger_db.exists()

    opened = BudgetLedger(ledger_db)
    try:
        assert opened.pool_state(IMPROVE_BUDGET_POOL).settled_usd == pytest.approx(
            IMPROVE_DISPATCH_ESTIMATE_USD
        )
    finally:
        opened.close()


def test_default_ledger_path_preserves_var_dir_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(IMPROVE_BUDGET_DB_ENV, raising=False)
    var_root = tmp_path / "var-first"
    var_dir_root = tmp_path / "var-dir-first"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir_root))

    assert dispatcher_mod._improve_budget_db_path() == (
        var_dir_root / "improve" / "budget.sqlite3"
    )


def test_default_ledger_refuses_a_simulation_checkout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / "budget-path"
    campaign_root.mkdir(parents=True)
    monkeypatch.delenv(IMPROVE_BUDGET_DB_ENV, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "budget-path")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))

    with pytest.raises(simgate.SimGateError):
        dispatcher_mod._improve_budget_db_path()
    assert dispatcher_mod._default_budget_ledger() is None


def test_unusable_ledger_fails_closed_under_block_and_open_under_advisory(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cap that cannot be consulted is unenforceable, and says so."""
    monkeypatch.setattr(dispatcher_mod, "_default_budget_ledger", lambda: None)

    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    with caplog.at_level(logging.ERROR, logger=DISPATCH_LOGGER):
        blocked = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=None)
    assert blocked["dispatched"] == []
    assert blocked["budget_blocked"] == ["t1"]
    assert blocked["budget_unenforceable"] is True
    assert _saga_ids(db_conn) == []
    assert any("ledger unavailable" in r.getMessage() for r in caplog.records)

    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    advisory = _tick(db_conn, tmp_path, test_id="t1", now="2026-07-27T01:00:00Z", ledger=None)
    assert advisory["dispatched"] == ["t1"]
    assert advisory["budget_unenforceable"] is True
