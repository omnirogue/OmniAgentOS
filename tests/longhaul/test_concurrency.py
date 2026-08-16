"""C-06 / H-27: cross-loop dispatch lock and SQLite transaction isolation.

These are behavioral proofs, not source-text assertions:

* C-06 — two real threads each drive their own event loop into
  ``LonghaulEngine.dispatch`` and both must complete in bounded time. The
  previous ``asyncio.Lock`` is loop-bound and deadlocks or raises when a codex
  worker thread calls ``asyncio.run`` against an engine owned by the sessions
  loop.
* H-27 — one thread opens a write transaction and later rolls it back while a
  second thread issues a raw ``commit()`` (the shape of the pre-fix
  ``_cancel_task`` path). The rolled-back work must not land.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import workbook
from omniagentos.longhaul.engine import LonghaulEngine
from omniagentos.longhaul.store import LonghaulStore


class FakeSupervisor:
    def __init__(self, *, hold_s: float = 0.0) -> None:
        self.hold_s = hold_s
        self.calls: list[dict[str, Any]] = []
        self._n = 0
        self._lock = threading.Lock()

    def spawn(self, **kwargs: Any) -> str:
        if self.hold_s > 0:
            time.sleep(self.hold_s)
        with self._lock:
            self._n += 1
            session_id = f"ses_fake_{self._n}"
        self.calls.append({**kwargs, "_session_id": session_id})
        return session_id


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "longhaul.db")
    migrate(path)
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks")
    return path


def _cfg(supervisor: FakeSupervisor | None = None, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "max_sessions": 8,
        "default_cooldown_s": 3600,
        "cross_harness_fallback": True,
        "static_fallback_order": [{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
        "review": {"enabled": False, "deny_respawns": 2},
        "spawn_grace_s": 0,
        "fast_crash_s": 0,
        "_codex_runner": lambda *_a, **_k: {"started": utc_now_iso()},
    }
    if supervisor is not None:
        cfg["_supervisor"] = supervisor
    cfg.update(overrides)
    return cfg


def _task(
    store: LonghaulStore,
    task_id: str,
    *,
    category_id: str | None = None,
    status: str = "pending",
    state: dict[str, Any] | None = None,
) -> None:
    import json

    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks "
        "(id,title,description,status,created_at,updated_at,lane,category_id,longhaul_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            f"Task {task_id}",
            "Implement the requested code.\n\nAcceptance criteria:\n- Tests pass",
            status,
            now,
            now,
            "longhaul",
            category_id,
            json.dumps(state or {}, separators=(",", ":"), sort_keys=True),
        ),
    )
    store._connection.commit()


# ---------------------------------------------------------------------------
# C-06
# ---------------------------------------------------------------------------


def test_dispatch_lock_is_thread_safe_not_loop_bound(db_path: str) -> None:
    """The dispatch mutex must not be an asyncio.Lock (loop-bound)."""
    engine = LonghaulEngine(LonghaulStore(db_path), _cfg(FakeSupervisor()), db_path)
    assert isinstance(engine._dispatch_lock, type(threading.Lock()))
    assert not isinstance(engine._dispatch_lock, asyncio.Lock)


def test_two_loops_two_threads_dispatch_complete_without_deadlock(db_path: str) -> None:
    """Two independent event loops on two threads both finish dispatch.

    This is the production shape: the sessions daemon owns one loop; a codex
    worker thread terminates via ``asyncio.run(on_session_terminal → dispatch)``
    on a second loop. With ``asyncio.Lock`` that second entry either raises
    ``RuntimeError: ... bound to a different event loop`` or hangs. With a
    threading mutex both entries complete in bounded time.
    """
    store = LonghaulStore(db_path)
    try:
        # Hold the codex runner long enough that the second loop is waiting on
        # the dispatch mutex while the first is still inside the critical section.
        def slow_codex(*_a: Any, **_k: Any) -> dict[str, str]:
            time.sleep(0.15)
            return {"started": utc_now_iso()}

        engine = LonghaulEngine(store, _cfg(FakeSupervisor(), _codex_runner=slow_codex), db_path)

        _task(store, "btk_loop_a")
        _task(store, "btk_loop_b")

        errors: list[BaseException] = []
        finished: list[str] = []
        barrier = threading.Barrier(2)

        def run_in_own_loop(task_id: str) -> None:
            try:
                barrier.wait(timeout=5)

                async def _go() -> None:
                    await engine.dispatch(task_id)

                asyncio.run(_go())
                finished.append(task_id)
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=run_in_own_loop, args=("btk_loop_a",), name="loop-a"),
            threading.Thread(target=run_in_own_loop, args=("btk_loop_b",), name="loop-b"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive(), f"{thread.name} did not finish — deadlock?"

        assert errors == [], f"cross-loop dispatch failed: {errors!r}"
        assert set(finished) == {"btk_loop_a", "btk_loop_b"}
        # Each task has exactly one durable attempt — the lock serialized opens.
        for task_id in ("btk_loop_a", "btk_loop_b"):
            attempts = store.list_attempts(task_id)
            assert len(attempts) == 1, (task_id, attempts)
    finally:
        store.close()


def test_same_task_double_dispatch_across_loops_opens_one_attempt(db_path: str) -> None:
    """Concurrent dispatch of ONE task from two loops still yields one attempt."""
    store = LonghaulStore(db_path)
    try:

        def slow_codex(*_a: Any, **_k: Any) -> dict[str, str]:
            time.sleep(0.15)
            return {"started": utc_now_iso()}

        engine = LonghaulEngine(store, _cfg(FakeSupervisor(), _codex_runner=slow_codex), db_path)
        _task(store, "btk_once")

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                asyncio.run(engine.dispatch("btk_once"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()

        assert errors == []
        assert len(store.list_attempts("btk_once")) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A5
# ---------------------------------------------------------------------------


def test_close_drains_admitted_terminal_callback_before_store_close(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown fences late delivery and waits at an explicit callback barrier."""
    store = LonghaulStore(db_path)
    engine = LonghaulEngine(store, _cfg(FakeSupervisor()), db_path)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    store_close_called = threading.Event()
    callback_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    async def blocked_callback(_session: dict, _events: list[dict], _rc: int) -> None:
        callback_entered.set()
        await asyncio.to_thread(release_callback.wait)

    real_store_close = store.close

    def tracked_store_close() -> None:
        store_close_called.set()
        real_store_close()

    monkeypatch.setattr(engine, "_on_session_terminal_admitted", blocked_callback)
    monkeypatch.setattr(store, "close", tracked_store_close)

    def deliver() -> None:
        try:
            asyncio.run(
                engine.on_session_terminal(
                    {"title": "[longhaul:tks_shutdown]", "state": "failed"}, [], 1
                )
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            callback_errors.append(exc)

    def close() -> None:
        try:
            engine.close()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            close_errors.append(exc)

    callback_thread = threading.Thread(target=deliver, name="terminal-callback")
    close_thread = threading.Thread(target=close, name="longhaul-close")
    callback_thread.start()
    assert callback_entered.wait(timeout=2)
    close_thread.start()

    try:
        with engine._terminal_condition:
            assert engine._terminal_condition.wait_for(
                lambda: engine._terminal_closing,
                timeout=2,
            )
        assert not store_close_called.is_set()

        release_callback.set()
        callback_thread.join(timeout=2)
        close_thread.join(timeout=2)
        assert not callback_thread.is_alive()
        assert not close_thread.is_alive()
        assert callback_errors == []
        assert close_errors == []
        assert store_close_called.is_set()

        # A callback arriving after the close fence is rejected before the
        # patched admitted body (and therefore before any store access).
        asyncio.run(
            engine.on_session_terminal(
                {"title": "[longhaul:tks_late]", "state": "failed"}, [], 1
            )
        )
        assert engine._rejected_terminal_callbacks == 1
        with pytest.raises(RuntimeError, match="LonghaulStore is closed"):
            _ = store._connection
    finally:
        release_callback.set()
        callback_thread.join(timeout=2)
        close_thread.join(timeout=2)
        engine.close()


# ---------------------------------------------------------------------------
# H-27
# ---------------------------------------------------------------------------


def test_begin_requires_writer_lock(db_path: str) -> None:
    store = LonghaulStore(db_path)
    try:
        with pytest.raises(AssertionError, match="writer lock"):
            store._begin()
        with store._lock:
            store._begin()
            store._rollback()
    finally:
        store.close()


def test_foreign_commit_cannot_land_rolled_back_work(db_path: str) -> None:
    """A second thread's commit must not persist another thread's rolled-back INSERT.

    Pre-fix shape: one shared connection + unlocked ``Connection.commit()``
    (see ``LonghaulEngine._cancel_task``). Thread A BEGIN+INSERT; thread B
    commit(); thread A ROLLBACK — too late, the row is durable. Per-thread
    connections isolate the transactions so B's commit cannot land A's work.

    The roller deliberately releases the writer lock while its transaction is
    still open — that is the interleaving window the old shared handle had —
    then rolls back after the interloper's commit returns.
    """
    store = LonghaulStore(db_path)
    try:
        started = threading.Event()
        release = threading.Event()
        roller_errors: list[BaseException] = []
        interloper_errors: list[BaseException] = []
        now = utc_now_iso()
        marker_id = "cat_should_rollback"

        def roller() -> None:
            try:
                with store._lock:
                    store._begin()
                    store._connection.execute(
                        "INSERT INTO task_categories "
                        "(id, name, slug, color, wip_limit, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            marker_id,
                            "Should Rollback",
                            "should-rollback",
                            "",
                            1,
                            now,
                            now,
                        ),
                    )
                    started.set()
                # Lock released; THIS thread's connection still has an open tx.
                assert release.wait(timeout=5), "interloper never signaled"
                time.sleep(0.05)
                with store._lock:
                    store._rollback()
            except BaseException as exc:  # noqa: BLE001
                roller_errors.append(exc)
                started.set()
                release.set()

        def interloper() -> None:
            try:
                assert started.wait(timeout=5), "roller never opened the transaction"
                # Raw commit — the exact hazard H-27 names. Must not land the
                # other thread's uncommitted INSERT.
                store._connection.commit()
            except BaseException as exc:  # noqa: BLE001
                interloper_errors.append(exc)
            finally:
                release.set()

        t_roll = threading.Thread(target=roller, name="tx-roller")
        t_other = threading.Thread(target=interloper, name="tx-interloper")
        t_roll.start()
        t_other.start()
        t_roll.join(timeout=10)
        t_other.join(timeout=10)
        assert not t_roll.is_alive() and not t_other.is_alive()
        assert roller_errors == [], roller_errors
        assert interloper_errors == [], interloper_errors

        row = store._connection.execute(
            "SELECT id FROM task_categories WHERE id = ?", (marker_id,)
        ).fetchone()
        assert row is None, "rolled-back insert was committed by another thread"
    finally:
        store.close()


def test_cancel_task_uses_locked_transaction_helpers(db_path: str) -> None:
    """``_cancel_task`` must not call raw ``Connection.commit()`` unlocked (H-27).

    Isolation under concurrent rollback is covered by
    ``test_foreign_commit_cannot_land_rolled_back_work``; this test proves the
    production cancel path still performs its own work through the locked
    begin/commit helpers.
    """
    store = LonghaulStore(db_path)
    try:
        engine = LonghaulEngine(store, _cfg(FakeSupervisor()), db_path)
        _task(store, "btk_cancel_me", status="in_progress")
        now = utc_now_iso()
        store._connection.execute(
            "INSERT INTO sessions "
            "(id, source, project_dir, provider, state, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "ses_cancel",
                "bridge",
                "/tmp",
                "claude",
                "running",
                "[longhaul:tks_x]",
                now,
                now,
            ),
        )
        store._connection.execute(
            "INSERT INTO task_sessions "
            "(id, board_task_id, seq, harness, model, session_id, started_at, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "tks_x",
                "btk_cancel_me",
                0,
                "cli-codex",
                "gpt-5",
                "ses_cancel",
                now,
                "",
            ),
        )
        store._connection.commit()

        engine._cancel_task(
            {
                "id": "btk_cancel_me",
                "lane": "longhaul",
                "status": "in_progress",
                "archived_at": now,
            }
        )

        session = store._connection.execute(
            "SELECT kill_requested, killed_by FROM sessions WHERE id = ?",
            ("ses_cancel",),
        ).fetchone()
        assert session is not None
        assert int(session["kill_requested"] or 0) == 1
        assert session["killed_by"] == "cancel_requested"
        attempt = store._connection.execute(
            "SELECT ended_at, end_reason FROM task_sessions WHERE id = ?",
            ("tks_x",),
        ).fetchone()
        assert attempt is not None and attempt["ended_at"] is not None
        assert attempt["end_reason"] == "superseded"
        board = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = ?",
            ("btk_cancel_me",),
        ).fetchone()
        assert board is not None and board["status"] == "cancelled"
    finally:
        store.close()


def test_file_store_threads_get_distinct_connections(db_path: str) -> None:
    """File-backed LonghaulStore isolates connections per thread (H-27 substrate)."""
    store = LonghaulStore(db_path)
    try:
        here = store._connection
        other: list[object] = []
        thread = threading.Thread(target=lambda: other.append(store._connection))
        thread.start()
        thread.join(timeout=5)
        assert other and other[0] is not here
    finally:
        store.close()
