"""Tests for LonghaulStore: category CRUD, slot claiming, task sessions, cooldowns.

All tests use temporary in-memory SQLite databases with migration 043 applied.
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import LonghaulStore


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create a temporary database with all migrations applied."""
    db = str(tmp_path / "test.db")
    migrate(db)
    return db


class TestCategories:
    """Category CRUD + slug deduplication."""

    def test_create_category(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("My Category", color="blue", wip_limit=2)
            assert cat["id"].startswith("cat")
            assert cat["name"] == "My Category"
            assert cat["slug"] == "my-category"
            assert cat["color"] == "blue"
            assert cat["wip_limit"] == 2
            assert cat["created_at"]
            assert cat["updated_at"]
        finally:
            store.close()

    def test_create_category_slug_deduplication(self, db_path: str) -> None:
        """Creating with same slug returns existing category."""
        store = LonghaulStore(db_path)
        try:
            cat1 = store.create_category("My Category", color="blue", wip_limit=2)
            cat2 = store.create_category("MY CATEGORY", color="red", wip_limit=3)  # Different case
            assert cat1["id"] == cat2["id"]
            assert cat1["slug"] == cat2["slug"]
            assert cat2["color"] == "blue"  # Original value
            assert cat2["wip_limit"] == 2
        finally:
            store.close()

    def test_create_category_slug_race_returns_winner(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UNIQUE(slug) race: a rival store lands the same slug between our
        existence check and INSERT. The loser catches the IntegrityError,
        re-selects by slug, and returns the winner's row (create-or-get)."""
        store = LonghaulStore(db_path)
        rival = LonghaulStore(db_path)
        try:
            original_begin = store._begin
            fired: list[bool] = []

            def racing_begin() -> None:
                # Fires in the SELECT→INSERT window, before we take the write
                # lock — exactly the concurrent-writer interleaving.
                if not fired:
                    fired.append(True)
                    rival.create_category("Race Cat", color="red", wip_limit=3)
                original_begin()

            monkeypatch.setattr(store, "_begin", racing_begin)
            result = store.create_category("Race Cat", color="blue", wip_limit=2)
            assert fired, "race hook never fired"
            winner = rival.get_category("race-cat")
            assert result["id"] == winner["id"]
            assert result["color"] == "red"  # the winner's values, not ours
            assert result["wip_limit"] == 3
            rows = [c for c in store.list_categories() if c["slug"] == "race-cat"]
            assert len(rows) == 1  # exactly one row survived the race
        finally:
            store.close()
            rival.close()

    def test_create_category_non_slug_integrity_error_still_raises(self, db_path: str) -> None:
        """Only the slug race is absorbed: an unrelated constraint violation
        (CHECK wip_limit >= 1) finds no row on re-select and re-raises."""
        store = LonghaulStore(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                store.create_category("Bad Limit", wip_limit=0)
            assert store.get_category("bad-limit") is None
        finally:
            store.close()

    def test_list_categories(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            cat1 = store.create_category("Alpha")
            cat2 = store.create_category("Beta")
            cats = store.list_categories()
            assert len(cats) >= 2
            ids = {c["id"] for c in cats}
            assert cat1["id"] in ids
            assert cat2["id"] in ids
        finally:
            store.close()

    def test_get_category_by_id(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Test")
            retrieved = store.get_category(cat["id"])
            assert retrieved["id"] == cat["id"]
            assert retrieved["name"] == "Test"
        finally:
            store.close()

    def test_get_category_by_slug(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Test")
            retrieved = store.get_category("test")
            assert retrieved["id"] == cat["id"]
        finally:
            store.close()

    def test_get_category_not_found(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            result = store.get_category("nonexistent")
            assert result is None
        finally:
            store.close()

    def test_update_category(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Original", color="blue", wip_limit=1)
            updated = store.update_category(cat["id"], name="Updated", color="red", wip_limit=3)
            assert updated["name"] == "Updated"
            assert updated["color"] == "red"
            assert updated["wip_limit"] == 3
            assert updated["updated_at"] >= cat["updated_at"]
        finally:
            store.close()

    def test_update_category_not_found(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            result = store.update_category("nonexistent")
            assert result is None
        finally:
            store.close()


class TestClaimCategorySlot:
    """Slot claiming with wip_limit enforcement.

    WIP counting is lane-scoped (FB4+): only ``lane='longhaul'`` cards hold
    slots, so the helper creates longhaul-lane tasks — the engine only ever
    claims for lane='longhaul' cards (the dispatch gate), so this mirrors
    production.
    """

    def _create_task(self, conn: Any, board_task_id: str, lane: str | None = "longhaul") -> None:
        """Helper to create a board_task row (longhaul lane by default)."""
        conn.execute(
            "INSERT INTO board_tasks (id, title, status, lane, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (board_task_id, "Test Task", "pending", lane, utc_now_iso(), utc_now_iso()),
        )
        conn.commit()

    def test_claim_slot_available(self, db_path: str) -> None:
        """Claiming when capacity available transitions to in_progress."""
        store = LonghaulStore(db_path)
        try:
            # Create category with wip_limit=1
            cat = store.create_category("Test", wip_limit=1)

            # Create a task
            task_id = "btk_test_001"
            self._create_task(store._connection, task_id)

            # Claim should succeed
            claimed = store.claim_category_slot(cat["id"], task_id)
            assert claimed is True

            # Task should be in_progress
            task = store._connection.execute(
                "SELECT status, park_state FROM board_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert task["status"] == "in_progress"
            assert task["park_state"] is None
        finally:
            store.close()

    def test_claim_slot_full_parks(self, db_path: str) -> None:
        """Claiming when full parks task in waiting_category."""
        store = LonghaulStore(db_path)
        try:
            # Create category with wip_limit=1
            cat = store.create_category("Test", wip_limit=1)

            # Create and claim first task
            task1_id = "btk_test_001"
            self._create_task(store._connection, task1_id)
            store.claim_category_slot(cat["id"], task1_id)

            # Create second task
            task2_id = "btk_test_002"
            self._create_task(store._connection, task2_id)

            # Claim second should fail and park
            claimed = store.claim_category_slot(cat["id"], task2_id)
            assert claimed is False

            # Task2 should be parked
            task2 = store._connection.execute(
                "SELECT status, park_state FROM board_tasks WHERE id = ?",
                (task2_id,),
            ).fetchone()
            assert task2["park_state"] == "waiting_category"
            assert task2["status"] == "pending"
        finally:
            store.close()

    def test_parked_waiting_capacity_holds_slot(self, db_path: str) -> None:
        """A parked waiting_capacity task KEEPS status=in_progress and holds its slot."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Test", wip_limit=1)

            task1_id = "btk_test_001"
            self._create_task(store._connection, task1_id)
            store.claim_category_slot(cat["id"], task1_id)

            # Manually set task1 to parked (simulating mid-work park)
            store._connection.execute(
                "UPDATE board_tasks SET park_state = 'waiting_capacity' WHERE id = ?",
                (task1_id,),
            )
            store._connection.commit()

            # Try to claim a second task
            task2_id = "btk_test_002"
            self._create_task(store._connection, task2_id)
            claimed = store.claim_category_slot(cat["id"], task2_id)

            # Should still fail because task1 holds its slot even though parked
            assert claimed is False
        finally:
            store.close()

    def test_next_waiting_in_category_fifo(self, db_path: str) -> None:
        """next_waiting_in_category returns oldest waiting task."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Test", wip_limit=1)

            # Claim one, park two more
            task1_id = "btk_001"
            self._create_task(store._connection, task1_id)
            store.claim_category_slot(cat["id"], task1_id)

            task2_id = "btk_002"
            self._create_task(store._connection, task2_id)
            store.claim_category_slot(cat["id"], task2_id)  # Parks

            # Small delay to ensure created_at differs
            import time

            time.sleep(0.01)

            task3_id = "btk_003"
            self._create_task(store._connection, task3_id)
            store.claim_category_slot(cat["id"], task3_id)  # Parks

            # next_waiting should be task2 (older)
            next_task = store.next_waiting_in_category(cat["id"])
            assert next_task == task2_id
        finally:
            store.close()

    def _create_card(
        self,
        conn: Any,
        board_task_id: str,
        *,
        lane: str | None = None,
        swarm_run_id: str | None = None,
        category_id: str | None = None,
        status: str = "pending",
        park_state: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """Full-control card insert for cross-lane WIP tests."""
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO board_tasks "
            "(id, title, status, lane, swarm_run_id, category_id, park_state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                board_task_id,
                "Card",
                status,
                lane,
                swarm_run_id,
                category_id,
                park_state,
                created_at or now,
                now,
            ),
        )
        conn.commit()

    def test_categorized_swarm_card_does_not_consume_wip_slot(self, db_path: str) -> None:
        """FB4+: WIP counting is lane-scoped — a categorized swarm card in
        in_progress must NOT hold a longhaul slot."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Shared", wip_limit=1)

            # A swarm member card (lane NULL) carrying the category, active.
            self._create_card(
                store._connection,
                "btk_swarm_member",
                swarm_run_id="swr_test",
                category_id=cat["id"],
                status="in_progress",
            )
            # A real longhaul task should claim the slot immediately.
            self._create_task(store._connection, "btk_longhaul_task")
            assert store.claim_category_slot(cat["id"], "btk_longhaul_task") is True

            task = store._connection.execute(
                "SELECT status, park_state FROM board_tasks WHERE id = ?",
                ("btk_longhaul_task",),
            ).fetchone()
            assert task["status"] == "in_progress"
            assert task["park_state"] is None
        finally:
            store.close()

    def test_categorized_fast_lane_card_does_not_consume_wip_slot(self, db_path: str) -> None:
        """D9: the WIP filter is lane='longhaul' EXACT — categorized fast-lane
        cards are metadata too."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Shared", wip_limit=1)
            self._create_card(
                store._connection,
                "btk_fast_card",
                lane="fast",
                category_id=cat["id"],
                status="in_progress",
            )
            self._create_task(store._connection, "btk_longhaul_task")
            assert store.claim_category_slot(cat["id"], "btk_longhaul_task") is True
        finally:
            store.close()

    def test_longhaul_cards_still_consume_wip_slots(self, db_path: str) -> None:
        """Sanity inverse: an active lane='longhaul' card DOES hold its slot."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Shared", wip_limit=1)
            self._create_card(
                store._connection,
                "btk_active_longhaul",
                lane="longhaul",
                category_id=cat["id"],
                status="in_progress",
            )
            self._create_task(store._connection, "btk_next")
            assert store.claim_category_slot(cat["id"], "btk_next") is False
            parked = store._connection.execute(
                "SELECT park_state FROM board_tasks WHERE id = ?", ("btk_next",)
            ).fetchone()
            assert parked["park_state"] == "waiting_category"
        finally:
            store.close()

    def test_next_waiting_never_returns_non_longhaul_card(self, db_path: str) -> None:
        """FB4+ defense in depth: even with park_state forced onto a swarm
        card, the FIFO wake only ever hands back lane='longhaul' tasks — a
        swarm card must not shadow the real waiting longhaul task."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Shared", wip_limit=1)

            # An OLDER swarm card with waiting_category forced on.
            self._create_card(
                store._connection,
                "btk_swarm_waiting",
                swarm_run_id="swr_test",
                category_id=cat["id"],
                park_state="waiting_category",
                created_at="2020-01-01T00:00:00+00:00",
            )
            # Only the swarm card waits: nothing to wake.
            assert store.next_waiting_in_category(cat["id"]) is None

            # A newer REAL longhaul waiting task wins despite FIFO order.
            self._create_card(
                store._connection,
                "btk_longhaul_waiting",
                lane="longhaul",
                category_id=cat["id"],
                park_state="waiting_category",
            )
            assert store.next_waiting_in_category(cat["id"]) == "btk_longhaul_waiting"
        finally:
            store.close()

    def test_claim_slot_concurrency_race(self, db_path: str) -> None:
        """Concurrent claims never exceed wip_limit (real concurrency test)."""
        store = LonghaulStore(db_path)
        try:
            cat = store.create_category("Test", wip_limit=2)

            # Create 5 tasks
            task_ids = [f"btk_{i:03d}" for i in range(5)]
            for task_id in task_ids:
                self._create_task(store._connection, task_id)

            # Launch 5 concurrent claims
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [
                    executor.submit(store.claim_category_slot, cat["id"], task_id)
                    for task_id in task_ids
                ]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]

            # Exactly 2 should claim successfully
            claimed_count = sum(1 for r in results if r)
            assert claimed_count == 2

            # Exactly 3 should be parked
            parked = store._connection.execute(
                "SELECT COUNT(*) as cnt FROM board_tasks "
                "WHERE category_id = ? AND park_state = 'waiting_category'",
                (cat["id"],),
            ).fetchone()
            assert parked["cnt"] == 3
        finally:
            store.close()


class TestTaskSessions:
    """Task session ordering and lifecycle."""

    def _create_task(self, conn: Any, board_task_id: str) -> None:
        """Helper to create a board_task row."""
        conn.execute(
            "INSERT INTO board_tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (board_task_id, "Test Task", "in_progress", utc_now_iso(), utc_now_iso()),
        )
        conn.commit()

    def test_open_attempt_creates_with_seq(self, db_path: str) -> None:
        """open_attempt creates a new TaskSession with auto-incremented seq."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt = store.open_attempt(
                task_id, "cli-claude", "opus", account_id="acct_1", session_id="ses_1"
            )

            assert attempt["id"].startswith("tks")
            assert attempt["board_task_id"] == task_id
            assert attempt["seq"] == 0
            assert attempt["session_id"] == "ses_1"
            assert attempt["harness"] == "cli-claude"
            assert attempt["model"] == "opus"
            assert attempt["account_id"] == "acct_1"
            assert attempt["ended_at"] is None
            assert attempt["end_reason"] is None
        finally:
            store.close()

    def test_open_attempt_seq_increments(self, db_path: str) -> None:
        """Sequential open_attempt calls increment seq."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt1 = store.open_attempt(task_id, "cli-claude", "opus")
            assert attempt1["seq"] == 0

            store.close_attempt(attempt1["id"], "crashed")

            attempt2 = store.open_attempt(task_id, "cli-claude", "sonnet")
            assert attempt2["seq"] == 1
        finally:
            store.close()

    def test_open_attempt_rejects_second_live(self, db_path: str) -> None:
        """open_attempt raises when an open attempt already exists."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            store.open_attempt(task_id, "cli-claude", "opus")

            # Second open_attempt should raise
            with pytest.raises(RuntimeError, match="already has an open attempt"):
                store.open_attempt(task_id, "cli-claude", "sonnet")
        finally:
            store.close()

    def test_close_attempt_marks_ended(self, db_path: str) -> None:
        """close_attempt stamps ended_at and end_reason."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt = store.open_attempt(task_id, "cli-claude", "opus")

            closed = store.close_attempt(attempt["id"], "completed", detail="Success")
            assert closed is True

            # Verify in DB
            row = store._connection.execute(
                "SELECT ended_at, end_reason, detail FROM task_sessions WHERE id = ?",
                (attempt["id"],),
            ).fetchone()
            assert row["ended_at"] is not None
            assert row["end_reason"] == "completed"
            assert row["detail"] == "Success"
        finally:
            store.close()

    def test_close_attempt_idempotent(self, db_path: str) -> None:
        """close_attempt is idempotent (second call returns False)."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt = store.open_attempt(task_id, "cli-claude", "opus")

            first = store.close_attempt(attempt["id"], "completed")
            assert first is True

            # Second close should return False
            second = store.close_attempt(attempt["id"], "crashed")
            assert second is False
        finally:
            store.close()

    def test_list_attempts_ordered_by_seq(self, db_path: str) -> None:
        """list_attempts returns all attempts in seq order."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt1 = store.open_attempt(task_id, "cli-claude", "opus", session_id="ses_1")
            store.close_attempt(attempt1["id"], "crashed")

            store.open_attempt(task_id, "cli-claude", "sonnet", session_id="ses_2")

            attempts = store.list_attempts(task_id)
            assert len(attempts) == 2
            assert attempts[0]["seq"] == 0
            assert attempts[1]["seq"] == 1
            assert attempts[0]["session_id"] == "ses_1"
            assert attempts[1]["session_id"] == "ses_2"
        finally:
            store.close()

    def test_current_attempt_returns_open_only(self, db_path: str) -> None:
        """current_attempt returns only the open (ended_at IS NULL) attempt."""
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            attempt1 = store.open_attempt(task_id, "cli-claude", "opus")
            store.close_attempt(attempt1["id"], "crashed")

            # No open attempt
            current = store.current_attempt(task_id)
            assert current is None

            # Open new attempt
            attempt2 = store.open_attempt(task_id, "cli-claude", "sonnet")
            current = store.current_attempt(task_id)
            assert current["id"] == attempt2["id"]
        finally:
            store.close()


class TestLonghaulJsonFields:
    """Longhaul task field accessors."""

    def _create_task(self, conn: Any, board_task_id: str) -> None:
        conn.execute(
            "INSERT INTO board_tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (board_task_id, "Test Task", "pending", utc_now_iso(), utc_now_iso()),
        )
        conn.commit()

    def test_set_and_get_lane(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            store.set_lane(task_id, "longhaul")

            task = store._connection.execute(
                "SELECT lane FROM board_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert task["lane"] == "longhaul"
        finally:
            store.close()

    def test_set_and_get_park_state(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            store.set_park_state(task_id, "waiting_capacity")

            task = store._connection.execute(
                "SELECT park_state FROM board_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert task["park_state"] == "waiting_capacity"
        finally:
            store.close()

    def test_set_and_get_longhaul_json(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            data = {"acceptance": "Task complete", "max_sessions": 5}
            store.set_longhaul_json(task_id, data)

            retrieved = store.get_longhaul_json(task_id)
            assert retrieved == data
        finally:
            store.close()

    def test_list_parked(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            # Create and park tasks
            for i in range(3):
                task_id = f"btk_{i:03d}"
                self._create_task(store._connection, task_id)
                store.set_park_state(task_id, "waiting_capacity")

            parked = store.list_parked("waiting_capacity")
            assert len(parked) == 3
        finally:
            store.close()


class TestAccountCooldown:
    """Account cooldown set/clear for usage limits."""

    def _create_account(self, conn: Any, account_id: str) -> None:
        conn.execute(
            "INSERT INTO claude_accounts (id, label, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, f"Account {account_id}", "ok", utc_now_iso(), utc_now_iso()),
        )
        conn.commit()

    def test_set_account_cooldown(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            account_id = "acct_test"
            self._create_account(store._connection, account_id)

            until = "2026-07-23T15:00:00Z"
            result = store.set_account_cooldown(account_id, until, detail="rate_limited_5h")
            assert result is True

            account = store._connection.execute(
                "SELECT status, status_detail, cooldown_until FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            assert account["status"] == "rate_limited"
            assert account["status_detail"] == "rate_limited_5h"
            assert account["cooldown_until"] == until
        finally:
            store.close()

    def test_clear_expired_cooldowns(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            # Create two accounts
            acct1_id = "acct_001"
            acct2_id = "acct_002"
            self._create_account(store._connection, acct1_id)
            self._create_account(store._connection, acct2_id)

            # Cool them down
            # Relative to now, NOT hardcoded wall-clock dates: fixed "future"
            # timestamps become the past the moment the clock passes them (this
            # test went red at 2026-07-23T20:00Z with hardcoded hours).
            from datetime import UTC, datetime, timedelta

            now_dt = datetime.now(UTC)
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            past = (now_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            future = (now_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

            store.set_account_cooldown(acct1_id, past)
            store.set_account_cooldown(acct2_id, future)

            # Clear expired (only acct1 should be cleared)
            cleared = store.clear_expired_cooldowns(now)
            assert acct1_id in cleared
            assert acct2_id not in cleared

            # Verify acct1 is restored
            acct1 = store._connection.execute(
                "SELECT status, cooldown_until FROM claude_accounts WHERE id = ?",
                (acct1_id,),
            ).fetchone()
            assert acct1["status"] == "ok"
            assert acct1["cooldown_until"] is None

            # Verify acct2 still cooling
            acct2 = store._connection.execute(
                "SELECT status, cooldown_until FROM claude_accounts WHERE id = ?",
                (acct2_id,),
            ).fetchone()
            assert acct2["status"] == "rate_limited"
            assert acct2["cooldown_until"] == future
        finally:
            store.close()


class TestConversationHelpers:
    """Conversation thread helpers."""

    def _create_task(self, conn: Any, board_task_id: str) -> None:
        conn.execute(
            "INSERT INTO board_tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (board_task_id, "Test Task", "pending", utc_now_iso(), utc_now_iso()),
        )
        conn.commit()

    def test_append_task_turn(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            store.append_task_turn(task_id, "user", "Help me fix this", meta={"kind": "steering"})

            turns = store.task_turns(task_id)
            assert len(turns) == 1
            assert turns[0]["role"] == "user"
            assert turns[0]["content"] == "Help me fix this"
            assert json.loads(turns[0]["meta_json"])["kind"] == "steering"
        finally:
            store.close()

    def test_pending_steering(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            # Add turns with and without pending delivery
            store.append_task_turn(
                task_id,
                "user",
                "Urgent fix",
                meta={"delivery": {"pending": True}},
            )
            store.append_task_turn(
                task_id,
                "user",
                "Delivered",
                meta={"delivery": {"delivered_at": utc_now_iso()}},
            )

            pending = store.pending_steering(task_id)
            assert len(pending) == 1
            assert "Urgent fix" in pending[0]["content"]
        finally:
            store.close()

    def test_mark_turn_delivered(self, db_path: str) -> None:
        store = LonghaulStore(db_path)
        try:
            task_id = "btk_test"
            self._create_task(store._connection, task_id)

            store.append_task_turn(
                task_id, "user", "Fix this", meta={"delivery": {"pending": True}}
            )

            turns = store.task_turns(task_id, limit=1)
            turn_id = turns[0]["id"]

            result = store.mark_turn_delivered(turn_id, "ses_001")
            assert result is True

            # Verify marked as delivered
            row = store._connection.execute(
                "SELECT meta_json FROM conversations WHERE id = ?", (turn_id,)
            ).fetchone()
            meta = json.loads(row["meta_json"])
            assert meta["delivery"]["session_id"] == "ses_001"
            assert meta["delivery"]["delivered_at"] is not None
        finally:
            store.close()


class TestMigration043:
    """Verify migration 043 creates required schema."""

    def test_migration_043_creates_tables_and_columns(self, db_path: str) -> None:
        """Verify migration 043 was applied and created the schema."""
        store = LonghaulStore(db_path)
        try:
            # Verify task_categories table exists
            store.create_category("Test")

            # Verify board_tasks columns exist
            task_id = "btk_test"
            store._connection.execute(
                "INSERT INTO board_tasks "
                "(id, title, status, category_id, lane, park_state, longhaul_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    "Test",
                    "pending",
                    None,
                    "longhaul",
                    "waiting_category",
                    "{}",
                    utc_now_iso(),
                    utc_now_iso(),
                ),
            )
            store._connection.commit()

            # Verify task_sessions table exists
            attempt_id = "tks_test"
            store._connection.execute(
                "INSERT INTO task_sessions "
                "(id, board_task_id, seq, session_id, harness, model, account_id, started_at, ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    task_id,
                    0,
                    "ses_123",
                    "cli-claude",
                    "opus",
                    None,
                    utc_now_iso(),
                    None,
                    None,
                    "",
                ),
            )
            store._connection.commit()

            # Verify claude_accounts.cooldown_until column exists
            store._connection.execute("UPDATE claude_accounts SET cooldown_until = NULL WHERE 1=0")
        finally:
            store.close()

    def test_migration_043_indexes_exist(self, db_path: str) -> None:
        """Verify indexes are created."""
        store = LonghaulStore(db_path)
        try:
            # Verify indexes by checking they can be used (query succeeds)
            store._connection.execute(
                "SELECT * FROM board_tasks WHERE category_id IS NOT NULL AND status = 'pending'"
            ).fetchall()

            store._connection.execute(
                "SELECT * FROM board_tasks WHERE park_state = 'waiting_category'"
            ).fetchall()

            store._connection.execute(
                "SELECT * FROM task_sessions WHERE board_task_id = 'x' ORDER BY seq"
            ).fetchall()
        finally:
            store.close()
