from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.adapters.claude import ClaudeAdapter, _resolve_claude_cli
from omniagentos.contracts import AgentInput, ResultStatus
from omniagentos.routing import (
    Account,
    AccountPool,
    AccountPoolConfig,
    Outcome,
    ProviderPool,
    reset_default_pool,
)

from .conftest import FakePopen


def claude_envelope(result: str = "hello") -> str:
    return json.dumps(
        {
            "type": "result",
            "result": result,
            "session_id": "claude-session",
            "num_turns": 2,
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }
    )


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def test_claude_cli_resolution_override_preferred_and_bare_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_popen: type[FakePopen],
    input_factory: Callable[..., AgentInput],
) -> None:
    override = tmp_path / "override" / "claude"
    _make_executable(override)
    monkeypatch.setenv("OMNIAGENTOS_CLAUDE_CLI", str(override))

    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    ClaudeAdapter().run(input_factory())
    assert fake_popen.commands[0][0] == str(override)

    monkeypatch.delenv("OMNIAGENTOS_CLAUDE_CLI")
    fake_home = tmp_path / "home"
    preferred = fake_home / ".local" / "bin" / "claude"
    _make_executable(preferred)
    assert _resolve_claude_cli(home=fake_home) == str(preferred)

    empty_home = tmp_path / "empty-home"
    assert _resolve_claude_cli(home=empty_home) == "claude"


def test_claude_usage_and_read_only_argv(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    result = ClaudeAdapter().run(input_factory())

    assert result.status is ResultStatus.OK
    assert result.session_ref == "claude-session"
    assert result.usage.model_dump() | {"wall_ms": result.usage.wall_ms} == {
        "wall_ms": result.usage.wall_ms,
        "turns": 2,
        "input_tokens": 12,
        "output_tokens": 5,
        "cost_usd": 0.0123,
        "estimated": False,
        "source": "cli-report",
    }
    assert "--permission-mode" not in fake_popen.commands[0]
    assert "--add-dir" not in fake_popen.commands[0]


def test_claude_emits_effort_from_metadata(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # Fable routing: --model fable --effort high, carried on metadata.
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    input = input_factory(model="fable", metadata={"effort": "high"})
    ClaudeAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert cmd[cmd.index("--model") + 1] == "fable"
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_claude_omits_effort_when_unset(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    ClaudeAdapter().run(input_factory())
    assert "--effort" not in fake_popen.commands[0]


def test_claude_workspace_write_and_error(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "authentication failed", 1, False)]
    input = input_factory(metadata={"sandbox": {"level": "workspace_write"}}, working_dir="/work")
    result = ClaudeAdapter().run(input)

    assert result.status is ResultStatus.ERROR
    assert result.error == "authentication failed"
    assert fake_popen.commands[0][-4:] == ["--permission-mode", "acceptEdits", "--add-dir", "/work"]


def _errorish_claude_envelope(**overrides: object) -> str:
    """Shape drawn from the live oauth-expiry fixture (exit 0, subtype success).

    sessions/supervisor treats is_error / error-* subtype / terminal_reason
    containing 'error' as a failed result. The adapter must not be weaker: an
    error outcome returned as ResultStatus.OK is the denied-as-COMPLETED class.
    """
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Failed to authenticate: OAuth session expired",
        "session_id": "claude-session",
        "num_turns": 1,
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_claude_exit_zero_oauth_error_envelope_is_not_ok(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """Live fixture: Claude exits 0 with is_error true + terminal_reason api_error.

    A counterfeit that only looks at returncode would map this to OK.
    """
    fake_popen.queued = [(_errorish_claude_envelope(), "", 0, False)]
    result = ClaudeAdapter().run(input_factory())
    assert result.status is ResultStatus.ERROR
    assert "authenticate" in (result.error or "").lower()


def test_claude_api_error_terminal_reason_without_is_error_is_not_ok(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """terminal_reason api_error is a structured failure even when is_error is absent.

    Counterfeit that only checks ``is_error is True`` would still return OK here.
    Supervisor/longhaul already gate on ``"error" in terminal_reason``.
    """
    envelope = json.loads(_errorish_claude_envelope())
    del envelope["is_error"]
    fake_popen.queued = [(json.dumps(envelope), "", 0, False)]
    result = ClaudeAdapter().run(input_factory())
    assert result.status is ResultStatus.ERROR
    assert "authenticate" in (result.error or "").lower()


def test_claude_error_subtype_prefix_is_not_ok(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """subtype error_* must fail closed, not only the exact string \"error\".

    Counterfeit that keeps ``subtype == \"error\"`` exact-match would still OK this.
    """
    envelope = {
        "type": "result",
        "subtype": "error_max_turns",
        "result": "Max turns reached",
        "session_id": "claude-session",
    }
    fake_popen.queued = [(json.dumps(envelope), "", 0, False)]
    result = ClaudeAdapter().run(input_factory())
    assert result.status is ResultStatus.ERROR
    assert "max turns" in (result.error or "").lower()


def test_claude_clean_success_envelope_still_ok(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """Genuine success must not be collateral damage of a broader error gate."""
    fake_popen.queued = [(claude_envelope("all good"), "", 0, False)]
    result = ClaudeAdapter().run(input_factory())
    assert result.status is ResultStatus.OK
    assert result.output_text == "all good"


def test_claude_add_dir_covers_working_dir_and_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # Drive Access for projects (W4): extra_dirs (runner/core.py::_project_extra_dirs)
    # rides alongside working_dir on --add-dir once the run is workspace_write.
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    input = input_factory(
        metadata={
            "sandbox": {"level": "workspace_write"},
            "extra_dirs": ["/vault/CopywritingBrainVault", "/drive/OmniAgent"],
        },
        working_dir="/work",
    )
    ClaudeAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert cmd[-8:] == [
        "--permission-mode",
        "acceptEdits",
        "--add-dir",
        "/work",
        "--add-dir",
        "/vault/CopywritingBrainVault",
        "--add-dir",
        "/drive/OmniAgent",
    ]


def test_claude_omits_extra_dirs_when_read_only(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # No --add-dir at all (for working_dir OR extra_dirs) outside workspace_write,
    # unchanged from pre-W4 behavior.
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    input = input_factory(metadata={"extra_dirs": ["/vault/CopywritingBrainVault"]})
    ClaudeAdapter().run(input)
    assert "--add-dir" not in fake_popen.commands[0]


def test_claude_structured_repair_resumes(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (claude_envelope("not json"), "", 0, False),
        (claude_envelope('{"ok": true}'), "", 0, False),
    ]
    result = ClaudeAdapter().run(input_factory(output_schema={"required": ["ok"]}))

    assert result.status is ResultStatus.OK
    assert result.output_json == {"ok": True}
    assert "--resume" in fake_popen.commands[1]
    assert "response is not valid JSON" in fake_popen.commands[1][2]


def test_claude_structured_repair_fails_after_one_attempt(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (claude_envelope("bad"), "", 0, False),
        (claude_envelope("still bad"), "", 0, False),
    ]
    result = ClaudeAdapter().run(input_factory(output_schema={"required": ["ok"]}))

    assert result.status is ResultStatus.ERROR
    assert len(fake_popen.commands) == 2
    assert result.error is not None and "response is not valid JSON" in result.error


def test_claude_config_dir_survives_the_env_scrub(
    monkeypatch: pytest.MonkeyPatch,
    fake_popen: type[FakePopen],
    input_factory: Callable[..., AgentInput],
) -> None:
    """Defense in depth on the common.py plumbing itself: env_overrides are merged
    into the env AFTER _scrubbed_env() runs, so the account router's
    CLAUDE_CONFIG_DIR injection always survives the scrub. The scrub is now an
    ALLOWLIST (AC-policy money boundary): CLAUDE_CONFIG_DIR is NOT on it, so an
    ambient value is dropped outright -- the strongest possible form of the old
    "even if it were denylisted" invariant. env_overrides re-applies the POOLED
    value on top, so the subprocess sees exactly the router's selection. Exercises
    CliAdapter._run_once/_invoke directly, independent of the account-pool
    orchestration in ClaudeAdapter.run() (covered separately below)."""
    # A stale ambient value the scrub must drop, proving the override -- not the
    # inherited env -- is what reaches the subprocess.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/stale-account")
    fake_popen.queued = [(claude_envelope(), "", 0, False)]

    result = ClaudeAdapter()._run_once(
        input_factory(), env_overrides={"CLAUDE_CONFIG_DIR": "/pooled/account-x"}
    )

    assert result.status is ResultStatus.OK
    assert fake_popen.envs[0]["CLAUDE_CONFIG_DIR"] == "/pooled/account-x"


def test_claude_env_closes_credential_shaped_prefix_admission(
    monkeypatch: pytest.MonkeyPatch,
    fake_popen: type[FakePopen],
    input_factory: Callable[..., AgentInput],
) -> None:
    """Adapter-backed swarm launches inherit the shared prefix closure."""
    monkeypatch.setenv("XDG_AUTH", "dummy-xdg-auth")
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_AUTH", "dummy-bridge-auth")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/config")
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_ID", "abc-def-123")
    fake_popen.queued = [(claude_envelope(), "", 0, False)]

    result = ClaudeAdapter().run(input_factory())

    assert result.status is ResultStatus.OK
    child_env = fake_popen.envs[0]
    assert "XDG_AUTH" not in child_env
    assert "OMNIAGENTOS_BRIDGE_SESSION_AUTH" not in child_env
    assert child_env["XDG_CONFIG_HOME"] == "/tmp/config"
    assert child_env["OMNIAGENTOS_BRIDGE_SESSION_ID"] == "abc-def-123"


class TestAccountPoolIntegration:
    """OMNIAGENTOS_ACCOUNT_POOL=1 wiring: omniagentos/routing/account_pool.py
    composed into ClaudeAdapter.run() (the minimal env_overrides plumbing it
    rides on lives in omniagentos/adapters/common.py, covered above)."""

    def teardown_method(self) -> None:
        reset_default_pool(None)

    @staticmethod
    def _pool(*account_ids: str, cooldown_seconds: int = 60) -> AccountPool:
        accounts = [
            Account(id=account_id, config_dir=f"/fake-home/{account_id}", priority=index)
            for index, account_id in enumerate(account_ids)
        ]
        config = AccountPoolConfig(
            providers={"claude": ProviderPool(cooldown_seconds=cooldown_seconds, accounts=accounts)}
        )
        return AccountPool(config)

    def test_disabled_by_default_no_config_dir_injected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.delenv("OMNIAGENTOS_ACCOUNT_POOL", raising=False)
        # A real ambient CLAUDE_CONFIG_DIR is common (e.g. this very process may
        # already be running under one) -- clear it so the assertion below
        # actually verifies we didn't ADD it, not just that the shell was clean.
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        reset_default_pool(self._pool("account-1"))  # pool configured but flag is off
        fake_popen.queued = [(claude_envelope(), "", 0, False)]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.OK
        assert "CLAUDE_CONFIG_DIR" not in fake_popen.envs[0]

    def test_enabled_but_no_accounts_configured_is_a_no_op(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)  # see note above
        reset_default_pool(self._pool())  # zero accounts configured for "claude"
        fake_popen.queued = [(claude_envelope(), "", 0, False)]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.OK
        assert "CLAUDE_CONFIG_DIR" not in fake_popen.envs[0]

    def test_enabled_injects_the_picked_accounts_config_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        reset_default_pool(self._pool("account-1"))
        fake_popen.queued = [(claude_envelope(), "", 0, False)]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.OK
        assert fake_popen.envs[0]["CLAUDE_CONFIG_DIR"] == "/fake-home/account-1"

    def test_rate_limited_rotates_to_next_account_within_one_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        pool = self._pool("account-1", "account-2")
        reset_default_pool(pool)
        fake_popen.queued = [
            ("", "Error: rate limit exceeded", 1, False),
            (claude_envelope("second account served it"), "", 0, False),
        ]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.OK
        assert result.output_text == "second account served it"
        assert len(fake_popen.commands) == 2
        assert fake_popen.envs[0]["CLAUDE_CONFIG_DIR"] == "/fake-home/account-1"
        assert fake_popen.envs[1]["CLAUDE_CONFIG_DIR"] == "/fake-home/account-2"
        # account-1 is now cooling, but account-2 isn't -- not everything is cooling.
        assert pool.all_cooling("claude") is False

    def test_non_rate_limit_error_does_not_rotate_accounts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        pool = self._pool("account-1", "account-2")
        reset_default_pool(pool)
        fake_popen.queued = [("", "authentication failed", 1, False)]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.ERROR
        assert result.error == "authentication failed"
        # A normal (non-rate-limit) error must NOT rotate accounts -- only one
        # CLI spawn, and neither account was cooled by it.
        assert len(fake_popen.commands) == 1
        assert pool.all_cooling("claude") is False

    def test_every_account_rate_limited_returns_the_last_real_cli_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        pool = self._pool("account-1", "account-2")
        reset_default_pool(pool)
        fake_popen.queued = [
            ("", "Error: rate limit exceeded", 1, False),
            ("", "Error: 429 too many requests", 1, False),
        ]

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.ERROR
        assert result.error == "Error: 429 too many requests"  # the LAST account's authentic error
        assert len(fake_popen.commands) == 2
        assert pool.all_cooling("claude") is True

    def test_call_made_while_pool_already_fully_cooling_spawns_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_popen: type[FakePopen],
        input_factory: Callable[..., AgentInput],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        pool = self._pool("account-1")
        picked = pool.pick("claude")
        assert picked is not None
        pool.report(picked.id, Outcome.RATE_LIMITED)  # pre-cool the only account
        reset_default_pool(pool)
        fake_popen.queued = []  # nothing should ever be popped

        result = ClaudeAdapter().run(input_factory())

        assert result.status is ResultStatus.ERROR
        assert "rate limit" in (result.error or "").lower()
        assert len(fake_popen.commands) == 0  # zero wasted CLI spawns
