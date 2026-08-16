"""Fixtures for the scoring / diagnostics / report battery.

Cards are built through the REAL write path wherever the store allows it —
create open, attach evidence, move to done, verify with a second person — so a
scoring test is also a test that the sequence it describes is actually
reachable. Only the two things the store cannot express are written directly:
a verification stamped at a chosen past time (``verify_task`` stamps *now*, and
these tests need cards inside and outside a fixed window) and bulk evidence rows
(five hundred one-row transactions would dominate the suite's runtime).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import new_id
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from tests.support.db_template import migrated_db

# One fixed week, so every window boundary in this suite is arithmetic a reader
# can check by eye rather than a moving "now".
DAY = "2026-08-14"
WINDOW_START = "2026-08-08"  # the 7d window is [WINDOW_START, DAY], inclusive
IN_WINDOW = "2026-08-12T10:00:00Z"
EARLIER_IN_WINDOW = "2026-08-09T09:00:00Z"
BEFORE_WINDOW = "2026-07-01T10:00:00Z"
YESTERDAY = "2026-08-13T10:00:00Z"

# The stores stamp transitions with the REAL wall clock (``utc_now_iso``), so a
# card driven to done through the real write path lands at "now" — which left
# the fixed window above the moment the calendar rolled past DAY (the suite was
# green only on its authoring date). Pin the clock the stores read to noon of
# DAY so the fixed week stays valid forever. ``utc_now_iso`` resolves
# ``datetime`` from ``omniagentos.contracts`` module globals, so this one patch
# point covers every store regardless of how it imported the helper.
_PINNED_NOW = (2026, 8, 14, 12, 0, 0)


@pytest.fixture(autouse=True)
def _pin_store_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime as _real_datetime

    class _PinnedDatetime(_real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _PinnedDatetime:
            return cls(*_PINNED_NOW, tzinfo=tz)

    monkeypatch.setattr("omniagentos.contracts.datetime", _PinnedDatetime)

_EVIDENCE_COLUMNS = (
    "id, task_id, kind, ref, repo, actor, title, attribution, confidence, "
    "quality_gate, meta_json, created_at"
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return migrated_db(CollabStore, tmp_path / "team_scoring.db")


@pytest.fixture
def collab_store(db_path: str) -> CollabStore:
    return CollabStore(db_path)


@pytest.fixture
def store(collab_store: CollabStore) -> SqliteStore:
    return collab_store._store


@pytest.fixture
def team_store(store: SqliteStore) -> TeamStore:
    return TeamStore(store)


@pytest.fixture
def employees(store: SqliteStore) -> dict[str, str]:
    """The three people the Team Work OS names, with roles set."""
    goals = CompanyGoalsStore(store)
    roster = {
        "owner": ("emp_owner", "the operator", "operator"),
        "bob": ("emp_bob", "Bob", "candidate-author"),
        "alice": ("emp_alice", "Alice", "reviewer-merger"),
    }
    for employee_id, name, role in roster.values():
        goals.ensure_employee(employee_id=employee_id, name=name, role=role)
    return {key: value[0] for key, value in roster.items()}


def _set(store: SqliteStore, sql: str, parameters: Iterable[Any]) -> None:
    store._write(sql, tuple(parameters))


@pytest.fixture
def make_task(
    collab_store: CollabStore, team_store: TeamStore, store: SqliteStore
) -> Callable[..., str]:
    """Create a card and drive it as far along the real path as asked.

    ``evidence`` is a sequence of ``(kind, ref, quality_gate)`` or
    ``(kind, ref, quality_gate, meta)``. ``verified_at`` moves the card to done
    and verifies it (with ``verifier``, who is NOT the owner by default), then
    back-stamps the verification time so the card lands in a chosen window.
    """

    def factory(
        *,
        owner: str,
        size: str = "M",
        title: str = "A card",
        ref: str | None = None,
        status: str | None = None,
        parent_task_id: str | None = None,
        source: str = "",
        acceptance: str = "the acceptance test passes",
        evidence: Sequence[tuple[Any, ...]] = (),
        verified_at: str | None = None,
        verifier: str = "emp_owner",
        blocked_reason: str = "",
        done: bool = False,
        updated_at: str | None = None,
    ) -> str:
        # Baseline rows are immutable once created. Imports create them in their
        # final state, so this fixture does the same instead of creating an open
        # baseline and immediately trying to PATCH it to done (which production
        # correctly refuses). Verification is still earned below through the
        # real TeamStore path.
        baseline_done_at_create = source == BASELINE_SOURCE and (verified_at is not None or done)
        card = BoardTask(
            title=title,
            ref=ref,
            size=size,
            status=(BoardTaskStatus.DONE if baseline_done_at_create else BoardTaskStatus.OPEN),
            owner_employee_id=owner,
            parent_task_id=parent_task_id,
            acceptance_criteria=acceptance,
            source=source,
            blocked_reason=blocked_reason,
        )
        collab_store.create_board_task(card)
        for item in evidence:
            kind, evidence_ref, gate = item[0], item[1], item[2]
            meta = item[3] if len(item) > 3 else {}
            team_store.add_evidence(
                kind=str(kind),
                ref=str(evidence_ref),
                task_id=card.id,
                actor=owner,
                quality_gate=str(gate),
                meta=dict(meta),
            )
        if (verified_at is not None or done) and not baseline_done_at_create:
            collab_store.update_board_task(
                card.id, {"status": BoardTaskStatus.DONE.value}, actor=owner
            )
        if verified_at is not None:
            team_store.verify_task(card.id, verifier)
            _set(
                store,
                "UPDATE board_tasks SET verified_at = ? WHERE id = ?",
                (verified_at, card.id),
            )
        if status is not None:
            # Through the real transition (so the blocked-needs-a-reason and
            # done-needs-evidence gates apply exactly as they would in life).
            collab_store.update_board_task(card.id, {"status": status}, actor=owner)
        if updated_at is not None:
            # Every transition above stamps updated_at = now; the review-latency
            # rule reads it, so a test about "waiting 30 hours" has to set it
            # last and explicitly rather than depend on the wall clock.
            _set(store, "UPDATE board_tasks SET updated_at = ? WHERE id = ?", (updated_at, card.id))
        return card.id

    return factory


@pytest.fixture
def bulk_evidence(team_store: TeamStore) -> Callable[..., int]:
    """Insert N evidence rows in ONE transaction (volume fixtures only)."""

    def factory(
        *,
        task_id: str | None,
        kind: str,
        count: int,
        quality_gate: str = "pass",
        prefix: str = "bulk",
        actor: str = "",
        meta_json: str = "{}",
        created_at: str = IN_WINDOW,
    ) -> int:
        rows = [
            (
                new_id("tev"),
                task_id,
                kind,
                f"{prefix}-{index}",
                "",
                actor,
                "",
                "deterministic",
                1.0,
                quality_gate,
                meta_json,
                created_at,
            )
            for index in range(count)
        ]

        def body(connection: sqlite3.Connection) -> int:
            connection.executemany(
                f"INSERT INTO task_evidence ({_EVIDENCE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)

        return team_store._store._execute_write_txn(body, op="test.bulk_evidence")

    return factory


@pytest.fixture
def add_session(store: SqliteStore) -> Callable[..., str]:
    """Append one ``task_sessions`` attempt row to a card."""
    counter = {"seq": 0}

    def factory(
        *,
        task_id: str,
        started_at: str = IN_WINDOW,
        ended_at: str | None = IN_WINDOW,
        harness: str = "cli-claude",
        model: str = "opus",
        end_reason: str | None = "completed",
    ) -> str:
        counter["seq"] += 1
        session_row_id = new_id("tks")
        _set(
            store,
            "INSERT INTO task_sessions "
            "(id, board_task_id, seq, session_id, harness, model, started_at, ended_at, "
            "end_reason, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
            (
                session_row_id,
                task_id,
                counter["seq"],
                f"ses_{session_row_id}",
                harness,
                model,
                started_at,
                ended_at,
                end_reason,
            ),
        )
        return session_row_id

    return factory


@pytest.fixture
def scores_now(team_store: TeamStore) -> Callable[[], dict[str, int]]:
    """``employee_id -> score`` for the fixed 7d window. The delta instrument."""
    from omniagentos.team.scoring import compute_scores

    def snapshot() -> dict[str, int]:
        return {
            employee_id: breakdown.score
            for employee_id, breakdown in compute_scores(
                team_store, period_start=WINDOW_START, period_end=DAY
            ).items()
        }

    return snapshot


@pytest.fixture(autouse=True)
def _hermetic_fleet_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the fleet-production reader at a nonexistent fixture path.

    Without this, gather()/the scoreboard would read the SERVING checkout's real
    ``var/loopqueue/ledger.jsonl`` and every score test would move with the live
    factory. Tests that WANT fleet data write their own file and re-point."""
    monkeypatch.setenv("OMNI_TEAM_FLEET_LEDGER", str(tmp_path / "no-such-ledger.jsonl"))
