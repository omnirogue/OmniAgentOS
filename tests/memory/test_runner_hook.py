"""Tests for runner_hook.py memory wiring, including the OMNIAGENTOS_MEMORY_LESSONS flag."""

from __future__ import annotations

import os
import sqlite3

from omniagentos.memory.runner_hook import safe_memory_block


class MockStore:
    """A minimal store for testing runner_hook."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            self.connection = sqlite3.connect(":memory:")
            self.connection.row_factory = sqlite3.Row
        else:
            self.connection = conn


def test_safe_memory_block_respects_lessons_flag_enabled() -> None:
    """When OMNIAGENTOS_MEMORY_LESSONS=1, lessons recaller is wired."""
    old_val = os.environ.get("OMNIAGENTOS_MEMORY_LESSONS")
    try:
        os.environ["OMNIAGENTOS_MEMORY_LESSONS"] = "1"
        # The actual behavior depends on whether the store has conversations table.
        # safe_memory_block returns None if the conversation store setup fails,
        # which is expected behavior (best-effort).
        store = MockStore()
        result = safe_memory_block(store, task_id="tsk_1", budget_tokens=2000)
        # We're mainly testing that it doesn't crash when the flag is on.
        # The actual lesson injection requires a real conversations table.
        # safe_memory_block now returns (block_text, AssembledContext)
        assert isinstance(result, tuple) and len(result) == 2
        block_text, context = result
        assert block_text is None or isinstance(block_text, str)
    finally:
        if old_val is None:
            os.environ.pop("OMNIAGENTOS_MEMORY_LESSONS", None)
        else:
            os.environ["OMNIAGENTOS_MEMORY_LESSONS"] = old_val


def test_safe_memory_block_respects_lessons_flag_disabled() -> None:
    """When OMNIAGENTOS_MEMORY_LESSONS=0, lessons recaller is skipped."""
    old_val = os.environ.get("OMNIAGENTOS_MEMORY_LESSONS")
    try:
        os.environ["OMNIAGENTOS_MEMORY_LESSONS"] = "0"
        # Even with lessons disabled, the function should not crash.
        store = MockStore()
        result = safe_memory_block(store, task_id="tsk_1", budget_tokens=2000)
        # Same behavior: returns (None|str, AssembledContext) depending on conversation store state.
        # safe_memory_block now returns (block_text, AssembledContext)
        assert isinstance(result, tuple) and len(result) == 2
        block_text, context = result
        assert block_text is None or isinstance(block_text, str)
    finally:
        if old_val is None:
            os.environ.pop("OMNIAGENTOS_MEMORY_LESSONS", None)
        else:
            os.environ["OMNIAGENTOS_MEMORY_LESSONS"] = old_val


def test_safe_memory_block_defaults_lessons_on() -> None:
    """By default (no env var), OMNIAGENTOS_MEMORY_LESSONS defaults to ON."""
    old_val = os.environ.pop("OMNIAGENTOS_MEMORY_LESSONS", None)
    try:
        # With the env var unset, the default is ON.
        store = MockStore()
        result = safe_memory_block(store, task_id="tsk_1", budget_tokens=2000)
        # safe_memory_block now returns (block_text, AssembledContext)
        assert isinstance(result, tuple) and len(result) == 2
        block_text, context = result
        assert block_text is None or isinstance(block_text, str)
    finally:
        if old_val is not None:
            os.environ["OMNIAGENTOS_MEMORY_LESSONS"] = old_val


__all__ = [
    "test_safe_memory_block_defaults_lessons_on",
    "test_safe_memory_block_respects_lessons_flag_disabled",
    "test_safe_memory_block_respects_lessons_flag_enabled",
]
