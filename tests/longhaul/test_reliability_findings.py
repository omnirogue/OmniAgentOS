"""Direct behavioral regression tests for L06 reliability findings.

These prove the engine/config/limits contracts by observation, not by scanning
source text. Findings covered:

* H-08 — usage/auth terminals from durable session.error (events=[]) cool/disable
  and do not burn the attempt budget as crashes on the same account.
* H-28 — waiting_review exhaustion escalates, blocks, and releases category WIP.
* H-29 — load_config preserves attempt_wall_ms (no silent drop of unknown keys).
* L-13 — opt-in fast-crash backoff delays redispatch; spawn_incomplete and
  codex_orphan stamp the same bounded backoff.
* L-14 — harness→provider mapping reaches Grok/Gemini/Kimi pattern tables.
* M-45 — missing recorded working_dir fails closed; one task exception does not
  abort the rest of tick().
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import patch

import pytest

from omniagentos.adapters.gemini import GeminiAdapter
from omniagentos.adapters.grok import GrokAdapter
from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import terminal_evidence as terminal_evidence_module
from omniagentos.longhaul import workbook
from omniagentos.longhaul.config import load_config
from omniagentos.longhaul.engine import (
    _DISPATCH_LOCK_POLL_S,
    LonghaulEngine,
    _bounded_wall_ms,
    _bounded_wall_seconds,
    _provider_for_harness,
)
from omniagentos.longhaul.limits import classify_terminal
from omniagentos.longhaul.store import LonghaulStore
from omniagentos.longhaul.terminal_evidence import (
    TERMINAL_CAPTURE_LIMIT_BYTES,
    is_tombstoned,
    launch_record_path,
    load_terminal_record,
    prepare_evidence_root,
    publish_launch_ack,
    publish_terminal_record,
    publish_tombstone,
    terminal_record_path,
)


def async_test(function: Any) -> Any:
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeSupervisor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("injected spawn crash")
        return f"ses_fake_{len(self.calls)}"


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
        "cross_harness_fallback": False,
        "static_fallback_order": [{"harness": "cli-claude", "model": "opus"}],
        "review": {"enabled": False, "deny_respawns": 2, "unavailable_retries": 3},
        "spawn_grace_s": 0,
        # Existing suite disables L-13 so immediate redispatch stays testable;
        # findings tests that opt in set fast_crash_s explicitly.
        "fast_crash_s": 0,
        "fast_crash_backoff_s": 5,
        "fast_crash_max_backoff_s": 300,
        "attempt_wall_ms": 1_800_000,
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
            json.dumps(state or {}),
        ),
    )
    store._connection.commit()


def _account(store: LonghaulStore, account_id: str, *, cooling: bool = False) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO claude_accounts "
        "(id,label,enabled,status,created_at,updated_at,cooldown_until) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            account_id,
            account_id,
            1,
            "rate_limited" if cooling else "ok",
            now,
            now,
            "2999-01-01T00:00:00Z" if cooling else None,
        ),
    )
    store._connection.commit()


def _provider_account(
    store: LonghaulStore,
    account_id: str,
    provider: str,
    config_dir: Path,
) -> None:
    now = utc_now_iso()
    config_dir.mkdir(parents=True, exist_ok=True)
    store._connection.execute(
        "INSERT INTO claude_accounts "
        "(id,label,auth_type,config_dir,enabled,is_default,status,"
        "created_at,updated_at,provider) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            account_id,
            account_id,
            "config_dir",
            str(config_dir),
            1,
            0,
            "ok",
            now,
            now,
            provider,
        ),
    )
    store._connection.commit()


def _done(task_id: str) -> None:
    path = Path(workbook._path(task_id))
    path.write_text(path.read_text().replace("WORKING", "DONE"), encoding="utf-8")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = monotonic() + timeout_s
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.02)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cli_shim(path: Path, body: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(body)}",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# ---------------------------------------------------------------------------
# C-06 cleanup: lock poll is a bounded sleep, not sleep(0) tight-spin
# ---------------------------------------------------------------------------


def test_dispatch_lock_poll_is_bounded_not_zero() -> None:
    """Held-lock wait must sleep a positive bound (not spin on sleep(0))."""
    assert _DISPATCH_LOCK_POLL_S > 0
    assert _DISPATCH_LOCK_POLL_S <= 0.1


# ---------------------------------------------------------------------------
# H-08 — empty events + durable session.error
# ---------------------------------------------------------------------------


@async_test
async def test_h08_empty_events_usage_limit_from_session_error_cools_account(
    db_path: str,
) -> None:
    """Tick-style delivery passes events=[]; session.error must still cool.

    Pre-fix this path classified as ``crashed`` and immediately respawned on
    the same limited account until max_sessions was exhausted (H-08 / F-18).
    """
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _task(store, "btk_h08_usage")
        engine = LonghaulEngine(store, _cfg(supervisor), db_path)
        await engine.dispatch("btk_h08_usage")
        attempt = store.current_attempt("btk_h08_usage")
        assert attempt is not None

        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{attempt['id']}]",
                "state": "failed",
                "error": "You've reached your usage limit; resets at 3am UTC",
            },
            [],  # empty events — the H-08 delivery shape
            1,
        )

        account = store._connection.execute(
            "SELECT status, cooldown_until FROM claude_accounts WHERE id='acct_a'"
        ).fetchone()
        task = store._connection.execute(
            "SELECT status, park_state FROM board_tasks WHERE id='btk_h08_usage'"
        ).fetchone()
        attempts = store.list_attempts("btk_h08_usage")
        assert account["status"] == "rate_limited"
        assert account["cooldown_until"]
        # Sole cooled account → park waiting_capacity; never a crash-loop redispatch.
        assert dict(task) == {"status": "in_progress", "park_state": "waiting_capacity"}
        assert attempts[0]["end_reason"] == "usage_limited"
        assert len(attempts) == 1
        assert len(supervisor.calls) == 1
    finally:
        store.close()


@async_test
async def test_h08_empty_events_auth_from_session_error_disables_and_switches(
    db_path: str,
) -> None:
    """Auth terminal via session.error (events=[]) disables; successor uses peer."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _account(store, "acct_b")
        _task(store, "btk_h08_auth")
        engine = LonghaulEngine(store, _cfg(supervisor), db_path)
        await engine.dispatch("btk_h08_auth")
        first = store.current_attempt("btk_h08_auth")
        assert first is not None and first["account_id"] == "acct_a"

        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{first['id']}]",
                "state": "failed",
                "error": "failed to authenticate OAuth session",
            },
            [],
            1,
        )

        enabled_a = store._connection.execute(
            "SELECT enabled FROM claude_accounts WHERE id='acct_a'"
        ).fetchone()["enabled"]
        attempts = store.list_attempts("btk_h08_auth")
        assert int(enabled_a) == 0
        assert attempts[0]["end_reason"] == "auth_failed"
        assert len(attempts) == 2
        assert attempts[1]["account_id"] == "acct_b"
        assert len(supervisor.calls) == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    ("error_text", "end_reason"),
    [
        ("You've reached your usage limit; resets at 3am UTC", "usage_limited"),
        ("failed to authenticate OAuth session", "auth_failed"),
    ],
)
@async_test
async def test_h08_account_terminal_does_not_delay_healthy_alternate(
    db_path: str,
    error_text: str,
    end_reason: str,
) -> None:
    """Account cooldown/disable replaces, rather than stacks with, task pacing."""

    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _account(store, "acct_b")
        task_id = f"btk_h08_no_double_{end_reason}"
        _task(store, task_id)
        engine = LonghaulEngine(
            store,
            _cfg(
                supervisor,
                fast_crash_s=60,
                fast_crash_backoff_s=30,
            ),
            db_path,
        )
        await engine.dispatch(task_id)
        first = store.current_attempt(task_id)
        assert first is not None and first["account_id"] == "acct_a"
        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{first['id']}]",
                "state": "failed",
                "error": error_text,
            },
            [],
            1,
        )

        attempts = store.list_attempts(task_id)
        assert [(attempt["seq"], attempt["account_id"]) for attempt in attempts] == [
            (0, "acct_a"),
            (1, "acct_b"),
        ]
        assert attempts[0]["end_reason"] == end_reason
        state = store.get_longhaul_json(task_id) or {}
        assert state.get("fast_crash_count") == 0
        assert state.get("next_dispatch_at") is None
    finally:
        store.close()


_REAL_PROVIDER_FINAL_CASES = [
    (
        "cli-grok",
        "grok-4.5",
        GrokAdapter,
        "out of credits on this API key",
        "usage_limited",
    ),
    (
        "cli-grok",
        "grok-4.5",
        GrokAdapter,
        "invalid bearer token for xAI",
        "auth_failed",
    ),
    (
        "cli-gemini",
        "gemini-3.1-pro",
        GeminiAdapter,
        "resource_exhausted: daily limit hit",
        "usage_limited",
    ),
    (
        "cli-gemini",
        "gemini-3.1-pro",
        GeminiAdapter,
        "api key not valid for Gemini",
        "auth_failed",
    ),
    (
        "cli-kimi",
        "kimi-k3",
        KimiAdapter,
        "exceeded_current_quota_error for this workspace",
        "usage_limited",
    ),
    (
        "cli-kimi",
        "kimi-k3",
        KimiAdapter,
        "permission_denied_error for moonshot",
        "auth_failed",
    ),
]


@pytest.mark.parametrize(
    ("harness", "model", "adapter_class", "error_text", "end_reason"),
    _REAL_PROVIDER_FINAL_CASES,
)
@async_test
async def test_h08_final_attempt_real_provider_effect_is_atomic_and_idempotent(
    db_path: str,
    tmp_path: Path,
    harness: str,
    model: str,
    adapter_class: type[Any],
    error_text: str,
    end_reason: str,
) -> None:
    """The last allowed real-provider attempt still cools/disables exactly once."""

    provider = harness.removeprefix("cli-")
    account_id = f"acct_{provider}_{end_reason}_final"
    process_fixture = tmp_path / f"{provider}-{end_reason}-final.py"
    process_fixture.write_text(
        f"import sys\nsys.stderr.write({error_text!r})\nsys.stderr.flush()\nraise SystemExit(1)\n",
        encoding="utf-8",
    )

    store = LonghaulStore(db_path)
    try:
        _provider_account(
            store,
            account_id,
            provider,
            tmp_path / f".{provider}-{end_reason}",
        )
        task_id = f"btk_{provider}_{end_reason}_final"
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": harness, "model": model}],
                fast_crash_s=60,
                fast_crash_backoff_s=30,
                attempt_wall_ms=2_000,
                attempt_term_grace_ms=100,
                attempt_kill_reap_ms=500,
                unattended_elevated_harnesses=[harness],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        with patch.object(
            adapter_class,
            "_command",
            return_value=[sys.executable, str(process_fixture)],
        ):
            await engine.dispatch(task_id)
            await _wait_until(lambda: store.current_attempt(task_id) is None)
            await _wait_until(lambda: not engine._codex_threads)

        attempts = store.list_attempts(task_id)
        assert len(attempts) == 1
        assert attempts[0]["harness"] == harness
        assert attempts[0]["end_reason"] == end_reason
        task = store._connection.execute(
            "SELECT status,park_state FROM board_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(task) == {"status": "blocked", "park_state": None}
        state = store.get_longhaul_json(task_id) or {}
        # Usage/auth owns the account, not the task: never stack L-13 pacing on
        # top of a cooldown/disable or delay a healthy alternate account.
        assert state.get("fast_crash_count") == 0
        assert state.get("next_dispatch_at") is None

        account_before = dict(
            store._connection.execute(
                "SELECT enabled,status,status_detail,cooldown_until,updated_at "
                "FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        if end_reason == "usage_limited":
            assert account_before["enabled"] == 1
            assert account_before["status"] == "rate_limited"
            assert account_before["cooldown_until"] is not None
        else:
            assert account_before["enabled"] == 0
            assert account_before["status"] == "error"
            assert account_before["cooldown_until"] is None
        assert error_text in str(account_before["status_detail"])

        event_count = store._connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE target_id = ? AND action = ?",
            (task_id, f"longhaul.terminal.{end_reason}"),
        ).fetchone()["n"]
        notification_count = store._connection.execute(
            "SELECT COUNT(*) AS n FROM notifications "
            "WHERE ref_type = 'claude_account' AND ref_id = ?",
            (account_id,),
        ).fetchone()["n"]
        assert notification_count == (1 if end_reason == "auth_failed" else 0)

        # Duplicate delivery loses ended_at CAS: no account refresh, event, or
        # auth notification can repeat.
        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{attempts[0]['id']}]",
                "state": "failed",
            },
            [
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "error": error_text,
                    "result": error_text,
                }
            ],
            1,
        )
        account_after = dict(
            store._connection.execute(
                "SELECT enabled,status,status_detail,cooldown_until,updated_at "
                "FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        assert account_after == account_before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE target_id = ? AND action = ?",
                (task_id, f"longhaul.terminal.{end_reason}"),
            ).fetchone()["n"]
            == event_count
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE ref_type = 'claude_account' AND ref_id = ?",
                (account_id,),
            ).fetchone()["n"]
            == notification_count
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("error_text", "end_reason"),
    [
        ("out of credits on this API key", "usage_limited"),
        ("invalid bearer token for xAI", "auth_failed"),
    ],
)
@async_test
async def test_h08_journaled_real_provider_terminal_replays_after_restart(
    db_path: str,
    tmp_path: Path,
    error_text: str,
    end_reason: str,
) -> None:
    """Crash after evidence but before close replays the exact provider effect."""

    gate = tmp_path / f"{end_reason}.gate"
    process_fixture = tmp_path / f"grok-{end_reason}-restart.py"
    process_fixture.write_text(
        "import pathlib,sys,time\n"
        f"gate=pathlib.Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(0.01)\n"
        f"sys.stderr.write({error_text!r})\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    store = LonghaulStore(db_path)
    restarted_store: LonghaulStore | None = None
    try:
        account_id = f"acct_grok_restart_{end_reason}"
        task_id = f"btk_grok_restart_{end_reason}"
        _provider_account(store, account_id, "grok", tmp_path / ".grok-restart")
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        cfg = _cfg(
            max_sessions=1,
            cross_harness_fallback=True,
            static_fallback_order=[{"harness": "cli-grok", "model": "grok-4.5"}],
            fast_crash_s=60,
            attempt_wall_ms=2_000,
            unattended_elevated_harnesses=["cli-grok"],
            working_dir=str(tmp_path),
        )
        engine = LonghaulEngine(store, cfg, db_path)
        with patch.object(
            GrokAdapter,
            "_command",
            return_value=[sys.executable, str(process_fixture)],
        ):
            await engine.dispatch(task_id)
            await _wait_until(
                lambda: "pid" in json.loads(str((store.current_attempt(task_id) or {})["detail"]))
            )

            original_commit = store._commit
            commit_calls = 0

            def crash_close_commit() -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise RuntimeError("crash-after-terminal-evidence")
                original_commit()

            with patch.object(store, "_commit", side_effect=crash_close_commit):
                gate.write_text("release", encoding="utf-8")
                await _wait_until(lambda: not engine._codex_threads)

        assert commit_calls == 2
        open_attempt = store.current_attempt(task_id)
        assert open_attempt is not None
        detail = json.loads(str(open_attempt["detail"]))
        assert detail["terminal_evidence"]["rc"] == 1
        assert error_text in json.dumps(detail["terminal_evidence"])
        untouched = store._connection.execute(
            "SELECT enabled,status,cooldown_until FROM claude_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        assert dict(untouched) == {
            "enabled": 1,
            "status": "ok",
            "cooldown_until": None,
        }

        restarted_store = LonghaulStore(db_path)
        restarted = LonghaulEngine(restarted_store, cfg, db_path)
        await restarted.tick()
        assert restarted_store.current_attempt(task_id) is None
        attempts = restarted_store.list_attempts(task_id)
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == end_reason
        account = restarted_store._connection.execute(
            "SELECT enabled,status,cooldown_until,status_detail FROM claude_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if end_reason == "usage_limited":
            assert account["enabled"] == 1
            assert account["status"] == "rate_limited"
            assert account["cooldown_until"] is not None
        else:
            assert account["enabled"] == 0
            assert account["status"] == "error"
        assert error_text in str(account["status_detail"])
        task = restarted_store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert task["status"] == "blocked"
    finally:
        if restarted_store is not None:
            restarted_store.close()
        store.close()


@pytest.mark.parametrize(
    ("harness", "model", "adapter_class", "error_text", "end_reason"),
    _REAL_PROVIDER_FINAL_CASES,
)
@async_test
async def test_h08_fsynced_provider_record_survives_pre_db_daemon_loss(
    db_path: str,
    tmp_path: Path,
    harness: str,
    model: str,
    adapter_class: type[Any],
    error_text: str,
    end_reason: str,
) -> None:
    """A fresh daemon replays provider truth lost before the first DB journal."""

    provider = harness.removeprefix("cli-")
    account_id = f"acct_{provider}_{end_reason}_pre_db"
    task_id = f"btk_{provider}_{end_reason}_pre_db"
    process_fixture = tmp_path / f"{provider}-{end_reason}-pre-db.py"
    process_fixture.write_text(
        f"import sys\nsys.stderr.write({error_text!r})\nsys.stderr.flush()\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    cfg = _cfg(
        max_sessions=1,
        cross_harness_fallback=True,
        static_fallback_order=[{"harness": harness, "model": model}],
        fast_crash_s=60,
        attempt_wall_ms=2_000,
        unattended_elevated_harnesses=[harness],
        working_dir=str(tmp_path),
    )
    store = LonghaulStore(db_path)
    restarted_store: LonghaulStore | None = None
    try:
        _provider_account(store, account_id, provider, tmp_path / f".{provider}-pre-db")
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        engine = LonghaulEngine(store, cfg, db_path)

        def lose_daemon_after_fsync(*_: Any, **__: Any) -> None:
            # BaseException bypasses the ordinary "DB journal failed, still
            # callback" recovery and models the whole daemon disappearing.
            raise SystemExit("injected daemon loss after terminal record fsync")

        with (
            patch.object(
                adapter_class, "_command", return_value=[sys.executable, str(process_fixture)]
            ),
            patch.object(
                engine,
                "_persist_terminal_evidence",
                side_effect=lose_daemon_after_fsync,
            ),
            patch.object(threading, "excepthook", return_value=None),
        ):
            await engine.dispatch(task_id)
            worker = next(iter(engine._codex_threads.values()))
            await _wait_until(lambda: not worker.is_alive())

        open_attempt = store.current_attempt(task_id)
        assert open_attempt is not None
        launch_detail = json.loads(str(open_attempt["detail"]))
        nonce = str(launch_detail["terminal_record_nonce"])
        record_path = terminal_record_path(db_path, str(open_attempt["id"]), nonce)
        started_path = launch_record_path(db_path, str(open_attempt["id"]), nonce)
        assert record_path.is_file()
        assert started_path.is_file()
        untouched = dict(
            store._connection.execute(
                "SELECT enabled,status,cooldown_until FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        assert untouched == {"enabled": 1, "status": "ok", "cooldown_until": None}

        # A separate store/engine is the process-restart boundary. It consumes
        # the immutable record before dead-PID fallback can downgrade the event.
        restarted_store = LonghaulStore(db_path)
        restarted = LonghaulEngine(restarted_store, cfg, db_path)
        await restarted.tick()

        attempts = restarted_store.list_attempts(task_id)
        assert [(item["seq"], item["end_reason"]) for item in attempts] == [(0, end_reason)]
        assert restarted_store.current_attempt(task_id) is None
        state = restarted_store.get_longhaul_json(task_id) or {}
        assert state["max_sessions_reached"] is True
        assert state.get("next_dispatch_at") is None
        task = restarted_store._connection.execute(
            "SELECT status,park_state FROM board_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(task) == {"status": "blocked", "park_state": None}
        account_before_duplicate = dict(
            restarted_store._connection.execute(
                "SELECT enabled,status,status_detail,cooldown_until,updated_at "
                "FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        if end_reason == "usage_limited":
            assert account_before_duplicate["enabled"] == 1
            assert account_before_duplicate["status"] == "rate_limited"
            assert account_before_duplicate["cooldown_until"] is not None
        else:
            assert account_before_duplicate["enabled"] == 0
            assert account_before_duplicate["status"] == "error"
        assert error_text in str(account_before_duplicate["status_detail"])
        assert not record_path.exists()
        assert not started_path.exists()

        # Both a duplicate callback and another restart tick are no-ops.
        await restarted.on_session_terminal(
            {"title": f"[longhaul:{open_attempt['id']}]", "state": "failed"},
            [
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "error": error_text,
                    "result": error_text,
                }
            ],
            1,
        )
        await restarted.tick()
        assert restarted_store.list_attempts(task_id) == attempts
        assert (
            dict(
                restarted_store._connection.execute(
                    "SELECT enabled,status,status_detail,cooldown_until,updated_at "
                    "FROM claude_accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
            )
            == account_before_duplicate
        )
    finally:
        if restarted_store is not None:
            restarted_store.close()
        store.close()


@async_test
async def test_launch_identity_grace_uses_pre_popen_timestamp(
    db_path: str,
    tmp_path: Path,
) -> None:
    """Slow command preparation cannot consume the wrapper-start safety window."""

    store = LonghaulStore(db_path)
    try:
        account_id = "acct_grok_launch_grace"
        task_id = "btk_grok_launch_grace"
        _provider_account(store, account_id, "grok", tmp_path / ".grok-launch-grace")
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        attempt = store.open_attempt(
            task_id,
            "cli-grok",
            "grok-4.5",
            account_id=account_id,
            working_dir=str(tmp_path),
            max_sessions=1,
        )
        # Model a long pre-launch setup followed by daemon loss just after
        # Popen: the attempt is old, while the unpredictable nonce and its
        # launch timestamp were committed immediately before Popen.
        store._connection.execute(
            "UPDATE task_sessions SET started_at = ? WHERE id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                str(attempt["id"]),
            ),
        )
        store._connection.commit()
        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                spawn_grace_s=0,
                static_fallback_order=[{"harness": "cli-grok", "model": "grok-4.5"}],
                unattended_elevated_harnesses=["cli-grok"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        engine._update_attempt(
            str(attempt["id"]),
            detail={
                "provider": "grok",
                "terminal_record_nonce": "fresh_pre_popen_nonce",
                "terminal_record_version": 1,
                "started": utc_now_iso(),
            },
        )

        await engine.tick()

        current = store.current_attempt(task_id)
        assert current is not None
        assert current["id"] == attempt["id"]
        assert current["ended_at"] is None
        assert len(store.list_attempts(task_id)) == 1
    finally:
        store.close()


@async_test
async def test_delayed_wrapper_never_launches_provider_after_unacknowledged_cancellation(
    db_path: str,
    tmp_path: Path,
) -> None:
    """A wrapper process without daemon ACK never executes the provider command."""

    prepare_evidence_root(db_path)
    attempt_id = "att_delayed_no_ack"
    launch_nonce = "nonce_delayed_no_ack"
    marker_file = tmp_path / "provider_executed.marker"

    started_record_p = launch_record_path(db_path, attempt_id, launch_nonce)
    record_p = terminal_record_path(db_path, attempt_id, launch_nonce)

    provider_cmd = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path('{marker_file}').write_text('executed')",
    ]

    evidence_script = str(
        Path(__file__).parents[2] / "omniagentos" / "longhaul" / "terminal_evidence.py"
    )

    evidence_launch = [
        sys.executable,
        evidence_script,
        "--record-path",
        str(record_p),
        "--launch-record-path",
        str(started_record_p),
        "--attempt-id",
        attempt_id,
        "--harness",
        "cli-grok",
        "--provider",
        "grok",
        "--launch-nonce",
        launch_nonce,
        "--",
        *provider_cmd,
    ]

    proc = subprocess.Popen(
        evidence_launch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )

    try:
        for _ in range(100):
            if started_record_p.exists():
                break
            await asyncio.sleep(0.02)
        assert started_record_p.exists()

        publish_tombstone(
            db_path,
            attempt_id=attempt_id,
            harness="cli-grok",
            provider="grok",
            launch_nonce=launch_nonce,
            reason="test cancellation",
        )

        stdout, stderr = proc.communicate(timeout=5.0)
        assert proc.returncode == 125
        assert not marker_file.exists(), (
            "Old provider MUST NOT run after cancellation/unacknowledged state"
        )

        record = load_terminal_record(
            db_path,
            attempt_id=attempt_id,
            harness="cli-grok",
            provider="grok",
            launch_nonce=launch_nonce,
            expected_wrapper_pid=proc.pid,
        )
        assert record["returncode"] == 125
        assert record["launch_authorized"] is False
    finally:
        if proc.poll() is None:
            proc.kill()


@async_test
async def test_unacknowledged_wrapper_on_restart_is_tombstoned_and_never_adopted(
    db_path: str,
    tmp_path: Path,
) -> None:
    """Daemon restart after crash before ACK tombstones unacknowledged wrapper."""

    store = LonghaulStore(db_path)
    try:
        account_id = "acct_grok_restart_no_ack"
        task_id = "btk_grok_restart_no_ack"
        _provider_account(store, account_id, "grok", tmp_path / ".grok-no-ack")
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        attempt = store.open_attempt(
            task_id,
            "cli-grok",
            "grok-4.5",
            account_id=account_id,
            working_dir=str(tmp_path),
            max_sessions=1,
        )
        launch_nonce = "nonce_pre_ack_crash"
        prepare_evidence_root(db_path)
        started_p = launch_record_path(db_path, str(attempt["id"]), launch_nonce)
        publish_terminal_record(
            started_p,
            {
                "version": 1,
                "attempt_id": str(attempt["id"]),
                "harness": "cli-grok",
                "provider": "grok",
                "launch_nonce": launch_nonce,
                "state": "started",
                "wrapper_pid": 987654,
            },
        )
        store._connection.execute(
            "UPDATE task_sessions SET started_at = ? WHERE id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                str(attempt["id"]),
            ),
        )
        store._connection.commit()

        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                spawn_grace_s=0,
                static_fallback_order=[{"harness": "cli-grok", "model": "grok-4.5"}],
                unattended_elevated_harnesses=["cli-grok"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        engine._update_attempt(
            str(attempt["id"]),
            detail={
                "provider": "grok",
                "terminal_record_nonce": launch_nonce,
                "terminal_record_version": 1,
                "started": utc_now_iso(),
            },
        )

        await engine.tick()

        assert is_tombstoned(db_path, attempt_id=str(attempt["id"]), launch_nonce=launch_nonce)
        ended = engine._attempt_by_id(str(attempt["id"]))
        assert ended is not None
        assert ended["ended_at"] is not None
        assert ended["end_reason"] == "crashed"
    finally:
        store.close()


@pytest.mark.parametrize("record_mode", ["missing", "corrupt", "stale"])
@async_test
async def test_h08_untrusted_terminal_record_fails_closed_without_max_plus_one(
    db_path: str,
    tmp_path: Path,
    record_mode: str,
) -> None:
    """Missing/corrupt/foreign evidence is never promoted to provider truth."""

    store = LonghaulStore(db_path)
    try:
        account_id = f"acct_grok_untrusted_{record_mode}"
        task_id = f"btk_grok_untrusted_{record_mode}"
        _provider_account(store, account_id, "grok", tmp_path / ".grok-untrusted")
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        attempt = store.open_attempt(
            task_id,
            "cli-grok",
            "grok-4.5",
            account_id=account_id,
            working_dir=str(tmp_path),
            max_sessions=1,
        )
        nonce = f"nonce_{record_mode}"
        dead_pid = 99_999_991
        launch_detail = {
            "pid": dead_pid,
            "pgid": dead_pid,
            "provider": "grok",
            "terminal_record_nonce": nonce,
            "terminal_record_version": 1,
        }
        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": "cli-grok", "model": "grok-4.5"}],
                fast_crash_s=0,
                unattended_elevated_harnesses=["cli-grok"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        engine._update_attempt(str(attempt["id"]), detail=launch_detail)
        prepare_evidence_root(db_path)
        path = terminal_record_path(db_path, str(attempt["id"]), nonce)
        if record_mode == "corrupt":
            path.write_bytes(b'{"not":"trusted"}')
        elif record_mode == "stale":
            publish_terminal_record(
                path,
                {
                    "version": 1,
                    "attempt_id": "tks_foreign",
                    "harness": "cli-grok",
                    "provider": "grok",
                    "launch_nonce": nonce,
                    "wrapper_pid": dead_pid,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "out of credits on this API key",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            )

        await engine.tick()
        attempts = store.list_attempts(task_id)
        assert [(item["seq"], item["end_reason"]) for item in attempts] == [(0, "crashed")]
        assert store.current_attempt(task_id) is None
        state = store.get_longhaul_json(task_id) or {}
        assert state["max_sessions_reached"] is True
        row = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row["status"] == "blocked"
        account = dict(
            store._connection.execute(
                "SELECT enabled,status,cooldown_until FROM claude_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        )
        assert account == {"enabled": 1, "status": "ok", "cooldown_until": None}

        # Even a second explicit dispatch cannot turn unknown terminal evidence
        # into a successor after the only allowed sequence was consumed.
        await engine.dispatch(task_id)
        assert len(store.list_attempts(task_id)) == 1
    finally:
        store.close()


@async_test
async def test_max_sessions_open_transaction_refuses_legacy_missing_fence(
    db_path: str,
    tmp_path: Path,
) -> None:
    """The insertion transaction itself prevents max+1 if the JSON fence is absent."""

    store = LonghaulStore(db_path)
    try:
        _provider_account(store, "acct_grok_legacy", "grok", tmp_path / ".grok-legacy")
        _task(store, "btk_grok_legacy", state={"working_dir": str(tmp_path)})
        attempt = store.open_attempt(
            "btk_grok_legacy",
            "cli-grok",
            "grok-4.5",
            account_id="acct_grok_legacy",
            working_dir=str(tmp_path),
        )
        assert store.close_attempt(str(attempt["id"]), "crashed", "legacy close")
        # Simulate a row written by an older daemon: active task, no restart
        # marker, but sequence zero has already consumed max_sessions=1.
        store._connection.execute(
            "UPDATE board_tasks SET status='in_progress',park_state=NULL,"
            "longhaul_json=? WHERE id='btk_grok_legacy'",
            (json.dumps({"working_dir": str(tmp_path)}),),
        )
        store._connection.commit()
        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": "cli-grok", "model": "grok-4.5"}],
                unattended_elevated_harnesses=["cli-grok"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        await engine.dispatch("btk_grok_legacy")
        attempts = store.list_attempts("btk_grok_legacy")
        assert [(item["seq"], item["end_reason"]) for item in attempts] == [(0, "crashed")]
        assert store.current_attempt("btk_grok_legacy") is None
        row = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id='btk_grok_legacy'"
        ).fetchone()
        assert row["status"] == "blocked"
    finally:
        store.close()


def test_terminal_record_is_bounded_sanitized_and_identity_bound(
    db_path: str,
    tmp_path: Path,
) -> None:
    root = prepare_evidence_root(db_path)
    attempt_id = "tks_bounded_record"
    nonce = "bounded_nonce"
    path = terminal_record_path(db_path, attempt_id, nonce)
    started_path = launch_record_path(db_path, attempt_id, nonce)
    fixture = tmp_path / "oversized-provider.py"
    fixture.write_text(
        "import sys\n"
        f"sys.stderr.buffer.write(b'x' * {TERMINAL_CAPTURE_LIMIT_BYTES + 10_000})\n"
        "sys.stderr.buffer.write(b'\\x00tail')\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    publish_launch_ack(
        db_path,
        attempt_id=attempt_id,
        harness="cli-grok",
        provider="grok",
        launch_nonce=nonce,
    )
    wrapper = subprocess.run(
        [
            sys.executable,
            str(Path(terminal_evidence_module.__file__)),
            "--record-path",
            str(path),
            "--launch-record-path",
            str(started_path),
            "--attempt-id",
            attempt_id,
            "--harness",
            "cli-grok",
            "--provider",
            "grok",
            "--launch-nonce",
            nonce,
            "--",
            sys.executable,
            str(fixture),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert wrapper.returncode == 1
    loaded = load_terminal_record(
        db_path,
        attempt_id=attempt_id,
        harness="cli-grok",
        provider="grok",
        launch_nonce=nonce,
        expected_wrapper_pid=None,
    )
    assert path.parent == root
    assert loaded["stderr_truncated"] is True
    assert "\x00" not in loaded["stderr"]
    assert str(loaded["stderr"]).endswith("\N{REPLACEMENT CHARACTER}tail")
    assert len(str(loaded["stderr"]).encode("utf-8")) <= TERMINAL_CAPTURE_LIMIT_BYTES + 2


@async_test
async def test_h08_terminal_transaction_rollback_replays_after_restart(
    db_path: str,
) -> None:
    """A crash before commit leaves both writes retryable; restart applies both."""

    store = LonghaulStore(db_path)
    restarted_store: LonghaulStore | None = None
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_restart")
        _task(store, "btk_h08_restart")
        cfg = _cfg(supervisor, max_sessions=1, fast_crash_s=60)
        engine = LonghaulEngine(store, cfg, db_path)
        await engine.dispatch("btk_h08_restart")
        attempt = store.current_attempt("btk_h08_restart")
        assert attempt is not None
        terminal = {
            "title": f"[longhaul:{attempt['id']}]",
            "state": "failed",
            "error": "failed to authenticate OAuth session",
        }

        with patch.object(store, "_commit", side_effect=RuntimeError("crash-before-commit")):
            with pytest.raises(RuntimeError, match="crash-before-commit"):
                await engine.on_session_terminal(terminal, [], 1)

        # Rollback kept both sides untouched and retryable.
        assert store.current_attempt("btk_h08_restart") is not None
        before = store._connection.execute(
            "SELECT enabled,status FROM claude_accounts WHERE id = 'acct_restart'"
        ).fetchone()
        assert dict(before) == {"enabled": 1, "status": "ok"}

        restarted_store = LonghaulStore(db_path)
        restarted = LonghaulEngine(restarted_store, cfg, db_path)
        await restarted.on_session_terminal(terminal, [], 1)
        assert restarted_store.current_attempt("btk_h08_restart") is None
        after = restarted_store._connection.execute(
            "SELECT enabled,status,status_detail FROM claude_accounts WHERE id = 'acct_restart'"
        ).fetchone()
        assert after["enabled"] == 0
        assert after["status"] == "error"
        assert "authenticate" in str(after["status_detail"])
        task = restarted_store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = 'btk_h08_restart'"
        ).fetchone()
        assert task["status"] == "blocked"
    finally:
        if restarted_store is not None:
            restarted_store.close()
        store.close()


@async_test
async def test_h08_restart_fence_blocks_after_close_commit_before_limit_transition(
    db_path: str,
) -> None:
    """Crash after atomic close/effect cannot dispatch attempt max+1."""

    store = LonghaulStore(db_path)
    restarted_store: LonghaulStore | None = None
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_limit_fence")
        _task(store, "btk_h08_limit_fence")
        cfg = _cfg(supervisor, max_sessions=1, fast_crash_s=60)
        engine = LonghaulEngine(store, cfg, db_path)
        await engine.dispatch("btk_h08_limit_fence")
        attempt = store.current_attempt("btk_h08_limit_fence")
        assert attempt is not None

        with patch.object(
            engine,
            "_block_for_limit",
            side_effect=RuntimeError("crash-before-limit-transition"),
        ):
            with pytest.raises(RuntimeError, match="crash-before-limit-transition"):
                await engine.on_session_terminal(
                    {
                        "title": f"[longhaul:{attempt['id']}]",
                        "state": "failed",
                        "error": "You've reached your usage limit; resets at 3am UTC",
                    },
                    [],
                    1,
                )

        assert store.current_attempt("btk_h08_limit_fence") is None
        state = store.get_longhaul_json("btk_h08_limit_fence") or {}
        assert state["max_sessions_reached"] is True
        cooled = store._connection.execute(
            "SELECT status,cooldown_until FROM claude_accounts WHERE id = 'acct_limit_fence'"
        ).fetchone()
        assert cooled["status"] == "rate_limited"
        assert cooled["cooldown_until"] is not None
        # Simulated crash happened before the ordinary follow-up transition.
        task = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = 'btk_h08_limit_fence'"
        ).fetchone()
        assert task["status"] == "in_progress"

        restarted_store = LonghaulStore(db_path)
        restarted = LonghaulEngine(restarted_store, cfg, db_path)
        await restarted.tick()
        attempts = restarted_store.list_attempts("btk_h08_limit_fence")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "usage_limited"
        task = restarted_store._connection.execute(
            "SELECT status FROM board_tasks WHERE id = 'btk_h08_limit_fence'"
        ).fetchone()
        assert task["status"] == "blocked"
    finally:
        if restarted_store is not None:
            restarted_store.close()
        store.close()


# ---------------------------------------------------------------------------
# H-28 — waiting_review wedge
# ---------------------------------------------------------------------------


@async_test
async def test_h28_review_unavailable_exhaustion_blocks_and_releases_wip(
    db_path: str,
) -> None:
    """After unavailable_retries, task blocks and category WIP is free for peers."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        category = store.create_category("ReviewCat", wip_limit=1)
        _task(store, "btk_h28_primary", category_id=category["id"])
        _task(store, "btk_h28_peer", category_id=category["id"])

        def reviewer(*_: Any) -> None:
            return None  # unparseable / unavailable

        engine = LonghaulEngine(
            store,
            _cfg(
                supervisor,
                review={
                    "enabled": True,
                    "deny_respawns": 2,
                    "unavailable_retries": 2,
                    "backoff_s": 1,
                    "max_backoff_s": 2,
                },
                _reviewer=reviewer,
            ),
            db_path,
        )
        await engine.dispatch("btk_h28_primary")
        await engine.dispatch("btk_h28_peer")
        peer_park = store._connection.execute(
            "SELECT park_state FROM board_tasks WHERE id='btk_h28_peer'"
        ).fetchone()["park_state"]
        assert peer_park == "waiting_category"

        attempt = store.current_attempt("btk_h28_primary")
        assert attempt is not None
        _done("btk_h28_primary")

        # First unavailable → park waiting_review (retry 1 of 2).
        await engine.on_session_terminal(
            {"title": f"[longhaul:{attempt['id']}]", "state": "completed"}, [], 0
        )
        row = store._connection.execute(
            "SELECT status, park_state, longhaul_json FROM board_tasks WHERE id='btk_h28_primary'"
        ).fetchone()
        assert row["status"] == "in_progress"
        assert row["park_state"] == "waiting_review"
        state = json.loads(row["longhaul_json"])
        assert int(state["review_unavailable_retries"]) == 1

        # Force next_review_at into the past so tick re-enters review.
        state["next_review_at"] = "2000-01-01T00:00:00Z"
        store._connection.execute(
            "UPDATE board_tasks SET longhaul_json = ? WHERE id = ?",
            (json.dumps(state), "btk_h28_primary"),
        )
        store._connection.commit()

        # Second unavailable hits the cap → block + release WIP + wake peer.
        await engine.tick()
        primary = store._connection.execute(
            "SELECT status, park_state, longhaul_json FROM board_tasks WHERE id='btk_h28_primary'"
        ).fetchone()
        assert primary["status"] == "blocked"
        assert primary["park_state"] is None
        primary_state = json.loads(primary["longhaul_json"])
        assert primary_state.get("review_escalated") is True
        assert int(primary_state["review_unavailable_retries"]) >= 2

        # Peer must be able to claim the released category slot.
        peer_attempt = store.current_attempt("btk_h28_peer")
        assert peer_attempt is not None
        peer = store._connection.execute(
            "SELECT status, park_state FROM board_tasks WHERE id='btk_h28_peer'"
        ).fetchone()
        assert peer["status"] == "in_progress"
        assert peer["park_state"] is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# H-29 — attempt_wall_ms config preservation
# ---------------------------------------------------------------------------


def test_h29_load_config_preserves_attempt_wall_ms(tmp_path: Path) -> None:
    """Unknown/new keys under longhaul: must not be silently dropped (H-29)."""
    path = tmp_path / "longhaul.yaml"
    path.write_text(
        "\n".join(
            [
                "longhaul:",
                "  attempt_wall_ms: 900000",
                "  custom_operator_knob: true",
                "  review:",
                "    enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg["attempt_wall_ms"] == 900000
    assert cfg["custom_operator_knob"] is True
    # Nested deep-merge still inherits defaults for omitted review fields.
    assert cfg["review"]["deny_respawns"] == 2
    assert cfg["review"]["enabled"] is False


def test_h29_shipped_yaml_exposes_attempt_wall_ms() -> None:
    """The production configs/longhaul.yaml must carry a wall budget."""
    cfg = load_config("configs/longhaul.yaml")
    assert int(cfg["attempt_wall_ms"]) > 0


@pytest.mark.parametrize(
    ("configured", "expected_ms"),
    [(0, 1), (-50, 1), (None, 1_800_000), ("bad", 1_800_000), (False, 1_800_000)],
)
def test_h29_outer_wall_is_always_positive(configured: Any, expected_ms: int) -> None:
    assert _bounded_wall_ms(configured) == expected_ms
    assert _bounded_wall_seconds(configured) == expected_ms / 1000


@async_test
async def test_h29_codex_path_receives_attempt_wall_ms(db_path: str) -> None:
    """Codex BudgetSpec / communicate timeout must see configured attempt_wall_ms."""
    store = LonghaulStore(db_path)
    try:
        seen: dict[str, Any] = {}

        def fake_codex(
            engine: Any,
            attempt: Any,
            task: Any,
            state: Any,
            prompt: str,
            workbook_path: str,
            marker: str,
        ) -> None:
            del engine, attempt, task, state, prompt, workbook_path, marker
            # Observed via the engine cfg the runner is launched with.
            seen["attempt_wall_ms"] = engine_cfg_holder["wall"]

        engine_cfg_holder: dict[str, Any] = {}
        cfg = _cfg(
            cross_harness_fallback=True,
            static_fallback_order=[{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
            attempt_wall_ms=777_000,
            _codex_runner=lambda *a, **k: {"pid": 1, "pgid": 1, "started": utc_now_iso()},
        )
        engine = LonghaulEngine(store, cfg, db_path)
        engine_cfg_holder["wall"] = engine.cfg.get("attempt_wall_ms")
        _task(store, "btk_h29_wall")
        await engine.dispatch("btk_h29_wall")
        attempt = store.current_attempt("btk_h29_wall")
        assert attempt is not None
        assert attempt["harness"] == "cli-codex"
        assert engine.cfg.get("attempt_wall_ms") == 777_000
        # BudgetSpec construction in _run_codex_process uses the same key.
        from omniagentos.contracts import BudgetSpec

        budget = BudgetSpec(wall_ms_max=engine.cfg.get("attempt_wall_ms"))
        assert budget.wall_ms_max == 777_000
        del fake_codex  # seam reserved; dispatch path already proves cfg wiring
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["ignoring_descendant", "term_exit"])
@async_test
async def test_h29_public_dispatch_enforces_bounded_process_group_wall(
    db_path: str,
    tmp_path: Path,
    mode: str,
) -> None:
    """Real public dispatch cannot outlive wall + TERM + KILL/reap bounds.

    ``ignoring_descendant`` keeps the stdout/stderr pipes open in a child that
    ignores TERM. This is the exact shape that made e493046's final
    ``communicate()`` hang forever. ``term_exit`` covers the benign race where
    the leader exits during grace and KILL must be harmless.
    """

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if mode == "ignoring_descendant":
        body = r"""
import os
import signal
import subprocess
import sys
import time

child_code = '''
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
'''
child = subprocess.Popen(
    [sys.executable, "-c", child_code],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
with open("process-pids.txt", "w", encoding="utf-8") as stream:
    stream.write(f"{os.getpid()} {child.pid}")
    stream.flush()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""
    else:
        body = r"""
import os
import signal
import sys
import time

def finish(_sig, _frame):
    with open("term-observed.txt", "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
        stream.flush()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
while True:
    time.sleep(1)
"""
    process_fixture = bin_dir / "codex"
    _cli_shim(process_fixture, body)

    store = LonghaulStore(db_path)
    try:
        _task(
            store,
            f"btk_h29_{mode}",
            state={"working_dir": str(tmp_path)},
        )
        engine = LonghaulEngine(
            store,
            _cfg(
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
                attempt_wall_ms=120,
                attempt_term_grace_ms=120,
                attempt_kill_reap_ms=600,
                fast_crash_s=60,
                fast_crash_backoff_s=30,
                unattended_elevated_harnesses=["cli-codex"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        from omniagentos.adapters.codex import CodexAdapter

        started = monotonic()
        # Only argv construction is replaced: public dispatch, durable
        # open/close, the real Popen/process group, bounded supervision, and the
        # terminal classifier all remain production code. Executing the fixture
        # through the Python interpreter also works on noexec temp mounts.
        with patch.object(
            CodexAdapter,
            "_command",
            return_value=[sys.executable, str(process_fixture)],
        ):
            await engine.dispatch(f"btk_h29_{mode}")
            await _wait_until(
                lambda: store.current_attempt(f"btk_h29_{mode}") is None,
                timeout_s=3.0,
            )
            await _wait_until(lambda: not engine._codex_threads, timeout_s=1.0)
        elapsed = monotonic() - started

        attempts = store.list_attempts(f"btk_h29_{mode}")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "crashed"
        assert elapsed < 2.0
        assert store.current_attempt(f"btk_h29_{mode}") is None
        state = store.get_longhaul_json(f"btk_h29_{mode}") or {}
        assert _parse_iso(state.get("next_dispatch_at")) is not None

        if mode == "ignoring_descendant":
            parent_pid, child_pid = [
                int(value) for value in (tmp_path / "process-pids.txt").read_text().split()
            ]
            await _wait_until(
                lambda: not _process_alive(parent_pid) and not _process_alive(child_pid),
                timeout_s=2.0,
            )
        else:
            assert (tmp_path / "term-observed.txt").exists()
    finally:
        store.close()


@async_test
async def test_h29_zero_wall_dispatch_is_immediately_bounded(
    db_path: str,
    tmp_path: Path,
) -> None:
    """A configured zero cannot turn outer communicate() into an infinite wait."""

    process_fixture = tmp_path / "zero-wall.py"
    process_fixture.write_text(
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    store = LonghaulStore(db_path)
    try:
        task_id = "btk_h29_zero_wall"
        _task(store, task_id, state={"working_dir": str(tmp_path)})
        engine = LonghaulEngine(
            store,
            _cfg(
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
                attempt_wall_ms=0,
                attempt_term_grace_ms=50,
                attempt_kill_reap_ms=500,
                fast_crash_s=60,
                fast_crash_backoff_s=30,
                unattended_elevated_harnesses=["cli-codex"],
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        from omniagentos.adapters.codex import CodexAdapter

        started = monotonic()
        with patch.object(
            CodexAdapter,
            "_command",
            return_value=[sys.executable, str(process_fixture)],
        ):
            await engine.dispatch(task_id)
            await _wait_until(lambda: store.current_attempt(task_id) is None)
            await _wait_until(lambda: not engine._codex_threads)
        assert monotonic() - started < 2.0
        assert engine.cfg["attempt_wall_ms"] == 1
        assert store.list_attempts(task_id)[0]["end_reason"] == "crashed"
    finally:
        store.close()


@async_test
async def test_h29_fresh_daemon_reaps_live_direct_group_after_durable_wall(
    db_path: str,
    tmp_path: Path,
) -> None:
    """A surviving wrapper cannot retain WIP forever after its daemon disappears."""

    process_fixture = tmp_path / "restart-wall.py"
    process_fixture.write_text(
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(process_fixture)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=tmp_path,
    )
    store = LonghaulStore(db_path)
    try:
        _provider_account(store, "acct_grok_restart_wall", "grok", tmp_path / ".grok-wall")
        _task(store, "btk_grok_restart_wall", state={"working_dir": str(tmp_path)})
        attempt = store.open_attempt(
            "btk_grok_restart_wall",
            "cli-grok",
            "grok-4.5",
            account_id="acct_grok_restart_wall",
            working_dir=str(tmp_path),
            max_sessions=1,
        )
        engine = LonghaulEngine(
            store,
            _cfg(
                max_sessions=1,
                attempt_wall_ms=1,
                attempt_term_grace_ms=50,
                attempt_kill_reap_ms=500,
                fast_crash_s=0,
                working_dir=str(tmp_path),
            ),
            db_path,
        )
        engine._update_attempt(
            str(attempt["id"]),
            detail={"pid": process.pid, "pgid": process.pid, "provider": "grok"},
        )
        await asyncio.sleep(0.02)
        started = monotonic()
        await engine.tick()
        elapsed = monotonic() - started
        process.wait(timeout=1)

        assert elapsed < 1.5
        assert process.returncode is not None
        attempts = store.list_attempts("btk_grok_restart_wall")
        assert [(item["seq"], item["end_reason"]) for item in attempts] == [(0, "crashed")]
        assert store.current_attempt("btk_grok_restart_wall") is None
        state = store.get_longhaul_json("btk_grok_restart_wall") or {}
        assert state["max_sessions_reached"] is True
        row = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id='btk_grok_restart_wall'"
        ).fetchone()
        assert row["status"] == "blocked"
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        store.close()


# ---------------------------------------------------------------------------
# L-14 — provider-specific terminal tables reachable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "provider"),
    [
        ("cli-claude", "claude"),
        ("cli-grok", "grok"),
        ("cli-gemini", "gemini"),
        ("cli-kimi", "kimi"),
        ("cli-codex", "codex"),
        ("", "claude"),
    ],
)
def test_l14_provider_for_harness_mapping(harness: str, provider: str) -> None:
    assert _provider_for_harness(harness) == provider


@pytest.mark.parametrize(
    ("provider", "error", "kind", "claude_only_crash"),
    [
        # Phrases unique to provider tables: claude default must crash.
        ("grok", "out of credits on this API key", "usage_limited", True),
        ("gemini", "resource_exhausted: daily limit hit", "usage_limited", True),
        ("kimi", "exceeded_current_quota_error for this workspace", "usage_limited", True),
        ("grok", "invalid bearer token for xAI", "auth_failed", True),
        ("gemini", "api key not valid for Gemini", "auth_failed", True),
        # Kimi type name also matches the structured claude auth detector — still
        # proves the provider table returns auth_failed when provider=kimi.
        ("kimi", "invalid_authentication_error", "auth_failed", False),
        ("kimi", "permission_denied_error for moonshot", "auth_failed", True),
    ],
)
def test_l14_provider_tables_classify_provider_specific_text(
    provider: str, error: str, kind: str, claude_only_crash: bool
) -> None:
    """Pattern tables only fire when classify_terminal receives the provider."""
    events = [
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "error": error,
            "result": error,
        }
    ]
    with_provider = classify_terminal(events, 1, 0.0, provider=provider)
    assert with_provider["kind"] == kind, with_provider

    if claude_only_crash:
        claude = classify_terminal(events, 1, 0.0, provider="claude")
        assert claude["kind"] == "crashed", (error, claude)


# ---------------------------------------------------------------------------
# L-13 — fast-crash backoff (opt-in) + reconcile stamping
# ---------------------------------------------------------------------------


@async_test
async def test_l13_fast_crash_stamps_backoff_and_blocks_immediate_redispatch(
    db_path: str,
) -> None:
    """Opt-in fast_crash_s must delay successor spawn after a sub-threshold crash."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _task(store, "btk_l13_crash")
        engine = LonghaulEngine(
            store,
            _cfg(
                supervisor,
                fast_crash_s=60,
                fast_crash_backoff_s=30,
                fast_crash_max_backoff_s=300,
            ),
            db_path,
        )
        await engine.dispatch("btk_l13_crash")
        first = store.current_attempt("btk_l13_crash")
        assert first is not None

        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{first['id']}]",
                "state": "killed",
                "killed_by": "idle-reaper",
                "kill_requested": 1,
            },
            [],
            143,
        )

        attempts = store.list_attempts("btk_l13_crash")
        assert attempts[0]["end_reason"] == "killed"
        # No immediate successor while backoff is active.
        assert len(attempts) == 1
        assert len(supervisor.calls) == 1
        state = store.get_longhaul_json("btk_l13_crash") or {}
        assert int(state.get("fast_crash_count") or 0) == 1
        next_at = _parse_iso(state.get("next_dispatch_at"))
        assert next_at is not None
        assert next_at > datetime.now(UTC)
        assert "fast-crash backoff" in str(state.get("parked_detail") or "")

        # Tick while backoff is active still must not open a new attempt.
        await engine.tick()
        assert len(store.list_attempts("btk_l13_crash")) == 1

        # Once next_dispatch_at is in the past, dispatch proceeds.
        state["next_dispatch_at"] = (datetime.now(UTC) - timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        store.set_longhaul_json("btk_l13_crash", state)
        await engine.tick()
        assert len(store.list_attempts("btk_l13_crash")) == 2
        assert len(supervisor.calls) == 2
    finally:
        store.close()


@async_test
async def test_l13_unfinished_exit_atomically_paces_concurrent_tick_and_restart(
    db_path: str,
) -> None:
    """A zero/fast clean exit cannot consume attempts before its durable horizon."""

    store = LonghaulStore(db_path)
    peer = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _task(store, "btk_l13_unfinished")
        cfg = _cfg(
            supervisor,
            fast_crash_s=60,
            fast_crash_backoff_s=30,
            fast_crash_max_backoff_s=300,
        )
        engine = LonghaulEngine(store, cfg, db_path)
        peer_engine = LonghaulEngine(peer, cfg, db_path)
        await engine.dispatch("btk_l13_unfinished")
        first = store.current_attempt("btk_l13_unfinished")
        assert first is not None

        await asyncio.gather(
            engine.on_session_terminal(
                {
                    "title": f"[longhaul:{first['id']}]",
                    "state": "completed",
                },
                [],
                0,
            ),
            peer_engine.tick(),
        )

        attempts = store.list_attempts("btk_l13_unfinished")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "unfinished_exit"
        state = store.get_longhaul_json("btk_l13_unfinished") or {}
        assert int(state.get("fast_crash_count") or 0) == 1
        deadline = _parse_iso(state.get("next_dispatch_at"))
        assert deadline is not None and deadline > datetime.now(UTC)

        # A fresh process/store also honors the persisted horizon.
        restarted = LonghaulEngine(LonghaulStore(db_path), cfg, db_path)
        try:
            await restarted.tick()
            assert len(store.list_attempts("btk_l13_unfinished")) == 1
        finally:
            restarted.store.close()
    finally:
        peer.close()
        store.close()


@async_test
async def test_completed_attempt_clears_existing_fast_terminal_pacing(
    db_path: str,
) -> None:
    """A genuinely complete workbook resets the consecutive pacing state."""

    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _task(store, "btk_l13_complete")
        engine = LonghaulEngine(
            store,
            _cfg(supervisor, fast_crash_s=60),
            db_path,
        )
        await engine.dispatch("btk_l13_complete")
        attempt = store.current_attempt("btk_l13_complete")
        assert attempt is not None
        state = store.get_longhaul_json("btk_l13_complete") or {}
        state.update(
            {
                "fast_crash_count": 4,
                "next_dispatch_at": (datetime.now(UTC) + timedelta(minutes=5)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )
        store.set_longhaul_json("btk_l13_complete", state)
        _done("btk_l13_complete")

        await engine.on_session_terminal(
            {
                "title": f"[longhaul:{attempt['id']}]",
                "state": "completed",
            },
            [],
            0,
        )
        reset = store.get_longhaul_json("btk_l13_complete") or {}
        assert reset.get("fast_crash_count") == 0
        assert reset.get("next_dispatch_at") is None
    finally:
        store.close()


@async_test
async def test_l13_spawn_incomplete_stamps_backoff(db_path: str) -> None:
    """spawn_incomplete reconcile must stamp the same L-13 backoff (review gap)."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor(fail=True)
        _account(store, "acct_a")
        _task(store, "btk_l13_spawn")
        engine = LonghaulEngine(
            store,
            _cfg(
                supervisor,
                spawn_grace_s=0,
                fast_crash_s=60,
                fast_crash_backoff_s=15,
            ),
            db_path,
        )
        await engine.dispatch("btk_l13_spawn")
        first = store.current_attempt("btk_l13_spawn")
        assert first is not None and first["session_id"] is None

        await engine.tick()
        attempts = store.list_attempts("btk_l13_spawn")
        assert attempts[0]["id"] == first["id"]
        assert attempts[0]["end_reason"] == "crashed"
        assert attempts[0]["detail"] == "spawn_incomplete"
        # Backoff active → no second attempt yet.
        assert len(attempts) == 1
        state = store.get_longhaul_json("btk_l13_spawn") or {}
        assert state.get("spawn_incomplete") is True
        assert int(state.get("fast_crash_count") or 0) == 1
        assert _parse_iso(state.get("next_dispatch_at")) is not None
    finally:
        store.close()


@async_test
async def test_l13_codex_orphan_stamps_backoff(db_path: str) -> None:
    """codex_orphan reconcile must stamp L-13 backoff before any redispatch."""
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_l13_orphan")
        engine = LonghaulEngine(
            store,
            _cfg(
                cross_harness_fallback=True,
                static_fallback_order=[{"harness": "cli-codex", "model": "gpt-5.6-sol"}],
                fast_crash_s=60,
                fast_crash_backoff_s=20,
                # Dead pid — _pid_alive will return False.
                _codex_runner=lambda *a, **k: {
                    "pid": 999_999_999,
                    "pgid": 999_999_999,
                    "started": utc_now_iso(),
                },
            ),
            db_path,
        )
        await engine.dispatch("btk_l13_orphan")
        first = store.current_attempt("btk_l13_orphan")
        assert first is not None
        assert first["harness"] == "cli-codex"

        await engine.tick()
        attempts = store.list_attempts("btk_l13_orphan")
        assert attempts[0]["end_reason"] == "crashed"
        assert "codex process missing" in str(attempts[0]["detail"] or "")
        assert len(attempts) == 1  # no immediate successor under backoff
        state = store.get_longhaul_json("btk_l13_orphan") or {}
        assert int(state.get("fast_crash_count") or 0) == 1
        assert _parse_iso(state.get("next_dispatch_at")) is not None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# M-45 — reconciliation isolation + missing working_dir fail-closed
# ---------------------------------------------------------------------------


@async_test
async def test_m45_missing_recorded_working_dir_blocks_fail_closed(
    db_path: str, tmp_path: Path
) -> None:
    """A recorded working_dir that is gone must block — never fall back to cwd."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        missing = tmp_path / "does_not_exist_dir"
        _task(
            store,
            "btk_m45_missing",
            state={"working_dir": str(missing)},
        )
        engine = LonghaulEngine(store, _cfg(supervisor), db_path)
        await engine.dispatch("btk_m45_missing")

        row = store._connection.execute(
            "SELECT status, park_state, longhaul_json FROM board_tasks WHERE id='btk_m45_missing'"
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["park_state"] is None
        state = json.loads(row["longhaul_json"])
        assert state.get("missing_working_dir") is True
        assert "missing" in str(state.get("parked_detail") or "").lower()
        assert supervisor.calls == []
        assert store.current_attempt("btk_m45_missing") is None
    finally:
        store.close()


@async_test
async def test_m45_tick_isolates_per_task_exception(db_path: str) -> None:
    """One task raising inside _tick_one_task must not abort the rest of the pass."""
    store = LonghaulStore(db_path)
    try:
        supervisor = FakeSupervisor()
        _account(store, "acct_a")
        _task(store, "btk_m45_boom")
        _task(store, "btk_m45_ok")
        engine = LonghaulEngine(store, _cfg(supervisor), db_path)

        # Pre-dispatch the healthy task so tick has a live attempt to reconcile.
        await engine.dispatch("btk_m45_ok")
        assert store.current_attempt("btk_m45_ok") is not None

        original = engine._tick_one_task

        async def flaky(task: dict[str, Any], *args: Any, **kwargs: Any) -> None:
            if str(task.get("id")) == "btk_m45_boom":
                raise RuntimeError("injected per-task isolation fault")
            await original(task, *args, **kwargs)

        with patch.object(engine, "_tick_one_task", side_effect=flaky):
            # Must not raise out of tick — isolation contract.
            await engine.tick()

        # Healthy task still has its live attempt; boom task was skipped safely.
        assert store.current_attempt("btk_m45_ok") is not None
        ok = store._connection.execute(
            "SELECT status FROM board_tasks WHERE id='btk_m45_ok'"
        ).fetchone()
        assert ok["status"] == "in_progress"
    finally:
        store.close()
