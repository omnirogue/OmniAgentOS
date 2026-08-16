"""LIVE multi-provider swarm smoke — real CLIs, real logins, real tokens.

Marked ``live`` so default CI suites skip it. Run with::

    uv run pytest -m live tests/swarm/test_live_all_providers.py -v

Prereqs (verified on this host 2026-07-25):
- claude: ``~/.claude-account-3`` (Keychain OAuth)
- codex: ChatGPT login in ``~/.codex``
- grok: ``~/.grok/auth.json``
- gemini: Keychain / ``~/.gemini`` API key + headless trust
- kimi: ``~/.kimi-code`` credentials
- qwen: ``~/.qwen`` provider/API-key or Coding Plan credentials
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from omniagentos.contracts import default_db_path, new_id
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.sessions.supervisor import SessionSupervisor
from omniagentos.swarm.provider_exec import SUPPORTED_PROVIDERS, ProviderSessionRunner

pytestmark = pytest.mark.live

# Cheap models where the CLI accepts a model flag; kimi uses its config default.
PROVIDER_MODELS: dict[str, str] = {
    "claude": "haiku",
    "codex": "gpt-5.6-luna",
    "grok": "grok-4.5",
    "gemini": "gemini-2.5-flash",
    "kimi": "",  # omit -m; use config.toml default_model
    "qwen": "",  # omit -m; use the provider configured in ~/.qwen
}

REQUIRED_BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "gemini": "gemini",
    "kimi": "kimi",
    "qwen": "qwen",
}


def _require_binaries() -> None:
    missing = [name for name, binary in REQUIRED_BINARIES.items() if shutil.which(binary) is None]
    if missing:
        pytest.skip(f"CLI binaries not on PATH: {missing}")


def _await_session(
    dal: SessionsDal,
    session_id: str,
    *,
    timeout_s: float = 180.0,
    poll_s: float = 0.5,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = dal.get_session(session_id)
        if row is None:
            time.sleep(poll_s)
            continue
        state = str(row.get("state") or "")
        if state in {
            SessionState.COMPLETED.value,
            SessionState.FAILED.value,
            SessionState.CANCELLED.value,
            SessionState.KILLED.value,
        }:
            return row
        time.sleep(poll_s)
    row = dal.get_session(session_id) or {}
    raise AssertionError(
        f"session {session_id} did not terminalize within {timeout_s}s; "
        f"last state={row.get('state')!r} error={row.get('error')!r}"
    )


@pytest.fixture()
def live_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "live-swarm-ws"
    ws.mkdir()
    (ws / "README.md").write_text("# live multi-provider swarm workspace\n", encoding="utf-8")
    return ws


def test_live_provider_exec_all_non_claude(live_workspace: Path, tmp_path: Path) -> None:
    """Spawn codex/grok/gemini/kimi/qwen via ProviderSessionRunner and await completion."""
    _require_binaries()
    db = str(tmp_path / "live-providers.db")
    # Point at the product DB for account config_dir lookup when account_id is set;
    # sessions themselves stay in the temp DB so we don't pollute product sessions.
    product_db = default_db_path()
    runner = ProviderSessionRunner(db_path=db, wall_timeout_seconds=150)
    # Allow account config lookup against the product DB (seeded accounts).
    runner.db_path = db

    # Load gemini key into this process so _provider_env can forward it.
    gemini_env = Path.home() / ".gemini" / ".env"
    if gemini_env.exists():
        for line in gemini_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

    dal = SessionsDal(db)
    results: dict[str, dict] = {}
    try:
        for provider in sorted(SUPPORTED_PROVIDERS):
            model = PROVIDER_MODELS[provider]
            prompt = (
                f"You are a multi-provider swarm worker for {provider}. "
                f"Reply with exactly: LIVE-OK-{provider.upper()} and nothing else."
            )
            # Prefer product-seeded account id when present.
            account_id = None
            try:
                import sqlite3

                conn = sqlite3.connect(product_db)
                row = conn.execute(
                    "SELECT id FROM claude_accounts "
                    "WHERE provider = ? AND enabled = 1 ORDER BY id LIMIT 1",
                    (provider,),
                ).fetchone()
                conn.close()
                if row:
                    account_id = str(row[0])
            except Exception:
                account_id = None

            # ProviderSessionRunner looks up config_dir from ITS db_path; seed
            # a matching account row into the temp DB when we have a product id.
            # (SessionsDal migrate already created the full claude_accounts schema.)
            if account_id:
                import sqlite3

                from omniagentos.contracts import utc_now_iso

                cfg = {
                    "codex": str(Path.home() / ".codex"),
                    "grok": str(Path.home() / ".grok"),
                    "gemini": str(Path.home() / ".gemini"),
                    "kimi": str(Path.home() / ".kimi-code"),
                    "qwen": str(Path.home() / ".qwen"),
                }[provider]
                conn = sqlite3.connect(db)
                now = utc_now_iso()
                conn.execute(
                    "INSERT OR REPLACE INTO claude_accounts "
                    "(id, label, auth_type, config_dir, enabled, is_default, "
                    "status, created_at, updated_at, provider) "
                    "VALUES (?, ?, 'config_dir', ?, 1, 0, 'ok', ?, ?, ?)",
                    (account_id, f"{provider}-live", cfg, now, now, provider),
                )
                conn.commit()
                conn.close()

            session_id = runner.spawn(
                provider=provider,
                model=model,
                prompt=prompt,
                working_dir=str(live_workspace),
                board_task_id=f"live-{provider}",
                swarm_run_id="swr_live_all",
                budget_usd_max=1.0,
                idle_minutes=5.0,
                risk_class="none",
                account_id=account_id,
                wall_timeout_seconds=150,
                effort="low",
            )
            # Pump until terminal (provider-exec tracks processes in-process).
            outcome = runner.await_terminal(session_id, 160, poll=0.25)
            row = dal.get_session(session_id) or {}
            results[provider] = {
                "session_id": session_id,
                "await": getattr(outcome, "status", outcome),
                "state": row.get("state"),
                "error": row.get("error"),
                "output": (row.get("output_text") or "")[:300],
            }
            assert row.get("state") == SessionState.COMPLETED.value, (
                f"{provider} failed: {results[provider]}"
            )
    finally:
        try:
            dal.close()
        except Exception:
            pass

    assert set(results) == set(SUPPORTED_PROVIDERS)
    for _provider, info in results.items():
        assert info["state"] == "completed", info


def test_live_claude_bridge_account_3(live_workspace: Path, tmp_path: Path) -> None:
    """Claude bridge spawn via SessionSupervisor using the live account-3 login."""
    _require_binaries()
    if shutil.which("claude") is None:
        pytest.skip("claude not on PATH")

    account3 = Path.home() / ".claude-account-3"
    if not account3.is_dir():
        pytest.skip("~/.claude-account-3 missing")

    db = str(tmp_path / "live-claude.db")
    import sqlite3

    from omniagentos.contracts import utc_now_iso

    # Force pick_account (default_db_path) onto this temp DB BEFORE any spawn.
    prev_db = os.environ.get("OMNIAGENTOS_DB")
    os.environ["OMNIAGENTOS_DB"] = db
    try:
        dal = SessionsDal(db)  # full migrate, including claude_accounts
        now = utc_now_iso()
        account_id = new_id("acct")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO claude_accounts "
            "(id, label, auth_type, config_dir, enabled, is_default, status, "
            "created_at, updated_at, provider) "
            "VALUES (?, 'account-3-live', 'config_dir', ?, 1, 1, 'ok', ?, ?, 'claude')",
            (account_id, str(account3), now, now),
        )
        conn.commit()
        conn.close()

        from omniagentos.routing.limit_state import pick_account

        picked = pick_account("claude", db_path=db)
        assert picked is not None
        assert "claude-account-3" in (picked.config_dir or "")

        supervisor = SessionSupervisor(db_path=db)
        session_id = supervisor.spawn(
            project_dir=str(live_workspace),
            model="haiku",
            prompt="Reply with exactly: LIVE-OK-CLAUDE and nothing else.",
            budget_usd_max=1.0,
            title="live-claude-all-providers",
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            supervisor.run_once()
            row = dal.get_session(session_id)
            if row and str(row.get("state")) in {
                SessionState.COMPLETED.value,
                SessionState.FAILED.value,
                SessionState.KILLED.value,
                SessionState.CANCELLED.value,
            }:
                break
            time.sleep(0.4)
        row = dal.get_session(session_id) or {}
        assert row.get("state") == SessionState.COMPLETED.value, (
            f"claude failed: state={row.get('state')} error={row.get('error')}"
        )
    finally:
        if prev_db is None:
            os.environ.pop("OMNIAGENTOS_DB", None)
        else:
            os.environ["OMNIAGENTOS_DB"] = prev_db
        try:
            dal.close()
        except Exception:
            pass
