# F1 — MCP bridge (client + server + media/workmodes reach + assess hook)

## Built
- `omniagentos/toolplane/mcp_types.py` — shared descriptors/results
- `omniagentos/toolplane/mcp_mode.py` — `OMNIAGENTOS_MCP_BRIDGE_MODE` (off/shadow/enforce, default off)
- `omniagentos/toolplane/mcp_client.py` — list/call through `compute_exposure` (exposure.py unmodified)
- `omniagentos/toolplane/mcp_server.py` — skills + memory served in-process; memory write separately gated
- `omniagentos/toolplane/media_bridge.py` — Globex image/video + voice TTS adapters
- `omniagentos/toolplane/workmodes_bridge.py` — `validate_ad_copy` + assess-time checks
- `omniagentos/execution/assess.py` — partial hook extended for workmodes verify (shadow records, enforce fails)

## Policy proof
- Denied tools return `McpDenial` / structured error, never silent success
- Flag-off list/call are inert

## Verify
```
uv run pytest -q tests/toolplane/test_mcp_client.py tests/toolplane/test_mcp_server.py tests/toolplane/test_tool_reach.py
uv run ruff check <touched>
uv run mypy <touched>
```
Demonstrates one exposed MCP tool call + media validation + assess hook.

## archdocs update needed
Yes — toolplane MCP bridge + assess workmodes verify input should be recorded via
`omniagentos.archdocs.update.apply_update` (do not edit ARCHI.md/ARCHI.json directly).

## owned_paths (this task)
- omniagentos/toolplane/mcp_*.py, media_bridge.py, workmodes_bridge.py
- omniagentos/execution/assess.py
- tests/toolplane/test_mcp_client.py, test_mcp_server.py, test_tool_reach.py
- docs/workbooks/team-f/F1-mcp-bridge.md

## est_minutes
55
## depends_on
[]
## verify_command
`uv run pytest -q tests/toolplane/test_mcp_client.py tests/toolplane/test_mcp_server.py tests/toolplane/test_tool_reach.py && uv run ruff check omniagentos/toolplane/mcp_client.py omniagentos/toolplane/mcp_server.py omniagentos/toolplane/media_bridge.py omniagentos/toolplane/workmodes_bridge.py omniagentos/execution/assess.py && uv run mypy omniagentos/toolplane/mcp_client.py omniagentos/toolplane/mcp_server.py omniagentos/toolplane/media_bridge.py omniagentos/toolplane/workmodes_bridge.py omniagentos/execution/assess.py`
