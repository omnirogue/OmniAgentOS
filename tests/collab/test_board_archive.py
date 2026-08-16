"""Tests for board task archive functionality (migration 035)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def collab_store(tmp_path):
    """Create a temporary collab store for testing."""
    return make_store(CollabStore, tmp_path / "test.db")


class TestBoardArchive:
    """Test board task archive functionality."""

    def test_archive_sets_archived_at(self, collab_store):
        """Test that archiving a task sets archived_at to current timestamp."""
        task = BoardTask(title="Test Task")
        collab_store.create_board_task(task)

        # Initially not archived
        row = collab_store.get_board_task(task.id)
        assert row["archived_at"] is None
        assert row["archived"] is False

        # Archive the task
        now_iso = utc_now_iso()
        collab_store.update_board_task(task.id, {"archived_at": now_iso})

        # Should be archived now
        row = collab_store.get_board_task(task.id)
        assert row["archived_at"] == now_iso
        assert row["archived"] is True

    def test_archived_field_in_response(self, collab_store):
        """Test that archived field appears in board task responses."""
        task1 = BoardTask(title="Not Archived")
        task2 = BoardTask(title="Archived")
        collab_store.create_board_task(task1)
        collab_store.create_board_task(task2)

        collab_store.update_board_task(task2.id, {"archived_at": utc_now_iso()})

        row1 = collab_store.get_board_task(task1.id)
        row2 = collab_store.get_board_task(task2.id)

        assert row1["archived"] is False
        assert row2["archived"] is True

    def test_list_excludes_archived_by_default(self, collab_store):
        """Test that list_board_tasks excludes archived cards by default."""
        task1 = BoardTask(title="Active 1")
        task2 = BoardTask(title="Archived 1")
        task3 = BoardTask(title="Active 2")

        for task in [task1, task2, task3]:
            collab_store.create_board_task(task)

        # Archive task2
        collab_store.update_board_task(task2.id, {"archived_at": utc_now_iso()})

        # Default list should exclude archived
        tasks = collab_store.list_board_tasks()
        task_ids = [t["id"] for t in tasks]
        assert task1.id in task_ids
        assert task2.id not in task_ids
        assert task3.id in task_ids

    def test_list_archived_only(self, collab_store):
        """Test that list_board_tasks with archived=1 shows only archived cards."""
        task1 = BoardTask(title="Active 1")
        task2 = BoardTask(title="Archived 1")
        task3 = BoardTask(title="Archived 2")

        for task in [task1, task2, task3]:
            collab_store.create_board_task(task)

        collab_store.update_board_task(task2.id, {"archived_at": utc_now_iso()})
        collab_store.update_board_task(task3.id, {"archived_at": utc_now_iso()})

        # Get only archived
        tasks = collab_store.list_board_tasks(archived=1)
        task_ids = [t["id"] for t in tasks]
        assert task1.id not in task_ids
        assert task2.id in task_ids
        assert task3.id in task_ids

    def test_open_tasks_exclude_archived(self, collab_store):
        """Test that open_tasks_for excludes archived cards."""
        task1 = BoardTask(title="Open Task", status=BoardTaskStatus.OPEN)
        task2 = BoardTask(title="Archived Open", status=BoardTaskStatus.OPEN)

        collab_store.create_board_task(task1)
        collab_store.create_board_task(task2)

        collab_store.update_board_task(task2.id, {"archived_at": utc_now_iso()})

        # Get open tasks
        tasks = collab_store.open_tasks_for([])
        task_ids = [t["id"] for t in tasks]
        assert task1.id in task_ids
        assert task2.id not in task_ids

    def test_purge_archived_older_than_cutoff(self, collab_store):
        """Test that purge_archived_board_tasks deletes cards older than N days."""
        # Create a few archived cards with different timestamps
        task_old = BoardTask(title="Old Archived")
        task_recent = BoardTask(title="Recent Archived")
        task_unarchived = BoardTask(title="Not Archived")

        for task in [task_old, task_recent, task_unarchived]:
            collab_store.create_board_task(task)

        # Simulate "now" as 10 days in the future
        now = datetime.now(UTC)
        test_now_iso = (now + timedelta(days=10)).isoformat().replace("+00:00", "Z")

        # Archive old card 9 days ago (relative to test_now)
        old_archived_at = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        collab_store.update_board_task(task_old.id, {"archived_at": old_archived_at})

        # Archive recent card 1 day ago (relative to test_now)
        recent_archived_at = (now + timedelta(days=9)).isoformat().replace("+00:00", "Z")
        collab_store.update_board_task(task_recent.id, {"archived_at": recent_archived_at})

        # Leave task_unarchived unarchived

        # Purge with 7-day cutoff
        deleted_count = collab_store.purge_archived_board_tasks(older_than_days=7, now=test_now_iso)

        # Should only delete the one archived >7 days ago
        assert deleted_count == 1

        # Verify old is gone, recent and unarchived remain
        assert collab_store.get_board_task(task_old.id) is None
        assert collab_store.get_board_task(task_recent.id) is not None
        assert collab_store.get_board_task(task_unarchived.id) is not None

    def test_purge_never_deletes_unarchived_cards(self, collab_store):
        """Test that purge never deletes un-archived cards, even if very old."""
        task_old_unarchived = BoardTask(title="Old but not archived")
        collab_store.create_board_task(task_old_unarchived)

        # Simulate task created very long ago (doesn't matter; we don't purge by created_at)
        # But make sure archived_at is null
        row = collab_store.get_board_task(task_old_unarchived.id)
        assert row["archived_at"] is None

        # Purge with 7-day cutoff and far future "now"
        now = datetime.now(UTC)
        test_now_iso = (now + timedelta(days=365)).isoformat().replace("+00:00", "Z")
        deleted_count = collab_store.purge_archived_board_tasks(older_than_days=7, now=test_now_iso)

        # Should not delete un-archived cards
        assert deleted_count == 0
        assert collab_store.get_board_task(task_old_unarchived.id) is not None

    def test_purge_archived_parent_detaches_open_child(self, collab_store):
        parent = BoardTask(title="Old archived parent")
        child = BoardTask(title="Live child", parent_task_id=parent.id)
        collab_store.create_board_task(parent)
        collab_store.create_board_task(child)
        collab_store.update_board_task(parent.id, {"archived_at": "2026-07-01T00:00:00Z"})

        deleted = collab_store.purge_archived_board_tasks(
            older_than_days=7, now="2026-08-10T00:00:00Z"
        )

        assert deleted == 1
        assert collab_store.get_board_task(parent.id) is None
        surviving_child = collab_store.get_board_task(child.id)
        assert surviving_child is not None
        assert surviving_child["parent_task_id"] is None

    def test_archive_idempotent(self, collab_store):
        """Test that archiving is idempotent."""
        task = BoardTask(title="Test Task")
        collab_store.create_board_task(task)

        now1 = utc_now_iso()
        collab_store.update_board_task(task.id, {"archived_at": now1})

        row1 = collab_store.get_board_task(task.id)
        archived_at_1 = row1["archived_at"]

        # Archive again (should be no-op or same result)
        row2 = collab_store.get_board_task(task.id)
        assert row2["archived_at"] == archived_at_1
        assert row2["archived"] is True

    def test_restore_archived_board_task(self, collab_store):
        """Test idempotent restoration of archived tasks."""
        task = BoardTask(title="Restore Me")
        collab_store.create_board_task(task)

        # 1. Restore on unarchived task is a no-op returning False
        assert collab_store.restore_archived_board_task(task.id) is False

        # 2. Archive it
        collab_store.update_board_task(task.id, {"archived_at": utc_now_iso()})
        assert collab_store.get_board_task(task.id)["archived"] is True

        # 3. Restore it successfully
        assert collab_store.restore_archived_board_task(task.id) is True
        assert collab_store.get_board_task(task.id)["archived"] is False
        assert collab_store.get_board_task(task.id)["archived_at"] is None

        # 4. Restoring again is idempotent, returns False
        assert collab_store.restore_archived_board_task(task.id) is False

    def test_restore_records_actor_only_when_it_restores(self, collab_store):
        team = TeamStore(collab_store._store)
        task = BoardTask(title="Restore with audit")
        collab_store.create_board_task(task)
        collab_store.update_board_task(task.id, {"archived_at": utc_now_iso()})
        before = len(team.list_events(task.id))

        assert collab_store.restore_archived_board_task(task.id, actor="emp_owner") is True
        events = team.list_events(task.id)
        assert len(events) == before + 1
        assert events[-1]["event"] == "comment"
        assert events[-1]["actor"] == "emp_owner"
        assert events[-1]["note"] == "archived_at"

        assert collab_store.restore_archived_board_task(task.id, actor="emp_owner") is False
        assert len(team.list_events(task.id)) == len(events)
