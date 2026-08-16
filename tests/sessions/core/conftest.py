from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.sessions import hook_token, ssh_keys
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.lifecycle import (
    _OPEN_SUPERVISORS,
    _OPEN_SUPERVISORS_LOCK,
    SessionSupervisorCloseError,
    reset_drain_failures,
)
from tests.support.db_template import migrated_db


@pytest.fixture(autouse=True)
def _drain_evidence_isolated() -> None:
    """H-39: the atexit drain records failures in a module-level ledger so the
    evidence survives a process teardown that has no caller to raise into. That
    ledger is global, so clear it around every test."""
    reset_drain_failures()
    yield
    reset_drain_failures()


@pytest.fixture(autouse=True)
def _sandbox_available_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep supervisor plumbing tests independent of the host OS sandbox.

    Confinement is proved by runner guardrail tests. The dedicated fail-closed
    supervisor test overrides this to False.
    """
    monkeypatch.setattr("omniagentos.runner.sandbox.sandbox_available", lambda: True)


@pytest.fixture(autouse=True)
def _hook_tokens_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the per-session hook-eval credential store (AC-policy hook-auth)
    test-local, mirroring the sessions-token isolation in tests/api/conftest.py.
    Every ``_launch`` now mints/revokes one of these files; without this the
    whole supervisor test suite would write real (if harmless, gitignored)
    files into this worktree's ``var/sessions/hook-tokens/``."""
    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")


@pytest.fixture
def sessions_dal(tmp_path: Path) -> SessionsDal:
    """Sessions DAL fixture that drains attached supervisors before close (H-39)."""
    dal = SessionsDal(migrated_db(SessionsDal, tmp_path / "sessions.db"))
    yield dal
    # Join any supervisor still attached to this DAL before closing the
    # connection — the original OA-005 failure mode was fixture teardown
    # racing reader/finalizer threads.
    with _OPEN_SUPERVISORS_LOCK:
        attached = [sup for sup in list(_OPEN_SUPERVISORS) if getattr(sup, "dal", None) is dal]
    for supervisor in attached:
        try:
            supervisor.close(join_timeout=2.0, terminate_children=True)
        except SessionSupervisorCloseError:
            # Surface was already raised to the closer; still force-close so the
            # next test does not inherit a poisoned connection.
            try:
                supervisor._close_dal()
            except Exception:  # noqa: BLE001 - best-effort fixture isolation
                pass
    dal.close()


def seed_session(
    dal: SessionsDal,
    project_dir: Path,
    *,
    session_id: str = "ses_test",
    state: str = "starting",
    pid: int | None = None,
    granted_roots: str | None = None,
) -> str:
    row = {
        "id": session_id,
        "source": "bridge",
        "project_dir": str(project_dir),
        "provider": "claude",
        "session_ref": "11111111-2222-3333-4444-555555555555",
        "state": state,
        "pid": pid,
        "model": "haiku",
    }
    if granted_roots is not None:
        row["granted_roots"] = granted_roots
    dal.create_session(row)
    return session_id
