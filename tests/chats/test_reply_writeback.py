"""Tests for chat reply write-back via safe_persist_agent_turn.

Verifies that agent turns are dual-written to both the task scope AND the
chat scope when the board task originated from a chat workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.chats import ChatStore
from omniagentos.collab.store import CollabStore
from omniagentos.conversations.store import ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.memory.runner_hook import (
    safe_persist_agent_turn,
    safe_persist_chat_agent_turn,
)
from tests.support.db_template import make_store


@pytest.fixture
def db(tmp_path: Path) -> SqliteStore:
    collab = make_store(CollabStore, tmp_path / "reply_wb.db")
    return collab._store


@pytest.fixture
def chat_store(db: SqliteStore) -> ChatStore:
    return ChatStore(db)


@pytest.fixture
def conv_store(db: SqliteStore) -> ConversationStore:
    return ConversationStore(db)


class TestReplyWriteBack:
    """safe_persist_agent_turn dual-writes to chat scope when board task is a chat companion."""

    def test_dual_write_with_explicit_board_task_id(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """When board_task_id is passed explicitly, the agent turn is written to chat scope."""
        chat = chat_store.create_chat(title="Test Chat")
        chat_id = chat["id"]
        btk_id = chat["board_task_id"]
        task_id = "tsk_ctrl_123"

        safe_persist_agent_turn(
            db,
            task_id=task_id,
            content="Here is my analysis of the tasks.",
            model="gemini-3.6-flash",
            board_task_id=btk_id,
        )

        # Verify: written to task scope
        task_turns = conv_store.read("task", task_id)
        assert len(task_turns) == 1
        assert task_turns[0]["role"] == "agent"
        assert task_turns[0]["content"] == "Here is my analysis of the tasks."
        assert task_turns[0]["model"] == "gemini-3.6-flash"

        # Verify: ALSO written to chat scope
        chat_turns = conv_store.read("chat", chat_id)
        assert len(chat_turns) == 1
        assert chat_turns[0]["role"] == "agent"
        assert chat_turns[0]["content"] == "Here is my analysis of the tasks."
        assert chat_turns[0]["model"] == "gemini-3.6-flash"

    def test_no_dual_write_for_non_chat_board_task(
        self, db: SqliteStore, conv_store: ConversationStore
    ) -> None:
        """When the board task is NOT a chat companion, no chat write-back occurs."""
        task_id = "tsk_non_chat"

        # Create a board task with origin='board' (not chat)
        db._connection.execute(
            "INSERT INTO board_tasks "
            "(id, title, description, required_expertise_json, discipline, "
            "priority, status, claimed_by, claim_version, result_ref, "
            "created_at, updated_at, origin) "
            "VALUES (?, 'Regular task', '', '[]', NULL, 'normal', 'open', "
            "NULL, 0, NULL, 'now', 'now', 'board')",
            ("btk_regular",),
        )
        db._connection.commit()

        safe_persist_agent_turn(
            db,
            task_id=task_id,
            content="Agent output.",
            board_task_id="btk_regular",
        )

        # Written to task scope
        task_turns = conv_store.read("task", task_id)
        assert len(task_turns) == 1

        # NOT written to any chat scope — no chat exists with this board task
        all_conversations = db._connection.execute(
            "SELECT * FROM conversations WHERE scope_type = 'chat'"
        ).fetchall()
        assert len(all_conversations) == 0

    def test_no_dual_write_when_board_task_id_missing(
        self, db: SqliteStore, conv_store: ConversationStore
    ) -> None:
        """Without board_task_id and no run to resolve from, no chat write-back."""
        task_id = "tsk_no_btk"

        safe_persist_agent_turn(
            db,
            task_id=task_id,
            content="Output without board context.",
        )

        task_turns = conv_store.read("task", task_id)
        assert len(task_turns) == 1

        chat_turns = db._connection.execute(
            "SELECT * FROM conversations WHERE scope_type = 'chat'"
        ).fetchall()
        assert len(chat_turns) == 0

    def test_resolve_from_run_chain(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """When board_task_id is omitted, resolve via runs → board_tasks → chats."""
        chat = chat_store.create_chat(title="Chain Test")
        chat_id = chat["id"]
        btk_id = chat["board_task_id"]
        task_id = "tsk_chain"

        # Create a run linked to the task
        run_id = "run_chain_test"
        db._connection.execute(
            "INSERT INTO tasks (id, title, state, created_at, updated_at, input_json) "
            "VALUES (?, 'Chain Task', 'completed', 'now', 'now', '{}')",
            (task_id,),
        )
        db._connection.execute(
            "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
            "created_at, updated_at) "
            "VALUES (?, ?, 'agent', 'trace_chain_test', 'completed', 'now', 'now', 'now')",
            (run_id, task_id),
        )
        # Link the board task to this run
        db._connection.execute(
            "UPDATE board_tasks SET run_id = ? WHERE id = ?",
            (run_id, btk_id),
        )
        db._connection.commit()

        safe_persist_agent_turn(
            db,
            task_id=task_id,
            content="Resolved via run chain.",
        )

        # Both scopes should have the turn
        task_turns = conv_store.read("task", task_id)
        assert len(task_turns) == 1

        chat_turns = conv_store.read("chat", chat_id)
        assert len(chat_turns) == 1
        assert chat_turns[0]["content"] == "Resolved via run chain."

    def test_empty_content_no_write(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """Empty content is silently skipped — no turn in any scope."""
        chat = chat_store.create_chat(title="Empty Test")

        safe_persist_agent_turn(
            db,
            task_id="tsk_empty",
            content="",
            board_task_id=chat["board_task_id"],
        )

        task_turns = conv_store.read("task", "tsk_empty")
        assert len(task_turns) == 0

    def test_multiple_turns_ordered(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """Multiple agent turns maintain seq order in chat scope."""
        chat = chat_store.create_chat(title="Multi-Turn")
        chat_id = chat["id"]
        btk_id = chat["board_task_id"]

        # Simulate a user message first
        conv_store.append("chat", chat_id, "user", "What should I do?")

        for i in range(3):
            safe_persist_agent_turn(
                db,
                task_id=f"tsk_multi_{i}",
                content=f"Agent response {i}",
                board_task_id=btk_id,
            )

        chat_turns = conv_store.read("chat", chat_id)
        # 1 user + 3 agent = 4
        assert len(chat_turns) == 4
        assert chat_turns[0]["role"] == "user"
        assert chat_turns[1]["role"] == "agent"
        assert chat_turns[1]["content"] == "Agent response 0"
        assert chat_turns[3]["content"] == "Agent response 2"


class TestSafeChatAgentTurn:
    """safe_persist_chat_agent_turn writes to chat scope and optionally task scope."""

    def test_chat_only(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """Without board_task_id, writes only to chat scope."""
        chat = chat_store.create_chat(title="Chat Only")
        chat_id = chat["id"]

        safe_persist_chat_agent_turn(
            db,
            chat_id=chat_id,
            content="Direct chat reply.",
            model="gemini-3.6-flash",
        )

        chat_turns = conv_store.read("chat", chat_id)
        assert len(chat_turns) == 1
        assert chat_turns[0]["content"] == "Direct chat reply."

    def test_chat_and_task(
        self, db: SqliteStore, chat_store: ChatStore, conv_store: ConversationStore
    ) -> None:
        """With board_task_id, writes to both chat and task scope."""
        chat = chat_store.create_chat(title="Chat + Task")
        chat_id = chat["id"]
        btk_id = chat["board_task_id"]

        safe_persist_chat_agent_turn(
            db,
            chat_id=chat_id,
            content="Dual write.",
            board_task_id=btk_id,
        )

        chat_turns = conv_store.read("chat", chat_id)
        assert len(chat_turns) == 1
        assert chat_turns[0]["content"] == "Dual write."

        task_turns = conv_store.read("task", btk_id)
        assert len(task_turns) == 1
        assert task_turns[0]["content"] == "Dual write."
