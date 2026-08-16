from __future__ import annotations

import json
from collections.abc import Callable

from omniagentos.adapters.codex import CodexAdapter
from omniagentos.contracts import AgentInput, ResultStatus

from .conftest import FakePopen


def codex_events(text: str = "hello") -> str:
    return "\n".join(
        json.dumps(event)
        for event in [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 30,
                    "cached_input_tokens": 4,
                    "output_tokens": 8,
                    "reasoning_output_tokens": 2,
                },
            },
        ]
    )


def test_codex_usage_and_sandbox_argv(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(codex_events(), "", 0, False)]
    result = CodexAdapter().run(input_factory())

    assert result.status is ResultStatus.OK
    assert result.session_ref == "codex-thread"
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 8
    assert result.usage.cost_usd is None
    assert result.usage.estimated is True
    assert result.usage.source == "mixed"
    command = fake_popen.commands[0]
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_codex_inner_sandbox_rewritten_under_outer_wrap(
    monkeypatch, fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """When the outer Seatbelt wrap engages, codex's own --sandbox is rewritten
    to danger-full-access (nested Seatbelt is impossible; the outer profile is
    the enforcement layer). When the wrap is unavailable (conftest default,
    covered by test_codex_usage_and_sandbox_argv) the original value stays."""
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_available", lambda argv, workspace_dir: True
    )
    fake_popen.queued = [(codex_events(), "", 0, False)]
    CodexAdapter().run(input_factory())
    command = fake_popen.commands[0]
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert "read-only" not in command


def _capture_wrap(monkeypatch, calls: list[dict[str, object]]) -> None:
    """Re-patch the conftest no-op wrap with a kwargs-capturing no-op, and force
    the wrap-engaged state (wrap_available True)."""

    def capture(command: list[str], workspace_dir: str, **kwargs: object) -> list[str]:
        calls.append({"workspace_dir": workspace_dir, **kwargs})
        return command

    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_command", capture)
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_available", lambda argv, workspace_dir: True
    )


def test_codex_read_only_request_keeps_outer_workspace_unwritable(
    monkeypatch, fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """F2 (reviewer write-capability): a read_only request under the engaged
    outer wrap — where the inner sandbox gets disabled — must NOT grant the
    workspace as an outer write root, and project-granted extra dirs (write
    grants too) are dropped; only the CLI state/config roots stay writable."""
    calls: list[dict[str, object]] = []
    _capture_wrap(monkeypatch, calls)
    fake_popen.queued = [(codex_events(), "", 0, False)]
    CodexAdapter().run(
        input_factory(metadata={"extra_dirs": ["/drive/Granted"]})  # level: read_only
    )
    # Inner sandbox disabled (wrap engaged) — the outer layer is now the only
    # sandbox, so it MUST be the read-only one.
    command = fake_popen.commands[0]
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert calls[0]["workspace_writable"] is False
    assert "/drive/Granted" not in (calls[0]["extra_write_roots"] or [])


def test_codex_workspace_write_request_keeps_outer_workspace_root(
    monkeypatch, fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    calls: list[dict[str, object]] = []
    _capture_wrap(monkeypatch, calls)
    fake_popen.queued = [(codex_events(), "", 0, False)]
    CodexAdapter().run(
        input_factory(
            metadata={
                "sandbox": {"level": "workspace_write"},
                "extra_dirs": ["/drive/Granted"],
            }
        )
    )
    assert calls[0]["workspace_writable"] is True
    assert "/drive/Granted" in calls[0]["extra_write_roots"]


def test_codex_add_dir_for_extra_dirs_is_unconditional(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # codex's own --add-dir declares dirs "writable alongside the primary
    # workspace" regardless of --sandbox value (like -C), so this fires even
    # under the default read_only sandbox in input_factory().
    fake_popen.queued = [(codex_events(), "", 0, False)]
    input = input_factory(
        metadata={"extra_dirs": ["/vault/CopywritingBrainVault", "/drive/OmniAgent"]}
    )
    CodexAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert cmd[-7:] == [
        "-C",
        ".",
        "--add-dir",
        "/vault/CopywritingBrainVault",
        "--add-dir",
        "/drive/OmniAgent",
        "-",
    ]


def test_codex_omits_add_dir_when_no_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(codex_events(), "", 0, False)]
    CodexAdapter().run(input_factory())
    cmd = fake_popen.commands[0]
    assert cmd[-3:] == ["-C", ".", "-"]
    assert "--add-dir" not in cmd


def test_codex_workspace_write_and_fresh_structured_repair(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (codex_events("bad"), "", 0, False),
        (codex_events('{"ok": true}'), "", 0, False),
    ]
    input = input_factory(
        metadata={"sandbox": {"level": "workspace_write"}}, output_schema={"required": ["ok"]}
    )
    result = CodexAdapter().run(input)

    assert result.status is ResultStatus.OK
    assert (
        fake_popen.commands[0][fake_popen.commands[0].index("--sandbox") + 1] == "workspace-write"
    )
    # 1bf41f55 enabled Codex native resume. A structured-output repair keeps
    # the originating thread so the CLI can correct its prior invalid response.
    assert fake_popen.commands[1][2:4] == ["resume", "codex-thread"]
    assert "response is not valid JSON" in fake_popen.prompts[1]
