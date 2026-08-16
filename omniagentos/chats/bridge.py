"""Chat turn bridge — streams live session replies into chat turns (P0-1/P0-2).

The v1 defect: chat sends dispatch ``execute="session"``, which spawns a live
session and creates NO run — so the runner-lane reply write-back
(``safe_persist_agent_turn``) never fires and ``chat.turn.delta`` /
``chat.turn.completed`` are never emitted. This bridge is the session-lane
reply pipeline:

* ``POST /api/chats/{id}/messages`` calls :meth:`ChatTurnBridge.register`
  with the dispatched session id.
* ONE shared daemon tailer thread (lazy start, idle-exit after 60s with an
  empty registry) polls each open session's transcript every 250ms through
  the shared byte-offset + rotation-guard read
  (:mod:`omniagentos.sessions.transcript_delta`). New assistant text
  coalesces into ``chat.turn.delta`` events, throttled to <=4/s per chat.
* On terminal session state (tailer-observed) or from the supervisor's
  ``_finish`` on COMPLETED, :meth:`ChatTurnBridge.close_turn` appends the
  full assistant text via ``safe_persist_chat_agent_turn`` and emits
  ``chat.turn.completed``. The write is idempotent across the two writers
  (and across processes): the append runs in the same lock-held read as a
  ``json_extract(meta_json,'$.session_id')`` dedupe probe.

Bounds: a hard 15-minute per-turn timeout (partial text persisted with
``meta.timed_out=true``), a 32-open-turn registry cap (the 33rd send gets a
503 from the route), one thread total, no file handle held across polls.

Set ``OMNIAGENTOS_CHAT_BRIDGE=0`` to disable the bridge entirely (register
becomes a no-op); the client's 6s poll fallback then carries replies.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.sessions.transcript import live_transcript_path
from omniagentos.sessions.transcript_delta import (
    assistant_text,
    read_transcript_delta,
    result_text,
)

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger(__name__)

_ACTIVE_SESSION_STATES = frozenset(
    {"queued", "planning", "starting", "running", "awaiting_approval", "resuming"}
)


class ChatBridgeFull(RuntimeError):
    """The open-turn registry is at capacity; the route answers 503."""


@dataclass
class _OpenTurn:
    chat_id: str
    session_id: str
    task_id: str
    turn: int
    store: Any
    model: str | None = None
    dal: Any | None = None
    offset: int = 0
    texts: list[str] = field(default_factory=list)
    final_result: str | None = None
    pending_delta: str = ""
    last_delta_at: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    missing_polls: int = 0


class ChatTurnBridge:
    """Process-local singleton streaming session replies into chat scopes."""

    MAX_OPEN_TURNS = 32
    POLL_SECONDS = 0.25
    IDLE_EXIT_SECONDS = 60.0
    TURN_TIMEOUT_SECONDS = 15 * 60.0
    DELTA_MIN_INTERVAL_SECONDS = 0.25  # pinned: <=4 deltas/s per chat
    # A session row that never appears (dispatch mocked away, spawn failed
    # before insert) gets one minute of polls before the turn is closed.
    MISSING_SESSION_CLOSE_POLLS = 240

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, _OpenTurn] = {}
        self._closing: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._idle_since: float | None = None
        self._dal_provider: Callable[[], Any] | None = None
        self._dal: Any = None
        # Signalled once the tailer thread enters its poll loop; tests use this
        # to synchronise with the daemon thread before relying on poll-driven
        # side effects (Q-FIX-02 — eliminates flaky bridge tailer test).
        self._thread_polling = threading.Event()
        # Set by stop() to break the tailer out of its poll loop at the next
        # iteration (test teardown / process shutdown). register() clears it
        # before starting a fresh thread so a new tailer never sees a stale
        # signal. Lifecycle only — normal streaming never touches it.
        self._stop_event = threading.Event()

    # -- configuration (tests) ------------------------------------------------

    def set_sessions_dal_provider(self, provider: Callable[[], Any] | None) -> None:
        """Inject a SessionsDal factory (tests); None restores the default."""
        with self._lock:
            self._dal_provider = provider
            self._dal = None

    def _sessions_dal(self) -> Any | None:
        with self._lock:
            if self._dal is not None:
                return self._dal
            provider = self._dal_provider
        try:
            if provider is not None:
                dal = provider()
            else:
                from omniagentos.sessions.dal import SessionsDal

                dal = SessionsDal(default_db_path())
        except Exception:  # noqa: BLE001
            _LOG.debug("chat bridge: sessions DAL unavailable", exc_info=True)
            return None
        with self._lock:
            if self._dal is None:
                self._dal = dal
            return self._dal

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("OMNIAGENTOS_CHAT_BRIDGE", "1") not in {"0", "false", "off"}

    # -- registry ---------------------------------------------------------------

    def register(
        self,
        chat_id: str,
        session_id: str,
        task_id: str,
        turn: int,
        *,
        store: Any,
        model: str | None = None,
        dal: Any | None = None,
    ) -> None:
        """Open a turn: stream ``session_id``'s assistant text into ``chat_id``.

        Raises :class:`ChatBridgeFull` when 32 turns are already open. A no-op
        when the bridge is disabled or ``session_id`` is empty. ``dal`` pins
        the SessionsDal this turn polls (tests bind it to the API store's own
        database); when omitted the process default is used.
        """
        if not session_id or not self.enabled():
            return
        with self._lock:
            if session_id in self._turns or session_id in self._closing:
                return
            if len(self._turns) >= self.MAX_OPEN_TURNS:
                raise ChatBridgeFull(
                    f"{self.MAX_OPEN_TURNS} chat turns already open; wait for one to finish"
                )
            self._turns[session_id] = _OpenTurn(
                chat_id=chat_id,
                session_id=session_id,
                task_id=task_id,
                turn=turn,
                store=store,
                model=model,
                dal=dal,
            )
            self._idle_since = None
            if self._thread is None or not self._thread.is_alive():
                self._thread_polling.clear()
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._tail_loop,
                    name="chat-turn-bridge",
                    daemon=True,
                )
                self._thread.start()

    def open_turn_count(self) -> int:
        with self._lock:
            # A claimed close remains observable until its persistence and
            # terminal-event work has completed.  Callers use zero as the
            # completion barrier before reading the conversation store.
            return len(self._turns) + len(self._closing)

    # -- lifecycle --------------------------------------------------------------

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop the tailer daemon thread and drop open turns (idempotent).

        A lifecycle/shutdown hook only — test teardown and process shutdown.
        Signals the poll loop to exit at its next iteration (at most
        ``POLL_SECONDS`` away) and joins the thread with a short timeout. Safe
        to call when no thread was ever started. Streaming behaviour is
        unchanged: a later :meth:`register` clears the signal and starts a
        fresh thread exactly as before.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
            self._turns.clear()
            self._idle_since = None
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread_polling.clear()

    # -- close ------------------------------------------------------------------

    def close_turn(self, session_id: str, *, final_text: str | None = None) -> None:
        """Close a turn idempotently: persist the reply, emit ``chat.turn.completed``.

        Both the tailer (terminal state observed) and the supervisor's
        ``_finish`` call this; only the first call persists. Timed-out turns
        persist their partial text with ``meta.timed_out=true``. A concurrent
        loser waits for the winning close to finish so returning from this method
        always means persistence and terminal-event emission have settled.
        """
        with self._lock:
            turn = self._turns.pop(session_id, None)
            completion = self._closing.get(session_id)
            if turn is not None:
                completion = threading.Event()
                self._closing[session_id] = completion
        if turn is None:
            if completion is not None:
                completion.wait()
            return

        assert completion is not None
        try:
            self._close_claimed_turn(turn, final_text=final_text)
        finally:
            # Publish completion only after the winner has finished every write
            # and event. Set before removing so a concurrent loser can always
            # observe either a waitable event or an already-complete close.
            completion.set()
            with self._lock:
                self._closing.pop(session_id, None)

    def _close_claimed_turn(
        self, turn: _OpenTurn, *, final_text: str | None = None
    ) -> None:
        """Finish a turn after :meth:`close_turn` has claimed sole ownership."""
        session_id = turn.session_id
        text = (final_text or "").strip() or (turn.final_result or "").strip()
        if not text:
            # The supervisor's _finish may fire before the tailer's last poll —
            # drain the live transcript directly so the reply is never lost to
            # that race (Q-FIX: deltas streamed from the WRONG file before the
            # live-transcript resolution landed, and close persisted nothing).
            session = self._session_row(turn)
            if session is not None:
                self._drain_transcript(turn, session)
            text = (turn.final_result or "").strip()
        if not text:
            text = "\n\n".join(t for t in turn.texts if t.strip()).strip()
        if not text:
            text = self._session_output_text(turn) or ""
        # Flush any unsent delta so late subscribers see the full text.
        timed_out = time.monotonic() - turn.started_at >= self.TURN_TIMEOUT_SECONDS
        meta: dict[str, Any] = {"session_id": session_id, "turn": turn.turn}
        if timed_out:
            meta["timed_out"] = True
        persisted = False
        if text:
            persisted = self._persist_agent_turn(turn, text, meta)
        payload: dict[str, Any] = {
            "text": text,
            "model": turn.model,
            "session_id": session_id,
            "ts": utc_now_iso(),
        }
        if not text:
            # A session that died with no assistant text (auth failure, crash)
            # still tells the client WHY — the UI renders "The agent stopped:
            # <reason>" instead of an empty bubble.
            session = self._session_row(turn)
            error = str(session.get("error") or "") if session else ""
            if error:
                payload["error"] = error
        if timed_out:
            payload["timed_out"] = True
        if not persisted and text:
            # Another writer (supervisor / second process) already stored this
            # turn's reply; the event still fires so clients settle.
            payload["deduped"] = True
        self._emit(turn, "chat.turn.completed", payload)

    def _persist_agent_turn(self, turn: _OpenTurn, text: str, meta: dict[str, Any]) -> bool:
        """Idempotent write: probe + append under one writer-lock hold."""
        from omniagentos.memory.runner_hook import safe_persist_chat_agent_turn

        store = turn.store
        try:
            with store._lock:
                row = store._connection.execute(
                    "SELECT 1 FROM conversations "
                    "WHERE scope_type = 'chat' AND scope_id = ? "
                    "AND json_extract(meta_json, '$.session_id') = ? "
                    "AND role = 'agent' LIMIT 1",
                    (turn.chat_id, turn.session_id),
                ).fetchone()
                if row is not None:
                    return False
                safe_persist_chat_agent_turn(
                    store,
                    chat_id=turn.chat_id,
                    content=text,
                    model=turn.model,
                    board_task_id=turn.task_id or None,
                    meta=meta,
                )
                return True
        except Exception:  # noqa: BLE001
            _LOG.debug("chat bridge: persist failed for %s", turn.chat_id, exc_info=True)
            return False

    # -- tailer -----------------------------------------------------------------

    def _tail_loop(self) -> None:
        self._thread_polling.set()
        while True:
            if self._stop_event.is_set():
                return
            with self._lock:
                sessions = list(self._turns.values())
            if not sessions:
                with self._lock:
                    if self._idle_since is None:
                        self._idle_since = time.monotonic()
                    idle_for = time.monotonic() - self._idle_since
                    still_empty = not self._turns
                if still_empty and idle_for >= self.IDLE_EXIT_SECONDS:
                    return
                time.sleep(self.POLL_SECONDS)
                continue
            with self._lock:
                self._idle_since = None
            for turn in sessions:
                try:
                    self._poll_turn(turn)
                except Exception:  # noqa: BLE001
                    _LOG.debug("chat bridge: poll failed for %s", turn.session_id, exc_info=True)
            time.sleep(self.POLL_SECONDS)

    def _poll_turn(self, turn: _OpenTurn) -> None:
        now = time.monotonic()
        if now - turn.started_at >= self.TURN_TIMEOUT_SECONDS:
            self.close_turn(turn.session_id)
            return

        session = self._session_row(turn)

        # 1. Transcript delta: the LIVE CLI transcript — stream-json JSONL at
        #    <account config_dir>/projects/<slug(project_dir)>/<session_ref>.jsonl,
        #    resolved through omniagentos.sessions.transcript. (The ledger
        #    sessions/<id>.jsonl manifest is the terminal AUDIT record, not the
        #    activity transcript — reading it was the v2 defect.) Provider
        #    sessions have no file and simply yield nothing.
        if session is not None:
            path = live_transcript_path(session, account_lookup=self._account_lookup_for(turn))
            if path is not None:
                delta = read_transcript_delta(path, turn.offset)
                self._ingest_delta(turn, delta, now)

        # 2. Session state: terminal -> close.
        if session is None:
            turn.missing_polls += 1
            if turn.missing_polls >= self.MISSING_SESSION_CLOSE_POLLS:
                self.close_turn(turn.session_id)
            return
        turn.missing_polls = 0
        state = str(session.get("state") or "")
        if state and state not in _ACTIVE_SESSION_STATES:
            self.close_turn(turn.session_id)

    def _ingest_delta(
        self, turn: _OpenTurn, delta: dict[str, Any], now: float, *, emit_delta: bool = True
    ) -> None:
        """Fold one transcript delta into the turn: offset, texts, final result,
        and (unless ``emit_delta`` is False — the close-time drain) the throttled
        ``chat.turn.delta`` event."""
        turn.offset = int(delta.get("new_offset") or turn.offset)
        new_texts: list[str] = []
        for entry in delta.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            text = assistant_text(entry)
            if text:
                new_texts.append(text)
            final = result_text(entry)
            if final:
                turn.final_result = final
        if new_texts:
            turn.texts.extend(new_texts)
            turn.pending_delta = (
                turn.pending_delta + "\n\n" if turn.pending_delta else ""
            ) + "\n\n".join(new_texts)
            if (
                emit_delta
                and turn.pending_delta
                and now - turn.last_delta_at >= self.DELTA_MIN_INTERVAL_SECONDS
            ):
                self._emit(
                    turn,
                    "chat.turn.delta",
                    {"text": turn.pending_delta, "model": turn.model, "ts": utc_now_iso()},
                )
                turn.pending_delta = ""
                turn.last_delta_at = now

    def _drain_transcript(self, turn: _OpenTurn, session: dict[str, Any]) -> None:
        """Close-time final read of the live transcript (no delta emission).

        Completes ``turn.texts``/``turn.final_result`` from whatever the tailer
        had not polled yet, so a supervisor-first close still persists the full
        reply. Best-effort: a resolution/read fault leaves the turn unchanged.
        """
        try:
            path = live_transcript_path(session, account_lookup=self._account_lookup_for(turn))
            if path is None:
                return
            delta = read_transcript_delta(path, turn.offset)
            self._ingest_delta(turn, delta, time.monotonic(), emit_delta=False)
        except Exception:  # noqa: BLE001
            _LOG.debug("chat bridge: final drain failed for %s", turn.session_id, exc_info=True)

    def _account_lookup_for(self, turn: _OpenTurn) -> Callable[[str], Any] | None:
        """claude_accounts lookup bound to the turn's DAL when it has one.

        SessionsDal exposes ``get_claude_account`` (account rows share the
        sessions DB); doubles that lack it fall through to the shared
        resolver's own default (accounts service → ~/.claude).
        """
        dal = turn.dal if turn.dal is not None else self._sessions_dal()
        getter = getattr(dal, "get_claude_account", None) if dal is not None else None
        return getter if callable(getter) else None

    def _session_row(self, turn: _OpenTurn) -> dict[str, Any] | None:
        dal = turn.dal if turn.dal is not None else self._sessions_dal()
        if dal is None:
            return None
        try:
            return dal.get_session(turn.session_id)
        except Exception:  # noqa: BLE001
            return None

    def _session_output_text(self, turn: _OpenTurn) -> str | None:
        session = self._session_row(turn)
        if session is None:
            return None
        text = str(session.get("output_text") or "").strip()
        return text or None

    # -- events -----------------------------------------------------------------

    def _emit(self, turn: _OpenTurn, event_type: str, payload: dict[str, Any]) -> None:
        """Write a chat SSE event to the events table (hub tailer fans it out).

        Same pinned shape as ``_emit_chat_event`` in routes/chats.py:
        ``{"chat_id", "task_id", "turn", ...payload}``.
        """
        event_payload = {
            "chat_id": turn.chat_id,
            "task_id": turn.task_id,
            "turn": turn.turn,
            **payload,
        }
        try:
            turn.store.insert_event(
                event_type,
                "api",
                "chat_turn",
                target_type="chat",
                target_id=turn.chat_id,
                payload=event_payload,
            )
        except Exception:  # noqa: BLE001
            _LOG.debug(
                "chat bridge: failed to emit %s for %s", event_type, turn.chat_id, exc_info=True
            )


_BRIDGE = ChatTurnBridge()


def get_chat_turn_bridge() -> ChatTurnBridge:
    """The process-local bridge singleton."""
    return _BRIDGE


def reset_chat_turn_bridge() -> None:
    """Stop the process singleton's tailer thread (test teardown / shutdown).

    Idempotent and safe when the bridge never started a thread. The singleton
    object is preserved and stays reusable — the next :func:`get_chat_turn_bridge`
    returns the same instance and a fresh ``register`` restarts the tailer.
    """
    _BRIDGE.stop()


__all__ = [
    "ChatBridgeFull",
    "ChatTurnBridge",
    "get_chat_turn_bridge",
    "reset_chat_turn_bridge",
]
