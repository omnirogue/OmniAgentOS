"""Attempt close hook seam: narrow, side-effect-free hook registry invoked after
CAS succeeds, with exception-safety wrapping (E0-hookseam)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    # A CollabStore-migrated template copy carries the shared schema
    # without re-applying all 86 migrations per test.
    return migrated_db(CollabStore, tmp_path / "swarm-dal.db")


def _card(collab: CollabStore, title: str) -> str:
    task = BoardTask(title=title, status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    return task.id


def _make_run(dal: SwarmDal, **overrides: object) -> str:
    defaults = {"working_dir": "/tmp/ws", "goal": "test goal"}
    defaults.update(overrides)
    return str(dal.create_run(**defaults, source="test")["id"])


class TestAttemptCloseHookSeam:
    """E0-hookseam: hook registry for terminal state after CAS succeeds."""

    def test_hook_registry_empty_by_default(self, db_path: str) -> None:
        """Behavior is identical to today with no hooks registered."""
        dal = SwarmDal(db_path)
        try:
            assert dal._attempt_close_hooks == []
        finally:
            dal.close()

    def test_hook_receives_terminal_state_after_close(self, db_path: str) -> None:
        """A registered hook receives the attempt's terminal state after CAS succeeds."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register a hook that captures the terminal state
            captured_states = []

            def capture_state(terminal_state: dict[str, object]) -> None:
                captured_states.append(terminal_state.copy())

            dal.register_attempt_close_hook(capture_state)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            assert dal.close_attempt(attempt["id"], "completed") is True

            # Verify hook was called and received the terminal state
            assert len(captured_states) == 1
            terminal_state = captured_states[0]
            assert terminal_state["id"] == attempt["id"]
            assert terminal_state["end_reason"] == "completed"
            assert terminal_state["ended_at"] is not None
        finally:
            dal.close()

    def test_hook_not_called_when_cas_fails(self, db_path: str) -> None:
        """A registered hook is not invoked when close_attempt returns False (CAS fails)."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register a hook
            call_count = [0]

            def count_calls(terminal_state: dict[str, object]) -> None:
                call_count[0] += 1

            dal.register_attempt_close_hook(count_calls)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            assert dal.close_attempt(attempt["id"], "completed") is True
            assert call_count[0] == 1

            # Try to close again (CAS fails because already closed)
            assert dal.close_attempt(attempt["id"], "crashed") is False
            assert call_count[0] == 1  # Hook should not be called again
        finally:
            dal.close()

    def test_raising_hook_does_not_disturb_close_attempt_return_value(
        self, db_path: str
    ) -> None:
        """A raising hook does not affect close_attempt's return value (exception-safe)."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register a hook that raises an exception
            def raising_hook(terminal_state: dict[str, object]) -> None:
                raise RuntimeError("Hook intentionally failed")

            dal.register_attempt_close_hook(raising_hook)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            # close_attempt should succeed despite hook exception
            result = dal.close_attempt(attempt["id"], "completed")
            assert result is True

            # Verify the attempt is actually closed
            closed_attempt = dal.list_attempts(task_id)[0]
            assert closed_attempt["end_reason"] == "completed"
            assert closed_attempt["ended_at"] is not None
        finally:
            dal.close()

    def test_multiple_hooks_all_invoked(self, db_path: str) -> None:
        """All registered hooks are invoked in order."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register multiple hooks
            call_order = []

            def hook1(terminal_state: dict[str, object]) -> None:
                call_order.append(1)

            def hook2(terminal_state: dict[str, object]) -> None:
                call_order.append(2)

            def hook3(terminal_state: dict[str, object]) -> None:
                call_order.append(3)

            dal.register_attempt_close_hook(hook1)
            dal.register_attempt_close_hook(hook2)
            dal.register_attempt_close_hook(hook3)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            assert dal.close_attempt(attempt["id"], "completed") is True

            # Verify all hooks were called in order
            assert call_order == [1, 2, 3]
        finally:
            dal.close()

    def test_unregister_hook(self, db_path: str) -> None:
        """Unregistering a hook prevents it from being invoked."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register and then unregister a hook
            call_count = [0]

            def counting_hook(terminal_state: dict[str, object]) -> None:
                call_count[0] += 1

            dal.register_attempt_close_hook(counting_hook)
            dal.unregister_attempt_close_hook(counting_hook)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            assert dal.close_attempt(attempt["id"], "completed") is True

            # Verify hook was not called
            assert call_count[0] == 0
        finally:
            dal.close()

    def test_unregister_nonexistent_hook_is_idempotent(self, db_path: str) -> None:
        """Unregistering a hook that was not registered is safe (idempotent)."""
        dal = SwarmDal(db_path)
        try:
            def dummy_hook(terminal_state: dict[str, object]) -> None:
                pass

            # Should not raise
            dal.unregister_attempt_close_hook(dummy_hook)
            dal.unregister_attempt_close_hook(dummy_hook)
        finally:
            dal.close()

    def test_hook_exception_continues_other_hooks(self, db_path: str) -> None:
        """An exception in one hook does not prevent other hooks from running."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register hooks where the second one raises
            call_order = []

            def hook1(terminal_state: dict[str, object]) -> None:
                call_order.append(1)

            def failing_hook(terminal_state: dict[str, object]) -> None:
                call_order.append(2)
                raise RuntimeError("Hook 2 failed")

            def hook3(terminal_state: dict[str, object]) -> None:
                call_order.append(3)

            dal.register_attempt_close_hook(hook1)
            dal.register_attempt_close_hook(failing_hook)
            dal.register_attempt_close_hook(hook3)

            # Open and close an attempt
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            result = dal.close_attempt(attempt["id"], "completed")

            # close_attempt should succeed
            assert result is True
            # All hooks should have been attempted
            assert call_order == [1, 2, 3]
        finally:
            dal.close()

    def test_terminal_state_contains_all_attempt_fields(self, db_path: str) -> None:
        """The terminal state dict contains all attempt fields including detail."""
        collab = CollabStore(db_path)
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            task_id = _card(collab, "test task")
            dal.assign_task_to_run(task_id, run_id)

            # Register a hook
            captured_states = []

            def capture_state(terminal_state: dict[str, object]) -> None:
                captured_states.append(terminal_state.copy())

            dal.register_attempt_close_hook(capture_state)

            # Open and close an attempt with detail
            attempt = dal.open_attempt(
                run_id, task_id, provider="claude", model="sonnet", source="test"
            )
            detail_msg = "test failure reason"
            assert dal.close_attempt(
                attempt["id"], "crashed", detail_msg
            ) is True

            # Verify terminal state contains expected fields
            assert len(captured_states) == 1
            terminal_state = captured_states[0]
            assert terminal_state["id"] == attempt["id"]
            assert terminal_state["end_reason"] == "crashed"
            assert terminal_state["detail"] == detail_msg
            assert terminal_state["ended_at"] is not None
            assert terminal_state["started_at"] is not None
            assert terminal_state["provider"] == "claude"
            assert terminal_state["model"] == "sonnet"
        finally:
            dal.close()
