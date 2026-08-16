"""Mode flag for the MCP bridge (off → shadow → enforce).

``OMNIAGENTOS_MCP_BRIDGE_MODE`` defaults to ``off`` so behavior is byte-identical
to pre-bridge code until an operator promotes the flag.
"""

from __future__ import annotations

import os
from typing import Literal

__all__ = [
    "MCP_BRIDGE_ENV",
    "MCP_BRIDGE_MODES",
    "McpBridgeMode",
    "mcp_bridge_enabled",
    "mcp_bridge_enforcing",
    "mcp_bridge_mode",
]

McpBridgeMode = Literal["off", "shadow", "enforce"]
MCP_BRIDGE_MODES: tuple[McpBridgeMode, ...] = ("off", "shadow", "enforce")
MCP_BRIDGE_ENV = "OMNIAGENTOS_MCP_BRIDGE_MODE"
DEFAULT_MCP_BRIDGE_MODE: McpBridgeMode = "off"


def mcp_bridge_mode(env: dict[str, str] | None = None) -> McpBridgeMode:
    source = env if env is not None else os.environ
    raw = str(source.get(MCP_BRIDGE_ENV, DEFAULT_MCP_BRIDGE_MODE)).strip().lower()
    if raw in MCP_BRIDGE_MODES:
        return raw  # type: ignore[return-value]
    return DEFAULT_MCP_BRIDGE_MODE


def mcp_bridge_enabled(env: dict[str, str] | None = None) -> bool:
    return mcp_bridge_mode(env) != "off"


def mcp_bridge_enforcing(env: dict[str, str] | None = None) -> bool:
    return mcp_bridge_mode(env) == "enforce"
