from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omniagentos.contracts import AgentInput, BudgetSpec


class FakePopen:
    """Small Popen stand-in used to keep adapter tests entirely offline."""

    queued: list[tuple[str, str, int, bool]] = []
    commands: list[list[str]] = []
    prompts: list[str | None] = []
    envs: list[dict[str, str]] = []
    instances: dict[int, FakePopen] = {}
    signals: list[int] = []
    next_pid = 9000

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, **_: Any) -> None:
        self.command = command
        self.pid = FakePopen.next_pid
        FakePopen.next_pid += 1
        self.stdout, self.stderr, self.returncode, self.hang = FakePopen.queued.pop(0)
        self.terminated = False
        FakePopen.commands.append(command)
        # Captured so account-pool tests can assert CLAUDE_CONFIG_DIR actually
        # reached the subprocess env (see tests/adapters/test_claude.py and
        # tests/routing/) -- every other existing test ignores this list.
        FakePopen.envs.append(dict(env) if env is not None else {})
        FakePopen.instances[self.pid] = self

    def communicate(self, _: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        FakePopen.prompts.append(_)
        if self.hang and not self.terminated:
            import subprocess

            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        return None if not self.terminated else self.returncode


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[FakePopen]:
    from omniagentos.adapters import common

    FakePopen.queued = []
    FakePopen.commands = []
    FakePopen.prompts = []
    FakePopen.envs = []
    FakePopen.instances = {}
    FakePopen.signals = []
    monkeypatch.setattr(common.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(common.os, "getpgid", lambda pid: pid)
    # These are argv-construction unit tests: no-op the OS sandbox wrap (its own
    # confinement is proved in tests/runner/test_guardrail_ac_policy.py). Without
    # this, wrap_command's live sandbox self-test would spawn via the monkeypatched
    # Popen. The wrap IS applied in production (proved by a dedicated test).
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_command",
        lambda command, workspace_dir, **kwargs: command,
    )
    # These offline argv tests must not depend on the host's real OS sandbox: pin
    # sandbox_available True so the adapter fail-closed floor (refuse a sub-CLI when
    # the sandbox is unavailable, AC-policy fix4 HIGH) does not turn every argv test
    # into a refusal on a non-macOS runner. The floor itself is proved directly in
    # tests/runner/test_guardrail_ac_policy.py.
    monkeypatch.setattr("omniagentos.runner.sandbox.sandbox_available", lambda: True)
    # Default argv tests represent the WRAP-NOT-ENGAGED state (wrap_command is
    # no-op'd above): the inner-sandbox disable for self-sandboxing CLIs must
    # then keep the CLI's own --sandbox pair untouched. Tests that assert the
    # under-wrap argv re-patch this to True explicitly.
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_available",
        lambda argv, workspace_dir: False,
    )

    def killpg(pgid: int, signum: int) -> None:
        FakePopen.signals.append(signum)
        process = FakePopen.instances.get(pgid)
        if process is not None:
            process.terminated = True

    monkeypatch.setattr(common.os, "killpg", killpg)
    return FakePopen


def agent_input(**overrides: Any) -> AgentInput:
    values: dict[str, Any] = {
        "run_id": "run_adapter_test",
        "task_id": "task_adapter_test",
        "prompt": "say hello",
        "working_dir": ".",
        "model": "test-model",
        "budget": BudgetSpec(wall_ms_max=1_000),
        # cli_unattended_elevated: these argv-construction tests represent an
        # allowed/human-elevated run, so a force-auto CLI (kimi) is not refused by
        # the unattended floor before its command is built (AC-policy fix4).
        "metadata": {"sandbox": {"level": "read_only"}, "cli_unattended_elevated": True},
    }
    values.update(overrides)
    return AgentInput(**values)


@pytest.fixture
def input_factory() -> Callable[..., AgentInput]:
    return agent_input
