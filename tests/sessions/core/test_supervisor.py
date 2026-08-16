from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.sessions import ssh_keys
from omniagentos.sessions import supervisor as supervisor_module
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    SessionSpawnError,
    SessionSupervisor,
    _is_auth_failure,
    bridge_settings_path,
    build_clean_env,
)

from .conftest import seed_session


def test_bridge_hook_matcher_is_gate_everything() -> None:
    """T-CODE-003: the gating PreToolUse matcher MUST be '.*' in every code path."""
    path = bridge_settings_path()
    content = json.loads(Path(path).read_text(encoding="utf-8"))
    matchers = [entry["matcher"] for entry in content["hooks"]["PreToolUse"]]
    assert matchers == [".*"]


def test_bridge_settings_fail_closed_when_installer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No weak-matcher fallback: a missing installer FAILS rather than under-gating."""

    def boom(_name: str) -> object:
        raise ImportError("installer unavailable")

    monkeypatch.setattr(supervisor_module.importlib, "import_module", boom)
    with pytest.raises(RuntimeError, match="gating"):
        bridge_settings_path()


class FakeProcess:
    next_pid = 50000

    def __init__(self, lines: list[dict[str, Any]] | None = None, returncode: int = 0) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.stdout = [json.dumps(line) + "\n" for line in (lines or [])]
        self.returncode: int | None = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return int(self.returncode or 0)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def wait_for_state(dal: SessionsDal, session_id: str, state: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = dal.get_session(session_id)
        if row is not None and row["state"] == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"session {session_id} did not reach {state}")


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"file did not become durable: {path}")


def _unwrap_sandbox(argv: list[str]) -> list[str]:
    """Strip a leading `sandbox-exec -p <profile>` wrapper (AC-policy #B4) so argv
    assertions still target the real Claude command. The wrap itself is asserted by
    test_session_launch_is_sandbox_confined."""
    if len(argv) >= 4 and argv[0].endswith("sandbox-exec") and argv[1] == "-p":
        return argv[3:]
    return argv


def fake_factory(
    captures: list[tuple[list[str], dict[str, Any]]],
    process_builder: Callable[[], FakeProcess],
) -> Callable[..., Any]:
    def factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        captures.append((_unwrap_sandbox(argv), kwargs))
        return process_builder()

    return factory


def _spawn_after_auth_failure(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    retry_auth_failure: bool = False,
) -> tuple[
    str, list[tuple[str, str]], list[tuple[str, str]], list[tuple[list[str], dict[str, Any]]]
]:
    """Launch a 401-failed account followed by a successful replacement account."""

    notifications: list[tuple[str, str]] = []
    status_updates: list[tuple[str, str]] = []
    enabled_updates: list[tuple[str, str]] = []
    captures: list[tuple[list[str], dict[str, Any]]] = []
    selections = iter(
        [
            (str(tmp_path / "account-one"), {}, "acct_one", "Account one"),
            (str(tmp_path / "account-two"), {}, "acct_two", "Account two"),
        ]
    )
    processes = [
        FakeProcess(
            [
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "error_status": 401,
                    "error": "authentication_failed",
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "error", "total_cost_usd": 0},
            ],
            returncode=1,
        ),
        FakeProcess(
            (
                [
                    {
                        "type": "result",
                        "subtype": "error",
                        "result": "Error: 401 Invalid authentication credentials",
                    }
                ]
                if retry_auth_failure
                else [{"type": "result", "subtype": "success", "total_cost_usd": 0}]
            ),
            returncode=1 if retry_auth_failure else 0,
        ),
    ]
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: next(selections))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.mark_status",
        lambda account_id, status, detail: status_updates.append(
            (account_id, f"{status}:{detail}")
        ),
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_updates.append((account_id, str(enabled))),
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: processes.pop(0)),
        notifier=lambda title, body: notifications.append((title, body)),
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work", title="original prompt")
    wait_for_state(sessions_dal, session_id, "failed" if retry_auth_failure else "completed")
    return session_id, notifications, enabled_updates + status_updates, captures


def test_auth_failure_detector_matches_401_oauth_revoked() -> None:
    assert _is_auth_failure(
        {
            "type": "result",
            "subtype": "error",
            "result": "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        }
    )
    assert _is_auth_failure(
        {
            "type": "result",
            "terminal_reason": "error",
            "result": "Error: 401 Invalid authentication credentials",
        }
    )


def test_auth_failure_detector_ignores_normal_errors() -> None:
    assert not _is_auth_failure(
        {"type": "result", "subtype": "error", "result": "Error: rate limited"}
    )
    assert not _is_auth_failure({"type": "result", "subtype": "error", "result": "Nonzero exit"})


def test_auth_failure_assistant_requires_corroboration(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hardening: assistant text with auth keyword requires system api_retry hint to trigger auth.

    Three cases:
    (a) assistant "invalid api key" + error + cost>0 → NO disable (cost gate)
    (b) assistant "invalid api key" + error + cost==0 + NO hint → NO disable (no corroboration)
    (c) assistant "invalid api key" + api_retry 401 hint + error + cost==0 → disable + retry
    """
    # Case (a): cost > 0 blocks auth path entirely
    notifications_a: list[tuple[str, str]] = []
    enabled_a: list[tuple[str, str]] = []
    captures_a: list[tuple[list[str], dict[str, Any]]] = []
    selections_a = iter([(str(tmp_path / "acct-a"), {}, "acct_a", "Account A")])
    processes_a = [
        FakeProcess(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "The service returned invalid api key on startup",
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "error", "total_cost_usd": 0.05},
            ],
            returncode=1,
        ),
    ]
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: next(selections_a))
    monkeypatch.setattr(
        "omniagentos.accounts.service.mark_status",
        lambda account_id, status, detail: None,
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_a.append((account_id, str(enabled))),
    )
    supervisor_a = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger-a"),
        process_factory=fake_factory(captures_a, lambda: processes_a.pop(0)),
        notifier=lambda title, body: notifications_a.append((title, body)),
    )
    session_id_a = supervisor_a.spawn(str(tmp_path), "haiku", "task-a")
    wait_for_state(sessions_dal, session_id_a, "failed")
    assert not any("acct_a" in str(u) for u in enabled_a)  # NOT disabled
    assert len(captures_a) == 1  # NO retry

    # Case (b): cost==0 but NO api_retry hint → no corroboration, no auth
    notifications_b: list[tuple[str, str]] = []
    enabled_b: list[tuple[str, str]] = []
    captures_b: list[tuple[list[str], dict[str, Any]]] = []
    processes_b = [
        FakeProcess(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "returns invalid api key on startup"}]
                    },
                },
                {"type": "result", "subtype": "error", "total_cost_usd": 0},
            ],
            returncode=1,
        ),
    ]
    selections_b_iter = iter([(str(tmp_path / "acct-b"), {}, "acct_b", "Account B")])
    monkeypatch.setattr(
        supervisor_module, "_resolve_spawn_account", lambda: next(selections_b_iter)
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.mark_status",
        lambda account_id, status, detail: None,
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_b.append((account_id, str(enabled))),
    )
    supervisor_b = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger-b"),
        process_factory=fake_factory(captures_b, lambda: processes_b.pop(0)),
        notifier=lambda title, body: notifications_b.append((title, body)),
    )
    session_id_b = supervisor_b.spawn(str(tmp_path), "haiku", "task-b")
    wait_for_state(sessions_dal, session_id_b, "failed")
    assert not any("acct_b" in str(u) for u in enabled_b)  # NOT disabled (no hint)
    assert len(captures_b) == 1  # NO retry

    # Case (c): api_retry 401 hint + assistant keyword + error + cost==0 → corroborated auth
    notifications_c: list[tuple[str, str]] = []
    enabled_c: list[tuple[str, str]] = []
    captures_c: list[tuple[list[str], dict[str, Any]]] = []
    selections_c = iter(
        [
            (str(tmp_path / "acct-c-broken"), {}, "acct_c", "Account C"),
            (str(tmp_path / "acct-c-backup"), {}, "acct_c_bak", "Account C Backup"),
        ]
    )
    processes_c = [
        FakeProcess(
            [
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "error_status": 401,
                    "error": "authentication_failed",
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "invalid api key"}]},
                },
                {"type": "result", "subtype": "error", "total_cost_usd": 0},
            ],
            returncode=1,
        ),
        FakeProcess(
            [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
            returncode=0,
        ),
    ]
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: next(selections_c))
    monkeypatch.setattr(
        "omniagentos.accounts.service.mark_status",
        lambda account_id, status, detail: None,
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_c.append((account_id, str(enabled))),
    )
    supervisor_c = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger-c"),
        process_factory=fake_factory(captures_c, lambda: processes_c.pop(0)),
        notifier=lambda title, body: notifications_c.append((title, body)),
    )
    session_id_c = supervisor_c.spawn(str(tmp_path), "haiku", "task-c")
    wait_for_state(sessions_dal, session_id_c, "completed")
    # Auth IS detected (hint present + keyword + error)
    assert ("acct_c", "False") in enabled_c  # DISABLED
    assert len(captures_c) == 2  # Retry happened


def test_auth_failure_disables_account_and_respawns(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _notifications, account_updates, captures = _spawn_after_auth_failure(
        sessions_dal, tmp_path, monkeypatch
    )

    assert ("acct_one", "False") in account_updates
    assert (
        "acct_one",
        "error:Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
    ) in account_updates
    assert len(captures) == 2
    assert captures[1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "account-two")
    assert sessions_dal.get_session(session_id)["state"] == "completed"  # type: ignore[index]


def test_auth_failure_notifies_operator(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_id, notifications, _account_updates, _captures = _spawn_after_auth_failure(
        sessions_dal, tmp_path, monkeypatch
    )

    assert notifications == [
        (
            "Claude login broken — Account one",
            f"Account Account one failed 401 authentication. To restore: "
            f"CLAUDE_CONFIG_DIR={tmp_path / 'account-one'} claude /login",
        )
    ]


def test_auth_failure_persists_error(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _notifications, _account_updates, _captures = _spawn_after_auth_failure(
        sessions_dal, tmp_path, monkeypatch
    )

    # F3: On successful completion after retry, error is cleared
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "completed"
    assert row["error"] is None  # Cleared on successful completion


def test_auth_failure_error_retained_on_terminal_failure(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F3: Failed terminal session retains error text
    session_id, _notifications, _account_updates, _captures = _spawn_after_auth_failure(
        sessions_dal, tmp_path, monkeypatch, retry_auth_failure=True
    )

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert "401" in (row["error"] or "")  # Error retained on terminal failure


def test_auth_failure_retry_is_consumed_after_one_respawn(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, notifications, account_updates, captures = _spawn_after_auth_failure(
        sessions_dal, tmp_path, monkeypatch, retry_auth_failure=True
    )

    assert len(captures) == 2
    assert ("acct_two", "False") in account_updates
    assert any(
        title == "ALL Claude logins broken — sessions cannot run" for title, _ in notifications
    )
    assert sessions_dal.get_session(session_id)["state"] == "failed"  # type: ignore[index]


def test_auth_failure_real_oauth_expired_fixture(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real fixture: oauth session expired (system api_retry + assistant + result with is_error).

    This is the EXACT three-event sequence captured from claude CLI 2.1.218 with a
    revoked OAuth account. Must detect auth failure even though:
    - result.subtype is "success" (not "error")
    - result.terminal_reason is "api_error" (contains, not starts with "error")
    - process exits with code 0 (not 1)
    - no "401" string appears in text
    """
    notifications: list[tuple[str, str]] = []
    status_updates: list[tuple[str, str]] = []
    enabled_updates: list[tuple[str, str]] = []
    captures: list[tuple[list[str], dict[str, Any]]] = []
    selections = iter(
        [
            (str(tmp_path / "oauth-account"), {}, "acct_oauth", "OAuth Account"),
            (str(tmp_path / "backup-account"), {}, "acct_backup", "Backup Account"),
        ]
    )
    # Real fixture from coordinator: system api_retry -> assistant -> result(is_error=true)
    processes = [
        FakeProcess(
            [
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "attempt": 1,
                    "max_retries": 10,
                    "retry_delay_ms": 587,
                    "error_status": 401,
                    "error": "authentication_failed",
                },
                {
                    "type": "assistant",
                    "message": {
                        "model": "<synthetic>",
                        "content": [
                            {
                                "type": "text",
                                "text": "Failed to authenticate: OAuth session expired and could not be refreshed",
                            }
                        ],
                    },
                    "error": "authentication_failed",
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "terminal_reason": "api_error",
                    "api_error_status": None,
                    "total_cost_usd": 0,
                    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
                    "num_turns": 1,
                },
            ],
            returncode=0,  # exits 0 even on auth failure!
        ),
        FakeProcess(
            [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
            returncode=0,
        ),
    ]
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: next(selections))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.mark_status",
        lambda account_id, status, detail: status_updates.append(
            (account_id, f"{status}:{detail}")
        ),
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_updates.append((account_id, str(enabled))),
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: processes.pop(0)),
        notifier=lambda title, body: notifications.append((title, body)),
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "task", title="oauth test")
    wait_for_state(sessions_dal, session_id, "completed")

    # Auth failure MUST be detected despite code 0, is_error=true caught it
    assert ("acct_oauth", "False") in enabled_updates
    assert any("OAuth Account" in str(n) for n in notifications)
    assert len(captures) == 2  # Two spawns: failed + retry
    # Verify respawn used the backup account
    assert captures[1][1]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "backup-account")
    assert sessions_dal.get_session(session_id)["state"] == "completed"


def test_auth_failure_rate_limited_not_auth(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: system api_retry with error_status 429 or error='rate_limited' NOT auth."""
    notifications: list[tuple[str, str]] = []
    enabled_updates: list[tuple[str, str]] = []
    captures: list[tuple[list[str], dict[str, Any]]] = []
    selections = iter(
        [
            (str(tmp_path / "account-one"), {}, "acct_one", "Account one"),
        ]
    )
    # Rate limit (429) is NOT an auth failure
    processes = [
        FakeProcess(
            [
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "attempt": 1,
                    "error_status": 429,
                    "error": "rate_limited",
                },
                {"type": "result", "subtype": "error", "total_cost_usd": 0},
            ],
            returncode=1,
        ),
    ]
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: next(selections))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    monkeypatch.setattr(
        "omniagentos.accounts.service.set_enabled",
        lambda account_id, enabled: enabled_updates.append((account_id, str(enabled))),
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: processes.pop(0)),
        notifier=lambda title, body: notifications.append((title, body)),
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "task")
    wait_for_state(sessions_dal, session_id, "failed")

    # Account NOT disabled (not an auth failure)
    assert not any("acct_one" in str(u) for u in enabled_updates)
    # No retry (only one spawn attempted)
    assert len(captures) == 1
    assert sessions_dal.get_session(session_id)["state"] == "failed"


def test_spawn_uses_argv_clean_env_streams_cost_and_completes(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This asserts the FULL argv including --max-budget-usd, so it pins the
    # blocking posture (opt-in since 2026-07-24). The advisory default omits the
    # flag entirely -- covered by tests/budget/test_budget_policy.py.
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    captures: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess(
        [
            {"type": "system", "subtype": "init", "session_id": "claude-ref"},
            {"type": "result", "subtype": "success", "total_cost_usd": 0.125},
        ]
    )
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: process),
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work", 1.5, "title")
    wait_for_state(sessions_dal, session_id, "completed")
    argv, kwargs = captures[0]
    assert argv == [
        supervisor.claude_binary,
        "-p",
        "do work",
        "--settings",
        "/tmp/hooks.json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--disallowedTools",
        "Task",
        "--model",
        "haiku",
        "--max-budget-usd",
        "1.5",
        "--session-id",
        next(value for index, value in enumerate(argv) if argv[index - 1] == "--session-id"),
    ]
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert "OPENAI_API_KEY" not in kwargs["env"]
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["session_ref"] == "claude-ref"
    assert row["cost_usd"] == pytest.approx(0.125)
    wait_for_file(tmp_path / "ledger" / "sessions" / f"{session_id}.jsonl")


def test_launch_persists_account_id_on_sessions_row(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 durable inflight attribution: _launch writes the selected account's
    id onto sessions.account_id (migration 045), so limit_state can count live
    sessions per account across processes."""
    captures: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        supervisor_module,
        "_resolve_spawn_account",
        lambda: (str(tmp_path / "acct-dir"), {}, "acct_wp2", "WP2 account"),
    )
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures,
            lambda: FakeProcess([{"type": "result", "subtype": "success", "total_cost_usd": 0}]),
        ),
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work")
    wait_for_state(sessions_dal, session_id, "completed")
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["account_id"] == "acct_wp2"


def test_default_claude_binary_is_resolved_once(
    sessions_dal: SessionsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_resolve_claude_cli", lambda: "/opt/claude/bin/claude")
    supervisor = SessionSupervisor(sessions_dal, notifier=lambda _title, _body: None)
    assert supervisor.claude_binary == "/opt/claude/bin/claude"

    explicit = SessionSupervisor(
        sessions_dal,
        claude_binary="/custom/claude",
        notifier=lambda _title, _body: None,
    )
    assert explicit.claude_binary == "/custom/claude"


def test_default_ssh_hosts_are_empty_and_never_inferred_from_inventory(
    sessions_dal: SessionsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory_reads: list[bool] = []

    def inventory_hosts() -> list[str]:
        inventory_reads.append(True)
        return ["inventory@discovered.example"]

    monkeypatch.setattr(ssh_keys, "read_server_inventory_hosts", inventory_hosts)

    default = SessionSupervisor(sessions_dal, notifier=lambda _title, _body: None)
    explicit = SessionSupervisor(
        sessions_dal,
        notifier=lambda _title, _body: None,
        allowed_ssh_hosts=["deploy@prod.example"],
    )

    assert default._allowed_ssh_hosts == ()
    assert explicit._allowed_ssh_hosts == ("deploy@prod.example",)
    assert inventory_reads == []


def test_launch_with_default_ssh_hosts_issues_empty_grant(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_hosts: list[str] | None = None

    def wrap(argv: list[str], _cwd: str, **kwargs: Any) -> list[str]:
        nonlocal observed_hosts
        observed_hosts = ssh_keys.read_ssh_key_grant(str(kwargs["ssh_key_grant_session_id"]))
        return argv

    monkeypatch.setattr(ssh_keys, "read_server_inventory_hosts", lambda: ["inventory@host"])
    monkeypatch.setattr("omniagentos.sessions.supervisor.sandbox.wrap_command", wrap)
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda *_args, **_kwargs: FakeProcess(),
        notifier=lambda _title, _body: None,
    )

    session_id = supervisor.spawn(str(tmp_path), "haiku", "read status")
    wait_for_state(sessions_dal, session_id, "completed")

    assert observed_hosts == []


def test_launch_mints_scoped_ssh_grant_and_terminalization_revokes_it(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, Any] = {}

    def wrap(argv: list[str], _cwd: str, **kwargs: Any) -> list[str]:
        session_id = str(kwargs["ssh_key_grant_session_id"])
        observed["session_id"] = session_id
        observed["hosts"] = ssh_keys.read_ssh_key_grant(session_id)
        observed["grant_existed"] = ssh_keys.ssh_key_grant_path(session_id).exists()
        return argv

    monkeypatch.setattr("omniagentos.sessions.supervisor.sandbox.wrap_command", wrap)
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda *_args, **_kwargs: FakeProcess(),
        notifier=lambda _title, _body: None,
        allowed_ssh_hosts=["deploy@prod.example"],
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "read production status")
    wait_for_state(sessions_dal, session_id, "completed")

    assert observed == {
        "session_id": session_id,
        "hosts": ["deploy@prod.example"],
        "grant_existed": True,
    }
    grant = ssh_keys.ssh_key_grant_path(session_id)
    deadline = time.monotonic() + 3
    while grant.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not grant.exists()


def test_spawn_persists_granted_roots_and_widens_sandbox(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3: spawn freezes the session's granted roots on the row, and _launch reads
    that SAME persisted value back to widen the OS-sandbox write roots."""
    observed: dict[str, Any] = {}

    def wrap(argv: list[str], _cwd: str, **kwargs: Any) -> list[str]:
        observed["extra_write_roots"] = list(kwargs["extra_write_roots"])
        return argv

    monkeypatch.setattr("omniagentos.sessions.supervisor.sandbox.wrap_command", wrap)
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    granted = tmp_path / "granted"
    granted.mkdir()
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda *_a, **_k: FakeProcess(),
        notifier=lambda _t, _b: None,
    )
    session_id = supervisor.spawn(
        str(tmp_path), "haiku", "do scoped work", granted_roots=[str(granted)]
    )
    wait_for_state(sessions_dal, session_id, "completed")

    row = sessions_dal.get_session(session_id)
    assert row is not None
    roots = json.loads(row["granted_roots"])
    # Explicit grant is preserved; AUTO-APPROVE standing roots may also appear.
    assert str(granted) in roots
    assert str(granted) in observed["extra_write_roots"]


def test_spawn_without_granted_roots_still_gets_standing_roots(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTO-APPROVE Phase 1: even with no project grants, standing roots are frozen."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.sandbox.wrap_command", lambda argv, _cwd, **_k: argv
    )
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda *_a, **_k: FakeProcess(),
        notifier=lambda _t, _b: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work")
    wait_for_state(sessions_dal, session_id, "completed")

    row = sessions_dal.get_session(session_id)
    assert row is not None
    roots = json.loads(row["granted_roots"] or "[]")
    assert any("Desktop" in r or "var" in r for r in roots)


def test_spawn_persists_idle_minutes_on_fresh_and_resume_rows(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D8: the idle_minutes kwarg is written at ROW CREATION on both spawn
    shapes (fresh launch and resume_session_ref), so the reaper never sees a
    longhaul session under the global default; omitted, the column stays NULL."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.sandbox.wrap_command", lambda argv, _cwd, **_k: argv
    )
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda *_a, **_k: FakeProcess(),
        notifier=lambda _t, _b: None,
    )

    fresh = supervisor.spawn(str(tmp_path), "haiku", "long build", idle_minutes=45.0)
    wait_for_state(sessions_dal, fresh, "completed")
    row = sessions_dal.get_session(fresh)
    assert row is not None
    assert row["idle_minutes"] == 45.0

    resumed = supervisor.spawn(
        str(tmp_path),
        "haiku",
        "continue the build",
        resume_session_ref="00000000-1111-2222-3333-444444444444",
        idle_minutes=45.0,
    )
    wait_for_state(sessions_dal, resumed, "completed")
    row = sessions_dal.get_session(resumed)
    assert row is not None
    assert row["idle_minutes"] == 45.0

    plain = supervisor.spawn(str(tmp_path), "haiku", "ordinary work")
    wait_for_state(sessions_dal, plain, "completed")
    row = sessions_dal.get_session(plain)
    assert row is not None
    assert row["idle_minutes"] is None

    # F3: bool is an int subclass — a YAML `true` reaching spawn must not
    # persist as a 1.0-minute reap threshold; the guard treats it as unset.
    boolish = supervisor.spawn(str(tmp_path), "haiku", "misconfigured", idle_minutes=True)
    wait_for_state(sessions_dal, boolish, "completed")
    row = sessions_dal.get_session(boolish)
    assert row is not None
    assert row["idle_minutes"] is None


def test_spawn_validation_failure_leaves_visible_failed_session(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _title, _body: None,
    )
    missing = tmp_path / "does-not-exist"

    with pytest.raises(SessionSpawnError, match="does-not-exist") as raised:
        supervisor.spawn(str(missing), "haiku", "do work")

    row = sessions_dal.get_session(raised.value.session_id)
    assert row is not None
    assert row["state"] == "failed"
    assert "does-not-exist" in row["error"]


def test_launch_refuses_when_sandbox_unavailable(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-policy fix5 #4: fail closed and never spawn unconfined."""
    captures: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    monkeypatch.setattr("omniagentos.runner.sandbox.sandbox_available", lambda: False)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, lambda: FakeProcess()),
        notifier=lambda _title, _body: None,
    )
    with pytest.raises(RuntimeError, match="sandbox"):
        supervisor.spawn(str(tmp_path), "haiku", "do work", None, "title")
    assert not captures, "no process may spawn when the sandbox is unavailable"


@pytest.mark.parametrize(
    "decided_by",
    ["runner:x", "session-supervisor", "bot", "automation", "system", "ci-bot", "bot:ci"],
)
def test_consequential_approval_from_automation_never_resumes(
    sessions_dal: SessionsDal, tmp_path: Path, decided_by: str
) -> None:
    """Irreversible (always_human) approvals reject every non-human decider (SEC-002).

    In AUTO mode `irreversible` is the class that hard-stops with always_human, so it
    is the one that must reject automated resumers. The shared
    approval_satisfies_gate is the sole authority for this check, and it rejects
    bot/automation/system/*-bot identities as well as runner-signed and actor-signed
    decisions.
    """
    session_id = seed_session(sessions_dal, tmp_path, state="awaiting_approval")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "irreversible", "risky", "{}", "high", "", None
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - p2 normally performs the decision
        "UPDATE approvals SET state='approved', decided_by=? WHERE id=?",
        (decided_by, approval_id),
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, FakeProcess),
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()
    assert captures == []
    assert sessions_dal.get_session(session_id)["state"] == "failed"  # type: ignore[index]


def test_human_approval_resumes_same_ref_once(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="awaiting_approval")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    store = SqliteStore(str(tmp_path / "sessions.db"))
    assert store.decide_approval(approval_id, "approved", "owner")
    captures: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures,
            lambda: FakeProcess([{"type": "result", "subtype": "success"}]),
        ),
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()
    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1
    argv = captures[0][0]
    # T-DESIGN-005: resume carries NO ambient acceptEdits; the hook one-shot is the
    # sole resume authority.
    assert argv[:5] == [
        supervisor.claude_binary,
        "-p",
        "--resume",
        "11111111-2222-3333-4444-555555555555",
        "The pending action was approved by a human; proceed with that action.",
    ]
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    supervisor.run_once()
    assert len(captures) == 1


def test_approved_after_expiry_refuses_resume(sessions_dal: SessionsDal, tmp_path: Path) -> None:
    """T-CODE-002 / F1: late approval on an already-expired row must not resume.

    AUTO-APPROVE 5.2 re-queues a fresh pending approval instead of failing the
    session, so work is not shredded — but the late stamp on the expired row
    still cannot authorize resume.
    """
    session_id = seed_session(sessions_dal, tmp_path, state="awaiting_approval")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", "2000-01-01T00:00:00Z"
    )
    # Simulate a late human decision landing on an already-expired row.
    sessions_dal._connection.execute(  # noqa: SLF001 - arrange the late-approval boundary
        "UPDATE approvals SET state='approved', decided_by='owner' WHERE id=?", (approval_id,)
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, FakeProcess),
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()
    assert captures == []
    # Re-queue keeps the session parked awaiting the fresh approval (not failed).
    state = sessions_dal.get_session(session_id)["state"]  # type: ignore[index]
    assert state in {"awaiting_approval", "failed"}


def test_rejected_approval_cancels_without_resume(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="awaiting_approval")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "", "", None
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - p2 normally decides
        "UPDATE approvals SET state='rejected', decided_by='owner' WHERE id=?", (approval_id,)
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(captures, FakeProcess),
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()
    assert captures == []
    assert sessions_dal.get_session(session_id)["state"] == "cancelled"  # type: ignore[index]


def test_kill_terminates_real_process_group_and_voids_pending(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    processes: list[subprocess.Popen[str]] = []

    def launch_sleep(_argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=launch_sleep,
        kill_grace=0.2,
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "sleep")
    sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "", "", None
    )
    assert sessions_dal.request_kill(session_id)
    supervisor.run_once()
    assert processes[0].poll() is not None
    assert sessions_dal.get_session(session_id)["state"] == "killed"  # type: ignore[index]
    assert sessions_dal.get_latest_session_approval(session_id)["state"] == "expired"  # type: ignore[index]


def test_cancel_terminates_process_and_marks_cancelled(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    processes: list[subprocess.Popen[str]] = []

    def launch_sleep(_argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=launch_sleep,
        kill_grace=0.2,
        notifier=lambda _title, _body: None,
    )
    session_id = supervisor.spawn(str(tmp_path), "haiku", "sleep")
    wait_for_state(sessions_dal, session_id, "running")

    assert sessions_dal.request_cancel(session_id)
    assert sessions_dal.request_cancel(session_id)  # idempotent before polling
    supervisor.run_once()

    assert processes[0].poll() is not None
    assert sessions_dal.get_session(session_id)["state"] == "cancelled"  # type: ignore[index]
    assert sessions_dal.request_cancel(session_id)  # idempotent after completion


@pytest.mark.parametrize(
    ("state", "pid", "expected"),
    [("running", 99999, "killed"), ("starting", None, "failed")],
)
def test_reconcile_dead_nonterminal_without_pending_approval_terminalizes(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    state: str,
    pid: int | None,
    expected: str,
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state=state, pid=pid)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        notifier=lambda _title, _body: None,
    )
    supervisor.reconcile()
    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["state"] == expected
    assert session["killed_by"] == "reconcile"
    supervisor.reconcile()  # the next terminal-row sweep writes the durable manifest
    assert (tmp_path / "ledger" / "sessions" / f"{session_id}.jsonl").exists()


@pytest.mark.parametrize(
    ("state", "pid"),
    [("running", 99999), ("starting", None)],
)
def test_reconcile_dead_session_with_pending_approval_parks_once(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    state: str,
    pid: int | None,
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state=state, pid=pid)
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "irreversible", "risky", "{}", "high", "", None
    )
    pushes: list[tuple[str, str]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        notifier=lambda title, body: pushes.append((title, body)),
    )

    supervisor.reconcile()
    supervisor.reconcile()

    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["state"] == "awaiting_approval"
    assert session["killed_by"] is None
    approval = sessions_dal.get_approval_by_id(approval_id)
    assert approval is not None
    assert approval["state"] == "pending"
    assert pushes == [
        (
            "OmniAgentOS approval required",
            f"Session {session_id} is waiting for approval.",
        )
    ]
    notifications = sessions_dal._connection.execute(  # noqa: SLF001
        "SELECT kind, ref_type, ref_id FROM notifications WHERE ref_id = ?",
        (session_id,),
    ).fetchall()
    assert [tuple(row) for row in notifications] == [("escalation", "session", session_id)]
    assert not (tmp_path / "ledger" / "sessions" / f"{session_id}.jsonl").exists()


def test_normal_parked_session_resumes_after_human_approval(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures,
            lambda: FakeProcess([{"type": "result", "subtype": "success"}]),
        ),
        notifier=lambda _title, _body: None,
    )

    supervisor._mark_awaiting(session_id)  # noqa: SLF001 - exercise the normal park seam
    assert sessions_dal.get_session(session_id)["state"] == "awaiting_approval"  # type: ignore[index]
    store = SqliteStore(str(tmp_path / "sessions.db"))
    assert store.decide_approval(approval_id, "approved", "owner")
    supervisor._service_approval(session_id)  # noqa: SLF001 - drive approval resume

    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1
    assert captures[0][0][:4] == [
        supervisor.claude_binary,
        "-p",
        "--resume",
        "11111111-2222-3333-4444-555555555555",
    ]


def test_reconcile_parked_session_resumes_after_human_approval(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=99999)
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    captures: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures,
            lambda: FakeProcess([{"type": "result", "subtype": "success"}]),
        ),
        liveness=lambda _pid: False,
        notifier=lambda _title, _body: None,
    )
    supervisor.reconcile()
    assert sessions_dal.get_session(session_id)["state"] == "awaiting_approval"  # type: ignore[index]
    store = SqliteStore(str(tmp_path / "sessions.db"))
    assert store.decide_approval(approval_id, "approved", "owner")

    supervisor._service_approval(session_id)  # noqa: SLF001 - drive the parked resume seam

    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1
    assert captures[0][0][:4] == [
        supervisor.claude_binary,
        "-p",
        "--resume",
        "11111111-2222-3333-4444-555555555555",
    ]


def test_reconcile_live_pid_with_pending_approval_is_adopted(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=4242)
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        notifier=lambda _title, _body: None,
    )

    supervisor.reconcile()

    assert sessions_dal.get_session(session_id)["state"] == "running"  # type: ignore[index]
    assert sessions_dal.get_approval_by_id(approval_id)["state"] == "pending"  # type: ignore[index]
    assert supervisor._adopted == {session_id: 4242}  # noqa: SLF001 - verify adoption


def test_reconcile_queued_spawn_with_pending_approval_awaits_launch(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="starting")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    sessions_dal.enqueue_spawn(
        session_id=session_id,
        project_dir=str(tmp_path),
        model="haiku",
        prompt="go",
        budget_usd_max=None,
        title=None,
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        notifier=lambda _title, _body: None,
    )

    supervisor.reconcile()

    assert sessions_dal.get_session(session_id)["state"] == "starting"  # type: ignore[index]
    assert sessions_dal.get_approval_by_id(approval_id)["state"] == "pending"  # type: ignore[index]
    assert sessions_dal.has_queued_spawn(session_id)


def test_reconcile_parked_session_survives_restart_with_approval_intact(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """AC-13(b): A parked session (awaiting_approval, dead pid) survives reconcile.

    A session in awaiting_approval with a dead pid is parked by design.
    The claude -p process exits when parked, so dead pid is expected.
    The key test is that reconcile does NOT kill such sessions.
    """
    session_id = seed_session(sessions_dal, tmp_path, state="awaiting_approval", pid=99999)
    sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "", "", None
    )
    approval_id = sessions_dal.get_latest_session_approval(session_id)["id"]  # type: ignore[index]

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,  # pid is dead
        notifier=lambda _title, _body: None,
    )
    supervisor.reconcile()

    # Session should still be in awaiting_approval (not killed)
    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["state"] == "awaiting_approval"

    # Approval should still be pending (not expired/voided)
    approval = sessions_dal.get_approval_by_id(str(approval_id))
    assert approval is not None
    assert approval["state"] == "pending"

    # Manifest should NOT have been written (session is not terminal)
    assert not (tmp_path / "ledger" / "sessions" / f"{session_id}.jsonl").exists()


def test_spawn_ingress_launches_queued_session_to_completion(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-OPS-003: module spawn() enqueues; the polling supervisor launches it."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    db = str(tmp_path / "sessions.db")
    session_id = supervisor_module.spawn(str(tmp_path), "haiku", "do work", db_path=db)
    assert sessions_dal.get_session(session_id)["state"] == "starting"  # type: ignore[index]
    assert sessions_dal.has_queued_spawn(session_id)

    captures: list[tuple[list[str], dict[str, Any]]] = []
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=fake_factory(
            captures, lambda: FakeProcess([{"type": "result", "subtype": "success"}])
        ),
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()
    wait_for_state(sessions_dal, session_id, "completed")
    assert len(captures) == 1
    assert captures[0][0][:3] == [supervisor.claude_binary, "-p", "do work"]
    assert not sessions_dal.has_queued_spawn(session_id)


def test_spawn_ingress_then_kill_through_run_once(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-OPS-003: a session spawned via the ingress is killable through the loop."""
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    db = str(tmp_path / "sessions.db")
    processes: list[subprocess.Popen[str]] = []

    def launch_sleep(_argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    session_id = supervisor_module.spawn(str(tmp_path), "haiku", "sleep", db_path=db)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=launch_sleep,
        kill_grace=0.2,
        notifier=lambda _title, _body: None,
    )
    supervisor.run_once()  # drains queue -> launches
    wait_for_state(sessions_dal, session_id, "running")
    assert sessions_dal.request_kill(session_id)
    supervisor.run_once()  # services the kill
    assert processes[0].poll() is not None
    assert sessions_dal.get_session(session_id)["state"] == "killed"  # type: ignore[index]


def test_reconcile_does_not_kill_external_dead_pid(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """T-OPS-004: reconcile never pid-kills report-only external sessions."""
    sessions_dal.create_session(
        {
            "id": "ses_ext",
            "source": "external",
            "project_dir": str(tmp_path),
            "session_ref": "provider-uuid",
            "state": "running",
            "pid": 99999,
            "model": "m",
        }
    )
    approval_id = sessions_dal.create_session_approval(
        "ses_ext", "hash", "consequential", "risky", "{}", "high", "", None
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        notifier=lambda _title, _body: None,
    )
    supervisor.reconcile()
    assert sessions_dal.get_session("ses_ext")["state"] == "running"  # type: ignore[index]
    assert sessions_dal.get_approval_by_id(approval_id)["state"] == "pending"  # type: ignore[index]


@pytest.mark.parametrize("state", ["killed", "failed", "completed"])
def test_reconcile_terminal_session_with_pending_approval_is_unchanged(
    sessions_dal: SessionsDal, tmp_path: Path, state: str
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state=state, pid=99999)
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: False,
        notifier=lambda _title, _body: None,
    )

    supervisor.reconcile()

    assert sessions_dal.get_session(session_id)["state"] == state  # type: ignore[index]
    assert sessions_dal.get_approval_by_id(approval_id)["state"] == "pending"  # type: ignore[index]


def test_reattached_live_pid_reaches_terminal_when_pid_dies(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """T-OPS-001: a re-attached live-pid session reaches a correct terminal state.

    The DB is the restart-safe transport: reconcile adopts the live pid and the
    run_once adopted-pid path terminalizes the session when that pid later dies.
    """
    alive = {"pid": True}
    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=4242)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: alive["pid"],
        notifier=lambda _title, _body: None,
    )
    supervisor.reconcile()  # adopts the live pid
    assert sessions_dal.get_session(session_id)["state"] == "running"  # type: ignore[index]
    alive["pid"] = False
    supervisor.run_once()  # adopted pid dead -> terminalize (no lost session)
    assert sessions_dal.get_session(session_id)["state"] == "failed"  # type: ignore[index]


def test_clean_environment_drops_credentials() -> None:
    env = build_clean_env(
        {
            "PATH": "/bin",
            "HOME": "/tmp/home",
            "OPENAI_API_KEY": "secret",
            "OMNI_SESSION_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "XDG_AUTH": "dummy-xdg-auth",
            "OMNIAGENTOS_BRIDGE_SESSION_AUTH": "dummy-bridge-auth",
            "LC_ALL": "C",
        },
        session_id="ses_test",
    )
    assert env == {
        "PATH": "/bin",
        "HOME": "/tmp/home",
        "LC_ALL": "C",
        "OMNIAGENTOS_BRIDGE_SESSION_ID": "ses_test",
    }


def test_stream_json_argv_always_includes_verbose():
    """Regression guard (live-e2e blocker): claude CLI rejects --print +
    --output-format stream-json unless --verbose is also present. Both the initial
    launch and the resume argv must carry it."""
    import omniagentos.sessions.supervisor as sup

    class _Stub(sup.SessionSupervisor):
        def __init__(self):
            pass

    s = _Stub.__new__(sup.SessionSupervisor)
    s.claude_binary = "claude"
    launch = sup.SessionSupervisor._bridge_launch_argv(s, "ref-1", "haiku", "p", None)
    assert "--verbose" in launch and "stream-json" in launch
    assert launch.index("--verbose") < launch.index("--include-hook-events")


def test_completed_session_harvests_output_text_from_live_transcript(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The reader thread records cost from the stream but never wrote
    sessions.output_text for claude-bridge sessions. _finish must harvest the
    final reply from the LIVE CLI transcript resolved through
    omniagentos.sessions.transcript (account config_dir + slug + session_ref).
    """
    import re

    from omniagentos.sessions.dal import SessionState

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
    )
    session_id = seed_session(sessions_dal, tmp_path)
    config_dir = tmp_path / "claude-account-4"
    sessions_dal._connection.execute(
        "INSERT INTO claude_accounts (id, label, auth_type, config_dir, created_at, updated_at) "
        "VALUES ('acct_x', 'X', 'config_dir', ?, 'now', 'now')",
        (str(config_dir),),
    )
    sessions_dal._connection.commit()
    sessions_dal.set_account_id(session_id, "acct_x")
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path))
    # seed_session's session_ref is the shared fixture UUID below.
    transcript = config_dir / "projects" / slug / "11111111-2222-3333-4444-555555555555.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "SOLO-OK"}]},
                }
            )
            + "\n"
        )
        handle.write(json.dumps({"type": "result", "result": "SOLO-OK"}) + "\n")

    supervisor._finish(session_id, SessionState.COMPLETED)

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["output_text"] == "SOLO-OK"


def test_output_harvest_preserves_existing_output_text(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """Provider sessions persist output_text at terminalization; the harvest
    must never overwrite an already-recorded reply."""
    from omniagentos.sessions.dal import SessionState

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
    )
    session_id = seed_session(sessions_dal, tmp_path)
    sessions_dal.set_session_output(session_id, "already recorded")

    supervisor._finish(session_id, SessionState.COMPLETED)

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["output_text"] == "already recorded"
