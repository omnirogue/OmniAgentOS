"""Tests for the ChatTurnBridge (P0-1/P0-2).

The bridge streams session replies into chat turns: transcript deltas become
``chat.turn.delta`` events, terminal state persists the full reply into the
chat scope and emits ``chat.turn.completed``. Close is idempotent across
concurrent writers (tailer + supervisor hook).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.chats.bridge import ChatBridgeFull, ChatTurnBridge
from omniagentos.chats.store import ChatStore
from omniagentos.collab.store import CollabStore
from omniagentos.conversations.store import ConversationStore
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


@pytest.fixture
def db(tmp_path: Path) -> SqliteStore:
    collab = make_store(CollabStore, tmp_path / "bridge.db")
    return collab._store


@pytest.fixture
def chat(db: SqliteStore) -> dict[str, Any]:
    return ChatStore(db).create_chat(title="Bridge Chat")


class _FakeDal:
    """Minimal SessionsDal stand-in: one session row, mutable state.

    Carries the fields the live-transcript resolution needs (provider /
    session_ref / project_dir / account_id) and a claude_accounts map for
    ``get_claude_account``; provider sessions pass ``provider != "claude"``.
    """

    def __init__(
        self,
        state: str = "running",
        output_text: str | None = None,
        *,
        provider: str = "claude",
        session_ref: str | None = "ref_fake",
        project_dir: str | None = "/work/fake-proj",
        account_id: str | None = "acct_fake",
        config_dir: str | None = None,
    ) -> None:
        self.session: dict[str, Any] = {
            "id": "ses_fake",
            "state": state,
            "output_text": output_text,
            "provider": provider,
            "session_ref": session_ref,
            "project_dir": project_dir,
            "account_id": account_id,
        }
        self.accounts: dict[str, dict[str, Any]] = {}
        if account_id is not None and config_dir is not None:
            self.accounts[account_id] = {"id": account_id, "config_dir": config_dir}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if session_id == self.session["id"]:
            return dict(self.session)
        return None

    def get_claude_account(self, account_id: str) -> dict[str, Any] | None:
        row = self.accounts.get(account_id)
        return dict(row) if row else None


def _write_cli_transcript(
    config_dir: Path, project_dir: str, session_ref: str, lines: list[dict[str, Any]]
) -> Path:
    """Write stream-json lines to the LIVE CLI transcript path the resolver expects."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", project_dir)
    path = config_dir / "projects" / slug / f"{session_ref}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


def _assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _wait_for(predicate: Any, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestCloseTurnIdempotency:
    def test_concurrent_close_produces_one_row(
        self, db: SqliteStore, chat: dict[str, Any]
    ) -> None:
        bridge = ChatTurnBridge()
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 3, store=db, model="grok-4"
        )
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def closer() -> None:
            try:
                barrier.wait(timeout=5)
                bridge.close_turn("ses_fake", final_text="The answer is 42.")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=closer), threading.Thread(target=closer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors

        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "The answer is 42."
        assert agents[0]["meta"]["session_id"] == "ses_fake"
        assert agents[0]["meta"]["turn"] == 3

    def test_losing_close_waits_for_winner_to_finish_persistence(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent no-op close cannot return ahead of the winning writer."""
        bridge = ChatTurnBridge()
        bridge.register(
            chat["id"],
            "ses_fake",
            chat["board_task_id"],
            4,
            store=db,
            dal=_FakeDal(state="running"),
        )
        with bridge._lock:
            bridge._turns["ses_fake"].texts.append("settled answer")

        original_persist = bridge._persist_agent_turn
        winner_entered = threading.Event()
        release_winner = threading.Event()
        loser_returned = threading.Event()

        def blocked_persist(turn: Any, text: str, meta: dict[str, Any]) -> bool:
            winner_entered.set()
            assert release_winner.wait(timeout=5)
            return original_persist(turn, text, meta)

        monkeypatch.setattr(bridge, "_persist_agent_turn", blocked_persist)
        winner = threading.Thread(target=bridge.close_turn, args=("ses_fake",))

        def losing_close() -> None:
            bridge.close_turn("ses_fake")
            loser_returned.set()

        loser = threading.Thread(target=losing_close)
        winner.start()
        assert winner_entered.wait(timeout=5)
        loser.start()
        assert not loser_returned.wait(timeout=0.1)
        assert bridge.open_turn_count() == 1
        release_winner.set()
        winner.join(timeout=5)
        loser.join(timeout=5)
        assert not winner.is_alive()
        assert not loser.is_alive()
        assert loser_returned.is_set()
        assert bridge.open_turn_count() == 0

        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "settled answer"

    def test_close_without_register_is_noop(self, db: SqliteStore) -> None:
        ChatTurnBridge().close_turn("ses_never_registered", final_text="x")
        # no exception, nothing written — nothing to assert beyond the no-op

    def test_second_close_does_not_duplicate(
        self, db: SqliteStore, chat: dict[str, Any]
    ) -> None:
        bridge = ChatTurnBridge()
        bridge.register(chat["id"], "ses_fake", chat["board_task_id"], 1, store=db)
        bridge.close_turn("ses_fake", final_text="done")
        bridge.close_turn("ses_fake", final_text="done again")
        rows = ConversationStore(db).read("chat", chat["id"])
        assert len([row for row in rows if row["role"] == "agent"]) == 1

    def test_completed_event_emitted(
        self, db: SqliteStore, chat: dict[str, Any]
    ) -> None:
        bridge = ChatTurnBridge()
        bridge.register(chat["id"], "ses_fake", chat["board_task_id"], 1, store=db)
        bridge.close_turn("ses_fake", final_text="final")
        rows = db._connection.execute(
            "SELECT type, payload_json FROM events WHERE type = 'chat.turn.completed'"
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["chat_id"] == chat["id"]
        assert payload["task_id"] == chat["board_task_id"]
        assert payload["turn"] == 1
        assert payload["text"] == "final"
        assert "ts" in payload


class TestRegistryBounds:
    def test_registry_cap(self, db: SqliteStore, chat: dict[str, Any]) -> None:
        bridge = ChatTurnBridge()
        for i in range(ChatTurnBridge.MAX_OPEN_TURNS):
            bridge.register(chat["id"], f"ses_{i}", chat["board_task_id"], i, store=db)
        assert bridge.open_turn_count() == ChatTurnBridge.MAX_OPEN_TURNS
        with pytest.raises(ChatBridgeFull):
            bridge.register(chat["id"], "ses_overflow", chat["board_task_id"], 99, store=db)

    def test_disabled_bridge_noop(
        self, db: SqliteStore, chat: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_CHAT_BRIDGE", "0")
        bridge = ChatTurnBridge()
        bridge.register(chat["id"], "ses_fake", chat["board_task_id"], 1, store=db)
        assert bridge.open_turn_count() == 0

    def test_turn_timeout_persists_partial_with_flag(
        self, db: SqliteStore, chat: dict[str, Any]
    ) -> None:
        bridge = ChatTurnBridge()
        bridge.register(chat["id"], "ses_fake", chat["board_task_id"], 2, store=db)
        with bridge._lock:
            turn = bridge._turns["ses_fake"]
            turn.texts.append("partial answer so far")
            turn.started_at = time.monotonic() - (ChatTurnBridge.TURN_TIMEOUT_SECONDS + 1)
        bridge.close_turn("ses_fake")
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "partial answer so far"
        assert agents[0]["meta"]["timed_out"] is True


class TestTailer:
    def test_streams_deltas_and_closes_on_terminal(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        bridge = ChatTurnBridge()
        config_dir = tmp_path / "claude-config"
        dal = _FakeDal(state="running", config_dir=str(config_dir))
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        # Wait for the tailer thread to enter its poll loop before writing
        # the transcript, so the first poll sees the file (Q-FIX-02).
        assert bridge._thread_polling.wait(timeout=5.0)
        _write_cli_transcript(
            config_dir,
            "/work/fake-proj",
            "ref_fake",
            [_assistant("Hello"), _assistant(" world")],
        )
        assert _wait_for(lambda: bridge.open_turn_count() == 1)
        # deltas emitted for the assistant text
        assert _wait_for(
            lambda: db._connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE type = 'chat.turn.delta'"
            ).fetchone()["n"]
            >= 1,
            timeout=10.0,
        )
        # session goes terminal → tailer closes the turn and persists the reply
        dal.session["state"] = "completed"
        assert _wait_for(lambda: bridge.open_turn_count() == 0, timeout=10.0)
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "Hello\n\nworld"

    def test_result_event_preferred_as_final_text(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        bridge = ChatTurnBridge()
        config_dir = tmp_path / "claude-config"
        dal = _FakeDal(state="running", config_dir=str(config_dir))
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        assert bridge._thread_polling.wait(timeout=5.0)
        _write_cli_transcript(
            config_dir,
            "/work/fake-proj",
            "ref_fake",
            [_assistant("interim"), {"type": "result", "result": "final answer"}],
        )
        dal.session["state"] = "completed"
        assert _wait_for(lambda: bridge.open_turn_count() == 0, timeout=10.0)
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "final answer"

    def test_close_drains_transcript_without_tailer_poll(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Supervisor-first close: the reply lands even if the tailer never polled.

        This is the confirmed production failure mode — the supervisor's
        ``_finish`` calls ``close_turn`` while the 250ms tailer has not seen the
        last transcript lines; the close-time drain must still persist the full
        reply exactly once.
        """
        bridge = ChatTurnBridge()
        config_dir = tmp_path / "claude-config"
        # Register while still running so the tailer does not close the empty
        # turn before the transcript exists; then close synchronously.
        dal = _FakeDal(state="running", config_dir=str(config_dir))
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        _write_cli_transcript(
            config_dir,
            "/work/fake-proj",
            "ref_fake",
            [_assistant("SOLO-OK"), {"type": "result", "result": "SOLO-OK"}],
        )
        bridge.close_turn("ses_fake")
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "SOLO-OK"

    def test_default_config_dir_fallback_without_account(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No account on the session → the transcript resolves under ~/.claude."""
        monkeypatch.setenv("HOME", str(tmp_path))
        bridge = ChatTurnBridge()
        dal = _FakeDal(state="running", account_id=None)
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        assert bridge._thread_polling.wait(timeout=5.0)
        _write_cli_transcript(
            tmp_path / ".claude", "/work/fake-proj", "ref_fake", [_assistant("home reply")]
        )
        dal.session["state"] = "completed"
        assert _wait_for(lambda: bridge.open_turn_count() == 0, timeout=10.0)
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "home reply"

    def test_unknown_account_falls_back_gracefully(
        self,
        db: SqliteStore,
        chat: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An account_id the lookup cannot resolve degrades to ~/.claude."""
        monkeypatch.setenv("HOME", str(tmp_path))
        bridge = ChatTurnBridge()
        # account_id present but no row in the accounts map → lookup misses.
        dal = _FakeDal(state="running", account_id="acct_missing")
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        assert bridge._thread_polling.wait(timeout=5.0)
        _write_cli_transcript(
            tmp_path / ".claude", "/work/fake-proj", "ref_fake", [_assistant("fallback ok")]
        )
        dal.session["state"] = "completed"
        assert _wait_for(lambda: bridge.open_turn_count() == 0, timeout=10.0)
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "fallback ok"

    def test_provider_session_yields_nothing(
        self, db: SqliteStore, chat: dict[str, Any], tmp_path: Path
    ) -> None:
        """Non-claude sessions resolve no live transcript: no deltas, close
        persists only the DAL's output_text fallback."""
        bridge = ChatTurnBridge()
        dal = _FakeDal(state="completed", output_text="provider reply", provider="codex")
        bridge.register(
            chat["id"], "ses_fake", chat["board_task_id"], 1, store=db, dal=dal
        )
        bridge.close_turn("ses_fake")
        rows = ConversationStore(db).read("chat", chat["id"])
        agents = [row for row in rows if row["role"] == "agent"]
        assert len(agents) == 1
        assert agents[0]["content"] == "provider reply"
        assert (
            db._connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE type = 'chat.turn.delta'"
            ).fetchone()["n"]
            == 0
        )
