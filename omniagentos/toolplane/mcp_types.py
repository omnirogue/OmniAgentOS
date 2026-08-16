"""Shared MCP request/response/descriptor types for the toolplane bridge.

Dependency-free dataclasses consumed by both the in-process MCP client shim
and the MCP server. Keep this surface stable — no third-party MCP SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "McpCallResult",
    "McpDenial",
    "McpToolDescriptor",
]


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """One listable MCP tool."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk: str = "low"
    source: str = "builtin"


@dataclass(frozen=True, slots=True)
class McpDenial:
    """Structured policy denial — never silent success."""

    tool: str
    reason: str
    code: str = "policy_denied"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.code,
            "detail": self.reason,
            "tool": self.tool,
        }


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """Result of one MCP tool invocation."""

    ok: bool
    content: Any = None
    error: str | None = None
    detail: str | None = None
    denial: McpDenial | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.denial is not None:
            return self.denial.as_dict()
        payload: dict[str, Any] = {"ok": self.ok}
        if self.content is not None:
            payload["content"] = self.content
        if self.error is not None:
            payload["error"] = self.error
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload
