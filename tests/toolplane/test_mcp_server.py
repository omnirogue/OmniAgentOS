"""Tests for the in-process MCP server (skills + memory)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from omniagentos.toolplane.exposure import ExposureDecision
from omniagentos.toolplane.mcp_mode import MCP_BRIDGE_ENV
from omniagentos.toolplane.mcp_server import MEMORY_WRITE_ENV, McpServer

_ALL_SERVER = (
    "skills_list",
    "skills_get",
    "memory_list",
    "memory_read",
    "memory_search",
    "memory_write",
)


@dataclass
class _Turn:
    seq: int
    role: str
    content: str
    created_at: str = "2026-01-01T00:00:00Z"
    model: str | None = None
    meta: dict | None = None


class _FakeMemory:
    def __init__(self) -> None:
        self.turns: list[_Turn] = [
            _Turn(0, "user", "hello world"),
            _Turn(1, "assistant", "hi there"),
        ]
        self.wrote: list[tuple[str, str, str, str]] = []

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[_Turn]:
        return self.turns[:limit]

    def rolling_summary(self, scope_type: str, scope_id: str) -> str | None:
        return "summary for " + scope_id

    def append_turn(
        self, scope_type: str, scope_id: str, role: str, content: str, **kwargs: Any
    ) -> _Turn:
        turn = _Turn(len(self.turns), role, content)
        self.turns.append(turn)
        self.wrote.append((scope_type, scope_id, role, content))
        return turn


def test_flag_off_yields_inert_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "off")
    server = McpServer(env=dict(os.environ), grants=_ALL_SERVER)
    assert server.list_tools() == []
    result = server.call_tool("skills_list", {})
    assert result.ok is False
    assert result.error == "bridge_off"


def test_empty_grants_deny_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLOCKER regression: empty grants expose no skills/memory tools."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    server = McpServer(env=dict(os.environ), grants=())
    assert server.list_tools() == []
    denied = server.call_tool("skills_list", {})
    assert denied.ok is False
    assert denied.denial is not None
    assert denied.error == denied.denial.code
    mem = server.call_tool(
        "memory_list",
        {"scope_type": "session", "scope_id": "ses_x", "limit": 5},
    )
    assert mem.ok is False
    assert mem.denial is not None


def test_skills_enumerate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    server = McpServer(
        env=dict(os.environ),
        grants=_ALL_SERVER,
        skills_list_fn=lambda: [{"id": "sk_1", "slug": "demo"}],
    )
    tools = server.list_tools()
    assert any(t.name == "skills_list" for t in tools)
    result = server.call_tool("skills_list", {"limit": 10})
    assert result.ok is True
    assert result.content["skills"][0]["slug"] == "demo"


def test_memory_read_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    mem = _FakeMemory()
    server = McpServer(env=dict(os.environ), grants=_ALL_SERVER, memory_store=mem)
    listed = server.call_tool(
        "memory_list",
        {"scope_type": "session", "scope_id": "ses_1", "limit": 5},
    )
    assert listed.ok is True
    assert len(listed.content["turns"]) == 2
    summary = server.call_tool(
        "memory_read",
        {"scope_type": "session", "scope_id": "ses_1"},
    )
    assert summary.ok is True
    assert "ses_1" in (summary.content["summary"] or "")
    search = server.call_tool(
        "memory_search",
        {"scope_type": "session", "scope_id": "ses_1", "query": "hello"},
    )
    assert search.ok is True
    assert search.content["matches"]


def test_policy_denied_absent_from_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    server = McpServer(
        env=dict(os.environ),
        grants=("skills_list",),  # memory tools not granted
    )
    names = {t.name for t in server.list_tools()}
    assert names == {"skills_list"}
    denied = server.call_tool("memory_list", {"scope_type": "s", "scope_id": "1"})
    assert denied.ok is False
    assert denied.denial is not None


def test_exposure_decision_is_enforced_for_list_and_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant cannot reintroduce a capability hidden by compute_exposure."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    monkeypatch.setattr(
        "omniagentos.toolplane.mcp_server.compute_exposure",
        lambda *args, **kwargs: ExposureDecision(
            core_tools=(),
            allowed=(),
            deferred=(),
            hidden=("skills_list",),
            reason="test-hidden",
            mode="enforce",
            bypassed=False,
            fallback=False,
            estimated_tokens=0,
        ),
    )
    server = McpServer(env=dict(os.environ), grants=("skills_list",))
    assert server.list_tools() == []
    denied = server.call_tool("skills_list", {})
    assert denied.ok is False
    assert denied.denial is not None


def test_memory_write_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    monkeypatch.setenv(MEMORY_WRITE_ENV, "off")
    mem = _FakeMemory()
    server = McpServer(env=dict(os.environ), grants=_ALL_SERVER, memory_store=mem)
    blocked = server.call_tool(
        "memory_write",
        {"scope_type": "session", "scope_id": "ses_1", "content": "x"},
    )
    assert blocked.ok is False
    assert blocked.error == "memory_write_off"

    monkeypatch.setenv(MEMORY_WRITE_ENV, "on")
    server = McpServer(env=dict(os.environ), grants=_ALL_SERVER, memory_store=mem)
    ok = server.call_tool(
        "memory_write",
        {"scope_type": "session", "scope_id": "ses_1", "content": "x", "role": "system"},
    )
    assert ok.ok is True
    assert mem.wrote
