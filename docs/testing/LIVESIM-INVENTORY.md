# LiveSim feature inventory — working features → tests

Inventory of the **currently working** OmniAgentOS features (verified live on
2026-08-06), each mapped to the LiveSim category that covers it. Status is what
was observed on the live system, not what the code claims.

Legend: ✅ working · ⚠️ working-with-known-defect · ⏸️ paused/disabled · 🔎 observational-only

| # | Feature / subsystem | Status | Evidence (live, 2026-08-06) | LiveSim category |
|---|---|---|---|---|
| 1 | **API on :8485** (FastAPI) | ✅ | `GET /api/health`→200 `{status:ok, db:true, worker.alive:true}` | api_endpoints |
| 2 | Auth on sensitive routes | ✅ | `GET /api/accounts`→401 (July unauth defect now closed) | api_endpoints, security |
| 3 | Dashboard (Next.js :3003) | ⚠️ | shell renders but every `/api/*` fetch 403s "trusted proxy required" → UI functionally down (LS-003) | e2e_live, ui (browser-operator) |
| 4 | Runner / worker heartbeat | ✅ | health `worker.alive:true, last_beat_at` fresh | orchestration |
| 5 | Routines (scheduler tick) | ✅ | launchd `com.omniagentos.routines` loaded; `routine_runs` table live | orchestration |
| 6 | Swarm execution | ✅ | `swarm_runs`/`swarm_attempts` populated; partial-unique live index | orchestration |
| 7 | Approvals | ⚠️ | `GET /api/approvals`→200, but paging never delivers (no SLACK_WEBHOOK_URL) → max-park kills | orchestration, reaper |
| 8 | **MemLife** (candidates/decisions/lessons) | ✅ | `memlife_*` tables live; recall bridge wired 2026-07-29 | memory |
| 9 | Knowledge base + recall | ✅ | `metacog_memory_records/retrieval_events`; recall = data-not-instructions | memory |
| 10 | Vault | ✅ | `vault/` git-versioned notes | memory |
| 11 | Context capsule (brief_digest) | ✅ | `omniagentos/context/capsule.py` deterministic digest | context |
| 12 | Session manifest / handoff | ✅ | `omniagentos/sessions/manifest.py` | context |
| 13 | Toolplane / tool exposure | ✅ | `omniagentos/toolplane/`; session tool catalog | tools_permissions |
| 14 | PreToolUse approval classifier | ⚠️ | `resolve_approval` fail-open on unlisted phrases (AD-15) | tools_permissions, security |
| 15 | Broker (per-run bearer tokens) | ✅ | `connectors/store issue_token`; `agent_capabilities` 0→2 rows | tools_permissions |
| 16 | Skills discovery/injection | ✅ | `skills`/`skill_versions`/`metacog_skill_versions`; name@version labels | skills |
| 17 | CORAL enforce mode | ✅(off) | pointers-only, enforce OFF by default | skills |
| 18 | WorkFS root safety | ✅ | `omniagentos/workfs` TOCTOU-guarded, root-FD relative | files_fs |
| 19 | Scope / path containment | ✅ | `path_containment.inode_paths_equal`, `omniagentos/scope` | files_fs |
| 20 | OS sandbox (SBPL profile) | ✅ | `runner/sandbox.py` confines writes to session roots | files_fs |
| 21 | board_files path denylist | ⚠️ | N-4 denylist admits /private/etc, ~/.ssh, /private/tmp, prod checkout | files_fs, security |
| 22 | DB migrations | ✅ | `schema_migrations` head=118 (append-only; next 119) | database |
| 23 | Live runtime DB | ✅ | `var/runtime/state.sqlite3`; sessions 1524 rows | database |
| 24 | Provider cost/usage recording | ✅ | `provider_call_usage`; cost quality exact\|estimated\|unknown\|mixed | telemetry_cost |
| 25 | Spend caps (code-defined) | ✅ | `loop_budget.py` GLOBAL=200/day, INSTANCE=50, render_probe=10 | telemetry_cost |
| 26 | Loops / LangGraph runtime | ✅ | `loop_reservations`/`loop_settings`; loop credential seam live | orchestration |
| 27 | Event hub (SSE) | ✅ | health `event_hub.state:ok, degraded:false` | degradation, api_endpoints |
| 28 | Slack socket ingestion | ✅ | launchd `com.omniagentos.comms-slack-socket` up | (out of scope; noted) |
| 29 | **liveness-reaper** (session rows) | ✅ | 160 dead-pid rows reaped; two-factor PID-reuse defense | reaper |
| 30 | **A2 session reaper** (idle/max-park) | ⚠️ | 16 max-park kills (approval starvation); enforce armed | reaper |
| 31 | idle-reaper.sh (RAM/CLIs) | ✅ | ARMED=0 report-only verified; heartbeat JSONL | reaper |
| 32 | fleet-reaper.sh (overdue flags) | ✅ | never signals; heartbeat log | reaper |
| 33 | Cheap-LLM probe path | ⚠️ | LiteLLM :4000 DOWN; Claude CLI haiku fallback; Kimi metered PAUSED | degradation, telemetry_cost |

## Categories omitted from the goal's list that LiveSim adds

- **degradation** — dependency-down behaviour (LiteLLM down, Kimi paused) is a
  first-class category, not just an edge case, because two dependencies are
  actually down right now.
- **reaper legitimacy tracking** — the goal asked to test the reaper; LiveSim
  also stands up a durable observer (`reaper_tracker.py`) since the killing signal
  is only durable in the DB, not the rotating logs.
- **security-observational** — the known-open defects (classifier fail-open,
  denylist gaps) are documented as passing observational negatives, not fixed.

## Not covered (explicit gaps for a later session)

- Real end-to-end **spawn of a live bridge session** and watching the A2 reaper
  act on it in enforce mode (would require creating a real session; deferred as
  too invasive for an observational suite — the reaper logic is covered via
  synthetic rows + live-kill evidence instead).
- **Playwright** dashboard interaction flows (click-through) — the UI check is a
  render/health check via browser-operator, not full interaction coverage.
- Comms pollers (Telegram/Slack thread replies), banking/revenue collectors —
  out of the stated scope.
