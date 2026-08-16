"""S14D/L14 migration 072 and joined L06 provider-path acceptance tests.

These tests never rewrite migration 043, never fabricate a task_sessions schema,
and never replace LonghaulEngine's durable attempt lookup. The only process seam
changes adapter argv so a real local subprocess emits deterministic provider
errors without contacting live subscription services.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import patch

import pytest

import omniagentos.db.migrate as migrate_module
from omniagentos.adapters.gemini import GeminiAdapter
from omniagentos.adapters.grok import GrokAdapter
from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import workbook
from omniagentos.longhaul.engine import LonghaulEngine
from omniagentos.longhaul.store import LonghaulStore

_MIGRATION_043_SHA256 = "6c0d5310061b35805f8fe8396419b383f478d922112c9b7a8c54f38b4ac7e0bb"
_MIGRATION_072_SHA256 = "573cc39d8b4631228aab02881a6ee2b813b92fb447d8f886e60265db6708e8f2"
_PROVIDER_CASES = [
    (
        "cli-grok",
        "grok-4.5",
        GrokAdapter,
        "out of credits on this API key",
        "usage_limited",
        "rate_limited",
        1,
    ),
    (
        "cli-grok",
        "grok-4.5",
        GrokAdapter,
        "invalid bearer token for xAI",
        "auth_failed",
        "error",
        0,
    ),
    (
        "cli-gemini",
        "gemini-3.1-pro",
        GeminiAdapter,
        "resource_exhausted: daily limit hit",
        "usage_limited",
        "rate_limited",
        1,
    ),
    (
        "cli-gemini",
        "gemini-3.1-pro",
        GeminiAdapter,
        "api key not valid for Gemini",
        "auth_failed",
        "error",
        0,
    ),
    (
        "cli-kimi",
        "kimi-k3",
        KimiAdapter,
        "exceeded_current_quota_error for this workspace",
        "usage_limited",
        "rate_limited",
        1,
    ),
    (
        "cli-kimi",
        "kimi-k3",
        KimiAdapter,
        "permission_denied_error for moonshot",
        "auth_failed",
        "error",
        0,
    ),
]


def async_test(function: Any) -> Any:
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return run


def _migration_path(version: int) -> Path:
    return next(path for number, path in migrate_module._migration_files() if number == version)


def _head_version() -> int:
    """Highest packaged migration version — what ``migrate`` reports when done.

    Migrations past 072 keep landing (073+); these tests pin 072's behaviour,
    not the repository head, so the expected return value is computed rather
    than hard-coded.
    """
    return max(number for number, _ in migrate_module._migration_files())


def _migrate_without_072(path: str) -> None:
    migrations = [entry for entry in migrate_module._migration_files() if entry[0] != 72]
    with patch.object(migrate_module, "_migration_files", return_value=migrations):
        migrate(path)


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _task(store: LonghaulStore, task_id: str, working_dir: Path) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks "
        "(id,title,description,status,created_at,updated_at,lane,longhaul_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            task_id,
            f"Task {task_id}",
            "Exercise the real persisted provider path.",
            "pending",
            now,
            now,
            "longhaul",
            json.dumps({"working_dir": str(working_dir)}),
        ),
    )
    store._connection.commit()


def _account(store: LonghaulStore, provider: str, config_dir: Path) -> str:
    account_id = f"acct_{provider}_072"
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
    return account_id


def _cfg(harness: str, model: str, working_dir: Path) -> dict[str, Any]:
    return {
        "max_sessions": 8,
        "default_cooldown_s": 3600,
        "cross_harness_fallback": True,
        "static_fallback_order": [{"harness": harness, "model": model}],
        "review": {"enabled": False},
        "fast_crash_s": 60,
        "fast_crash_backoff_s": 30,
        "attempt_wall_ms": 2_000,
        "attempt_term_grace_ms": 100,
        "attempt_kill_reap_ms": 500,
        "unattended_elevated_harnesses": [harness],
        "working_dir": str(working_dir),
    }


async def _wait_until(predicate: Any, timeout_s: float = 3.0) -> None:
    deadline = monotonic() + timeout_s
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.02)


def test_043_and_072_exact_file_checksums() -> None:
    """Freeze historical 043 and the one 072 object B04/B07 must absorb."""

    assert hashlib.sha256(_migration_path(43).read_bytes()).hexdigest() == (_MIGRATION_043_SHA256)
    assert hashlib.sha256(_migration_path(72).read_bytes()).hexdigest() == (_MIGRATION_072_SHA256)


def test_072_rebuild_preserves_rows_columns_indexes_and_constraints(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "upgrade.db")
    _migrate_without_072(db_path)
    connection = _connect(db_path)
    now = utc_now_iso()
    for task_id in ("btk_old_claude", "btk_live_codex"):
        connection.execute(
            "INSERT INTO board_tasks "
            "(id,title,description,status,created_at,updated_at,lane,longhaul_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, task_id, "", "in_progress", now, now, "longhaul", "{}"),
        )
    connection.execute(
        "INSERT INTO task_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tks_old",
            "btk_old_claude",
            0,
            "ses_old",
            "cli-claude",
            "opus",
            "acct_old",
            now,
            now,
            "completed",
            "preserve me",
        ),
    )
    connection.execute(
        "INSERT INTO task_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tks_live",
            "btk_live_codex",
            0,
            None,
            "cli-codex",
            "gpt-5.6-sol",
            None,
            now,
            None,
            None,
            "",
        ),
    )
    connection.commit()
    expected_rows = [
        tuple(row)
        for row in connection.execute("SELECT * FROM task_sessions ORDER BY id").fetchall()
    ]
    expected_columns = [
        tuple(row) for row in connection.execute("PRAGMA table_info(task_sessions)").fetchall()
    ]
    expected_foreign_keys = [
        tuple(row)
        for row in connection.execute("PRAGMA foreign_key_list(task_sessions)").fetchall()
    ]
    expected_indexes = {
        row["name"]: (int(row["unique"]), int(row["partial"]))
        for row in connection.execute("PRAGMA index_list(task_sessions)").fetchall()
        if not str(row["name"]).startswith("sqlite_autoindex")
    }
    connection.close()

    assert migrate(db_path) == _head_version()
    connection = _connect(db_path)
    try:
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM task_sessions ORDER BY id").fetchall()
        ] == expected_rows
        assert [
            tuple(row) for row in connection.execute("PRAGMA table_info(task_sessions)").fetchall()
        ] == expected_columns
        assert [
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_list(task_sessions)").fetchall()
        ] == expected_foreign_keys
        assert (
            {
                row["name"]: (int(row["unique"]), int(row["partial"]))
                for row in connection.execute("PRAGMA index_list(task_sessions)").fetchall()
                if not str(row["name"]).startswith("sqlite_autoindex")
            }
            == expected_indexes
            == {
                "idx_task_sessions_task": (0, 0),
                "idx_task_sessions_live": (1, 1),
                "idx_task_sessions_session": (1, 1),
            }
        )

        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_sessions'"
            ).fetchone()["sql"]
        )
        for harness in (
            "cli-claude",
            "cli-codex",
            "cli-grok",
            "cli-gemini",
            "cli-kimi",
        ):
            assert f"'{harness}'" in table_sql

        connection.execute(
            "INSERT INTO task_sessions "
            "(id,board_task_id,seq,harness,model,started_at) "
            "VALUES ('tks_grok','btk_grok',0,'cli-grok','grok-4.5',?)",
            (now,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_sessions "
                "(id,board_task_id,seq,harness,model,started_at) "
                "VALUES ('tks_bad','btk_bad',0,'cli-unknown','bad',?)",
                (now,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_sessions "
                "(id,board_task_id,seq,harness,model,started_at,end_reason) "
                "VALUES ('tks_reason','btk_reason',0,'cli-grok','grok-4.5',?,'bogus')",
                (now,),
            )
        connection.rollback()
    finally:
        connection.close()

    # Repeat-safe through the repository runner: no duplicate application row.
    assert migrate(db_path) == _head_version()
    connection = _connect(db_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 72"
            ).fetchone()["n"]
            == 1
        )
        migration_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(schema_migrations)").fetchall()
        }
        # Consolidated L14 migration 068 adds this column. On the joined tree,
        # the repository checksum runner must record the exact 072 artifact;
        # on this isolated prerequisite branch 068–071 intentionally are not
        # duplicated, so the conditional keeps the artifact independently
        # testable.
        if "checksum" in migration_columns:
            checksums = {
                int(row["version"]): str(row["checksum"])
                for row in connection.execute(
                    "SELECT version,checksum FROM schema_migrations WHERE version IN (43,72)"
                ).fetchall()
            }
            assert checksums == {
                43: _MIGRATION_043_SHA256,
                72: _MIGRATION_072_SHA256,
            }
    finally:
        connection.close()


@async_test
async def test_pre_072_public_provider_dispatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "pre-072.db")
    _migrate_without_072(db_path)
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks-pre")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_pre_072", tmp_path)
        engine = LonghaulEngine(store, _cfg("cli-grok", "grok-4.5", tmp_path), db_path)
        await engine.dispatch("btk_pre_072")

        assert store.current_attempt("btk_pre_072") is None
        assert store.list_attempts("btk_pre_072") == []
        row = store._connection.execute(
            "SELECT status, park_state, longhaul_json FROM board_tasks WHERE id='btk_pre_072'"
        ).fetchone()
        assert row["status"] == "in_progress"
        assert row["park_state"] == "waiting_capacity"
        assert "no enabled worker capacity" in json.loads(row["longhaul_json"])["parked_detail"]
    finally:
        store.close()


@pytest.mark.parametrize(
    (
        "harness",
        "model",
        "adapter_class",
        "error_text",
        "end_reason",
        "account_status",
        "enabled",
    ),
    _PROVIDER_CASES,
)
@async_test
async def test_post_072_public_provider_dispatch_persists_and_classifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    model: str,
    adapter_class: type[Any],
    error_text: str,
    end_reason: str,
    account_status: str,
    enabled: int,
) -> None:
    db_path = str(tmp_path / f"{harness}-{end_reason}.db")
    assert migrate(db_path) == _head_version()
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks")
    provider = harness.removeprefix("cli-")
    process_fixture = tmp_path / f"{provider}-{end_reason}.py"
    process_fixture.write_text(
        f"import sys\nsys.stderr.write({error_text!r})\nsys.stderr.flush()\nraise SystemExit(1)\n",
        encoding="utf-8",
    )

    store = LonghaulStore(db_path)
    try:
        account_id = _account(store, provider, tmp_path / f".{provider}")
        task_id = f"btk_{provider}_{end_reason}"
        _task(store, task_id, tmp_path)
        engine = LonghaulEngine(store, _cfg(harness, model, tmp_path), db_path)

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
        assert attempts[0]["model"] == model
        assert attempts[0]["account_id"] == account_id
        assert attempts[0]["end_reason"] == end_reason
        assert error_text in str(attempts[0]["detail"])

        account = store._connection.execute(
            "SELECT enabled,status,cooldown_until,status_detail FROM claude_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        assert account["enabled"] == enabled
        assert account["status"] == account_status
        if end_reason == "usage_limited":
            assert account["cooldown_until"] is not None
        else:
            assert account["cooldown_until"] is None
        assert error_text in str(account["status_detail"])

        state = store.get_longhaul_json(task_id) or {}
        assert state["prior_end_reason"] == end_reason
        # Provider usage/auth already owns account cooldown/disable. Stacking a
        # task-level crash backoff would delay a healthy alternate account.
        assert state["fast_crash_count"] == 0
        assert state["next_dispatch_at"] is None
    finally:
        store.close()
