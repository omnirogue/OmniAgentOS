# Lane F Phase 1 — five tasks (F1→F5) materialized

Planner degraded to a solo task; this file records the five tasks as executed
verbatim in dependency order with owned_paths / est_minutes / verify_command /
depends_on. No subtasks_request fan-out.

| Id | Title | depends_on | est_minutes | Workbook |
| --- | --- | --- | --- | --- |
| F1 | MCP bridge (client+server+media/workmodes+assess) | — | 55 | F1-mcp-bridge.md |
| F2 | Session memory capture (PreToolUse envelope, no migration) | F1 | 55 | F2-session-memory-capture.md |
| F3 | Lifecycle reconcile + notif cap/retention | F2 | 50 | F3-lifecycle-reconcile.md |
| F4 | Launch-env include + three-db-map | F3 | 40 | F4-launch-env.md |
| F5 | GitHub adapter + correlation | F4 | 45 | F5-github-comms.md |

Flags (all default **off**):
- `OMNIAGENTOS_MCP_BRIDGE_MODE`
- `OMNIAGENTOS_SESSION_MEMORY_CAPTURE_MODE`
- `OMNIAGENTOS_LIFECYCLE_RECONCILE_MODE`
- `OMNIAGENTOS_GITHUB_COMMS_MODE`

No SQLite migration 082 created. exposure.py policy semantics unmodified.
board_sweep stale-card/approval-hang preserved. Frozen comms message shape preserved.
archdocs update needed: F1, F2, F4 (via apply_update only).
