"""Tests for completed-run reconcile + dead-session flip (truthful budget)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.intake.run_card_reconcile import (
    DEAD_SESSION_STALE_SECONDS,
    LIFECYCLE_RECONCILE_ENV,
    MAX_DETECT_TO_FLIP_SECONDS,
    ROUTINES_TICK_SECONDS,
    reconcile_run_cards,
)
from omniagentos.notifications.dal import NotificationsDal

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


class _FakeCollab:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = {str(t["id"]): dict(t) for t in tasks}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.events: list[dict[str, Any]] = []
        self.cas_misses = 0

    def list_board_tasks(self, archived: int = 0) -> list[dict[str, Any]]:
        return [dict(t) for t in self.tasks.values() if not t.get("archived_at")]

    def get_board_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def update_board_task(
        self,
        task_id: str,
        fields: dict[str, Any],
        *,
        expect_status: str | None = None,
    ) -> bool:
        if expect_status is not None and str(self.tasks[task_id].get("status")) != expect_status:
            self.cas_misses += 1
            return False
        self.updates.append((task_id, dict(fields)))
        self.tasks[task_id].update(fields)
        return True


class _FakeStore:
    def __init__(self, runs: dict[str, dict[str, Any]], *, db_path: str | None = None) -> None:
        self._runs = runs
        self.events: list[dict[str, Any]] = []
        if db_path is not None:
            self._db_path = db_path

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def insert_event(self, *args: Any, **kwargs: Any) -> None:
        self.events.append({"args": args, "kwargs": kwargs})


def test_completed_run_clears_blocked_cards() -> None:
    collab = _FakeCollab(
        [
            {
                "id": "bt_1",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "waiting",
            },
            {
                "id": "bt_2",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "also waiting",
            },
            {
                "id": "bt_live",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_live",
                "result_ref": "",
                "description": "still running",
            },
        ]
    )
    store = _FakeStore(
        {
            "run_done": {"id": "run_done", "state": "completed"},
            "run_live": {"id": "run_live", "state": "running"},
        }
    )
    result = reconcile_run_cards(
        store,
        collab,
        runs=store._runs,
        now=NOW,
        mode="enforce",
    )
    assert result["unblocked"] == 2
    assert result["blocked_remaining_for_completed"] == 0
    assert collab.tasks["bt_1"]["status"] == BoardTaskStatus.DONE.value
    assert collab.tasks["bt_2"]["status"] == BoardTaskStatus.DONE.value
    assert collab.tasks["bt_live"]["status"] == BoardTaskStatus.BLOCKED.value


def test_completed_transition_emits_existing_board_and_notification_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, str]] = []
    notified: list[tuple[str, str]] = []

    def fake_emit(_collab: Any, task_id: str, **kwargs: Any) -> None:
        emitted.append((task_id, str(kwargs.get("run_id") or "")))

    def fake_notify(_collab: Any, task_id: str, **kwargs: Any) -> None:
        notified.append((task_id, str(kwargs.get("run_id") or "")))

    monkeypatch.setattr("omniagentos.intake.service._emit_board_event", fake_emit)
    monkeypatch.setattr("omniagentos.intake.service._notify_task_done_safe", fake_notify)
    collab = _FakeCollab(
        [
            {
                "id": "bt_done",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "",
            }
        ]
    )
    store = _FakeStore({"run_done": {"id": "run_done", "state": "completed"}})

    result = reconcile_run_cards(store, collab, runs=store._runs, now=NOW, mode="enforce")

    assert result["unblocked"] == 1
    assert emitted == [("bt_done", "run_done")]
    assert notified == [("bt_done", "run_done")]


def test_dead_session_flips_within_truthful_budget() -> None:
    # Injected clock: heartbeat past stale threshold, still within
    # ROUTINES_TICK + stale worst-case budget (not a false 60s claim).
    heartbeat = NOW - timedelta(seconds=DEAD_SESSION_STALE_SECONDS + 5)
    assert (NOW - heartbeat).total_seconds() <= MAX_DETECT_TO_FLIP_SECONDS
    assert MAX_DETECT_TO_FLIP_SECONDS == ROUTINES_TICK_SECONDS + DEAD_SESSION_STALE_SECONDS
    assert MAX_DETECT_TO_FLIP_SECONDS > 60  # truthful vs 300s cadence
    collab = _FakeCollab(
        [
            {
                "id": "bt_ses",
                "status": BoardTaskStatus.IN_PROGRESS.value,
                "run_id": "",
                "result_ref": "ses_dead",
                "description": "working",
                "updated_at": heartbeat.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
    )
    sessions = {
        "ses_dead": {
            "id": "ses_dead",
            "state": "running",
            "heartbeat_at": heartbeat.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    result = reconcile_run_cards(
        _FakeStore({}),
        collab,
        sessions=sessions,
        runs={},
        now=NOW,
        mode="enforce",
    )
    assert result["flipped"] == 1
    assert collab.tasks["bt_ses"]["status"] == BoardTaskStatus.BLOCKED.value
    age = next(p["age_seconds"] for p in result["planned"] if p["action"] == "dead_session_flip")
    assert age is not None
    assert age <= MAX_DETECT_TO_FLIP_SECONDS


def test_longhaul_and_swarm_ownership_excluded() -> None:
    """MAJOR: board_sweep ownership exclusions reused — no fight with managers."""
    collab = _FakeCollab(
        [
            {
                "id": "bt_lh",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "longhaul",
                "lane": "longhaul",
            },
            {
                "id": "bt_sw",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "swarm",
                "swarm_run_id": "swr_abc",
            },
            {
                "id": "bt_ok",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "normal",
            },
        ]
    )
    store = _FakeStore({"run_done": {"id": "run_done", "state": "completed"}})
    result = reconcile_run_cards(store, collab, runs=store._runs, now=NOW, mode="enforce")
    assert collab.tasks["bt_lh"]["status"] == BoardTaskStatus.BLOCKED.value
    assert collab.tasks["bt_sw"]["status"] == BoardTaskStatus.BLOCKED.value
    assert collab.tasks["bt_ok"]["status"] == BoardTaskStatus.DONE.value
    assert result["unblocked"] == 1
    assert result["skipped_owned"] >= 2


def test_guarded_path_does_not_revert_live_completion() -> None:
    """MAJOR: unconditional status writes must not clobber a concurrent completion."""
    store = _FakeStore({"run_done": {"id": "run_done", "state": "completed"}})

    # Simulate concurrent completion between plan and apply: list still returns
    # blocked (for planning), but get_board_task re-read sees DONE.
    class _RaceCollab(_FakeCollab):
        def list_board_tasks(self, archived: int = 0) -> list[dict[str, Any]]:
            rows = super().list_board_tasks(archived=archived)
            self.tasks["bt_1"]["status"] = BoardTaskStatus.DONE.value
            return rows

    race = _RaceCollab(
        [
            {
                "id": "bt_1",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "",
            }
        ]
    )
    result = reconcile_run_cards(store, race, runs=store._runs, now=NOW, mode="enforce")
    # Planned one, but guarded apply skips because from_status no longer matches.
    assert race.tasks["bt_1"]["status"] == BoardTaskStatus.DONE.value
    assert result["unblocked"] == 0


def test_status_update_is_compare_and_set_on_real_store(tmp_path: Path) -> None:
    """R4: a completion between the real read and UPDATE wins the CAS race."""
    db_path = str(tmp_path / "cas.db")
    migrate(db_path)
    collab = CollabStore(db_path)
    now = utc_now_iso()
    collab.create_board_task(
        BoardTask(
            id="bt_cas",
            title="cas card",
            description="",
            status=BoardTaskStatus.IN_PROGRESS,
            result_ref="ses_cas",
            created_at=now,
            updated_at=now,
        )
    )

    store = SqliteStore(db_path)
    seen: list[str | None] = []

    real_update = collab.update_board_task

    def race_on_update(
        task_id: str,
        fields: dict[str, Any],
        *,
        expect_status: str | None = None,
    ) -> bool:
        seen.append(expect_status)
        # A second real connection commits completion after reconcile's live
        # status read but before its UPDATE. The production CAS must then miss.
        competing = CollabStore(db_path)
        competing.update_board_task(
            task_id,
            {"status": BoardTaskStatus.DONE.value},
        )
        return real_update(task_id, fields, expect_status=expect_status)

    collab.update_board_task = race_on_update  # type: ignore[method-assign]
    stale = NOW - timedelta(seconds=DEAD_SESSION_STALE_SECONDS + 1)

    result = reconcile_run_cards(
        store,
        collab,
        sessions={
            "ses_cas": {
                "id": "ses_cas",
                "state": "running",
                "heartbeat_at": stale.isoformat(),
            }
        },
        runs={},
        now=NOW,
        mode="enforce",
    )
    final = CollabStore(db_path).get_board_task("bt_cas")
    assert final is not None
    assert final["status"] == BoardTaskStatus.DONE.value
    assert "[auto-blocked: dead-session]" not in str(final["description"])
    assert result["flipped"] == 0
    assert seen == [BoardTaskStatus.IN_PROGRESS.value]


def test_notification_uses_real_store_db_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R3: reconcile persists the done bell beside the card, never in fallback DB."""
    db_path = str(tmp_path / "card-and-notification.db")
    fallback_path = str(tmp_path / "wrong-default.db")
    migrate(db_path)
    migrate(fallback_path)
    monkeypatch.setenv("OMNIAGENTOS_DB", fallback_path)

    sql = SqliteStore(db_path)
    collab = CollabStore(db_path)
    now = utc_now_iso()
    collab.create_board_task(
        BoardTask(
            id="bt_notify_path",
            title="same database",
            description="",
            status=BoardTaskStatus.BLOCKED,
            created_at=now,
            updated_at=now,
        )
    )
    collab.update_board_task("bt_notify_path", {"run_id": "run_done"})

    result = reconcile_run_cards(
        sql,
        collab,
        runs={"run_done": {"id": "run_done", "state": "completed"}},
        now=NOW,
        mode="enforce",
    )

    card = CollabStore(db_path).get_board_task("bt_notify_path")
    assert card is not None
    assert card["status"] == BoardTaskStatus.DONE.value
    assert result["unblocked"] == 1
    assert NotificationsDal(db_path).has_for_ref_kind("board_task", "bt_notify_path", "done")
    assert not NotificationsDal(fallback_path).has_for_ref_kind(
        "board_task", "bt_notify_path", "done"
    )


def test_idempotent() -> None:
    collab = _FakeCollab(
        [
            {
                "id": "bt_1",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "",
            }
        ]
    )
    store = _FakeStore({"run_done": {"id": "run_done", "state": "completed"}})
    first = reconcile_run_cards(store, collab, runs=store._runs, now=NOW, mode="enforce")
    second = reconcile_run_cards(store, collab, runs=store._runs, now=NOW, mode="enforce")
    assert first["unblocked"] == 1
    assert second["unblocked"] == 0  # already not blocked
    assert second["blocked_remaining_for_completed"] == 0


def test_shadow_applies_no_writes() -> None:
    collab = _FakeCollab(
        [
            {
                "id": "bt_1",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "",
            }
        ]
    )
    store = _FakeStore({"run_done": {"id": "run_done", "state": "completed"}})
    result = reconcile_run_cards(store, collab, runs=store._runs, now=NOW, mode="shadow")
    assert result["mode"] == "shadow"
    assert result["unblocked"] == 0
    assert result["planned"]
    assert collab.tasks["bt_1"]["status"] == BoardTaskStatus.BLOCKED.value
    assert collab.updates == []


def test_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIFECYCLE_RECONCILE_ENV, "off")
    collab = _FakeCollab(
        [
            {
                "id": "bt_1",
                "status": BoardTaskStatus.BLOCKED.value,
                "run_id": "run_done",
                "result_ref": "",
                "description": "",
            }
        ]
    )
    result = reconcile_run_cards(
        _FakeStore({"run_done": {"id": "run_done", "state": "completed"}}),
        collab,
        env={LIFECYCLE_RECONCILE_ENV: "off"},
    )
    assert result["mode"] == "off"
    assert collab.updates == []
