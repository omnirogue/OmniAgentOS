<!-- archdocs:stamp git_head=7f7c64e max_migration=137 route_count=354 generated_at=2026-08-16T11:18:05Z -->
# ARCHI.md — OmniAgentOS architecture map

Compact top map for agents and humans: what exists, where it lives, how to extend it.
Regenerate the machine-verifiable sections with `python -m omniagentos.archdocs.generate`;
narrative sections and the human-notes section below are preserved on regeneration.
Per-domain detail lives in `docs/architecture/*.md` (execution, governance, knowledge,
ui, scheduling, reliability, organization).

## Subsystems

- **Execution** — Fable-conducted task/run/step state machine (runner, orchestrator,
  routing/adapters). Features private git branch worktrees with fencing enforcement for parallel execution, where coordinator-owned files (e.g., `PLAN.md`) are exempt from ownership violations but are automatically reverted from the branch base and committed by the coordinator on the branch (resolving the `sched-scope` stranded branch conflicts) to prevent worker-committed edits from landing or causing conflicts. The `integration/v5` multi-lane pipeline integrates a 3-lineage flow: coder (`grok-4.5`), per-lane reviewer (`gpt-5.6-sol`), and an aggregate verifier (`claude-opus-5` / Anthropic) executing on a merged integration branch to prevent lane conflicts and verify overall cohesion. Includes a T3 backlog executor merging into the open integration batch (never main) that refuses protected branches and root aliases in the batch resolver, and a T4 integration role config + verdict parser with exact verdict matching and lineage fallback. S1+S3 read/retrieval paths are bound by query PLAN (not LIMIT) and byte reads (not lines), scoping shared ledgers to specific runs, enforcing hard ceilings at finalize, and ensuring source claims require actually reading the source to prevent failed reads from masquerading as clean ones. Lane acceptance doctrine is structural rather than ceremonial, enforcing ordered blocks, comparing bytes exactly, and printing the whole witness. Model routing, allow-listing policies, and model intelligence/registry engines (`modelintel`) support OpenRouter-verified lineages (such as `z-ai/glm-5.2` and `moonshotai/kimi-k2.6`) mapped to their authoritative lineages in templates, robustly refreshing rankings from `model-rankings.json` (tracking precise outcomes like `written`, `absent`, `unreadable`, or `wrong-shape` via `RankingsRefresh` to avoid stale constant routing), while the escalation ladder advances models (e.g., to `gemini-3.6-flash`) upon repeated failures. Real activity logs are preferred over synthetic transcripts. Fleet repair and lane contracts enforce strict restoration of dynamically-generated `TASK.md` contracts, CLI legacy argv compatibility, GLA-1 red-first test suites, and GLA rework ladder bindings for evidence SHA tracking. See `docs/architecture/execution.md`.
- **Governance** — ActionClass risk gate, approvals, policy config, cognitive budgets (CBM), ledger.
  See `docs/architecture/governance.md`. A self-defending workspace floor check (`_enforce_workspace_floor`) fails closed on half-set environments and compares home containment by inode to protect the sandbox, safeguarding against differing CLI sandbox profile names (e.g. Grok rejecting Codex profile names). Enforces strict mechanical merge gate safeguards (`merge-gate.sh`) that refuse merges on empty branches, tracked virtual environments (`.venv`), root `WORKBOOK.md` mutations, oracle-path modifications, or when daily `archi-morning` maintenance is run on non-main branches. To prevent merge-gate bypass, the previously unsatisfiable gate check is resolved by configuring `default_gate_workspace()` and routing `merge_candidate` gates (`091` migration) to sign and mint candidate-bound receipts at `var/gate-evidence/records/merge-gate/<candidate-sha>.json`. Features a self-growing, self-pruning quality gate registry based on observed defects, treating the 'docs' gate as a ceiling where any production file edit revokes the docs-only path. Merge operations can proceed when the only refusal is a check that cannot pass under the circumstances. Quality gates systematically measure and block nine recurring defect shapes across three classes uncovered in aggregate reviews: 1) built, tested, never wired (enforced deterministically via `scripts/reachability-gate.py` on the merge path); 2) unknown rendered as a favorable value; and 3) docstring claims stronger than the code (failing closed when claims are verbally asserted but not structurally closed). Reflection integration leverages swarm-verdict harvest adapters, a Fable approval gate shadowing by default, and S0 core nightly hooks. Git branch integrity is maintained with the correction of the overstated '109 unreviewed commits' claim: the bulk are harmless, empty, or PLAN/WORKBOOK-only automation history noise with zero material secret exposure.
- **Knowledge** — skills library, Synapse knowledge graph, memory layer, vaultgraph,
  repomap, filesearch, and metacog automated skill synthesis. See `docs/architecture/knowledge.md`. Features a unified memory recall front-door `recall()` fusing conversation, Postgres-powered Synapse knowledge graph, metacog, and the vault using Reciprocal Rank Fusion (RRF). Adds 5 read-only retrieval tools to the toolplane (`repomap`, `semantic_search`, `knowledge_recall`, `memory_search`, `vault_search`). Automatically synchronizes the repo playbook (`vault/playbook/`) into the runtime vault, indexing files on startup. Gated by default-on `OMNIAGENTOS_KNOWLEDGE` on the launch path.
- **UI** — Next.js mission-control dashboard featuring Chat v2, a Kanban chat dock, a loops/connections/pulse observatory, real-time SSE approvals, session tracking, and a modern ANSI-color terminal follow stream. Under dashboard Band 1, the UI enforces strict fixture honesty (fully decoupled from empty-state fallback fixtures, asserting contract coverage and making tests runnable by reviewers), correct tokenization, and strict conventions RFC compliance (including Rule 5). Deletes dead stylesheets to close the lint ratchet, prunes the dead organization client, repoints live `/chat` links, and witnesses all eight core dashboard symbols. See `docs/architecture/ui.md`.
- **Scheduling** — launchd job templates + installers, routines engine, and the daily `archi-morning` repository-map/diagram regeneration job (which automatically refreshes `ARCHI.md`, `ARCHI.json`, and the system map diagrams daily at 05:30, executing a diff-guarded local auto-commit with the commit subject `archi-morning: refresh map + diagram` to keep the workspace and diagrams perfectly aligned with the live codebase). See `docs/architecture/scheduling.md`. The launchd routines tick is pointed at the product runtime (`var/runtime`), sourcing `launch-env.sh` to align its database path (`var/runtime/state.sqlite3`) and environment with the API. Built-in routines (e.g. `improve-dispatcher`, `lab-jobs-drain`) are seeded on API startup.
- **Reliability** — V2 self-improving failure-detection, recovery, judge, and pipeline system. See `docs/architecture/reliability.md`. Features a deterministic swarm simulation harness with 20 scenarios (no LLM, no network) for testing boundaries offline. Crash recovery is reachable and bound to the state database. Resolves idle API CPU/SQLite handle leaks (O-15).
- **Organization** — V2 agent org hierarchy (CTO/VPs/managers/specialists/integrators/judges), ensuring the integration task card is assigned to the 'integrator' role rather than 'reviewer'. Removes legacy `company/` and `novel/` shims in favor of `-m home` execution in `orgdims` on Tier-S paths, with privatized design helpers ensuring reachability gate compliance, restored CLI `--db` argv handling, and subprocess E1 witnesses. See `docs/architecture/organization.md`.
- **Longhaul** — long-horizon coding lane: durable board task + executor attempt
  chain, usage-limit account handoff (cooldown, never disable) with robust account status/auth state preservation (where late cooldown writes and expired-cooldown sweeps explicitly preserve authentication errors and operator-disabled states on `claude_accounts` rather than overriding them to healthy), task-level steering with a finish-refusal guarantee, per-category WIP serialization, registry-ranked worker routing, fail-closed completion review. See `docs/architecture/longhaul.md`.

## Entry points

- API: `omniagentos.api:app` (FastAPI, uvicorn), unified on `127.0.0.1:8485` (Grok default, sibling uses `:8484`). Pins `CLAUDE_CONFIG_DIR` to avoid profile inheritance, seeds routines and indexes the playbook at startup, and exposes both tree and flat (`GET /api/skills`) registries.
- Runner: `python -m omniagentos.runner` (or `scripts/launch-omniagentos.sh`), polling
  worker executing persisted step plans.
- Dashboard: Next.js app under `dashboard/` (Chat v2 and Kanban dock), `npm run dev`/`npm run start`, port 3003.
- CLI entry points: `python -m omniagentos.<package>` for repomap, filesearch, modelintel rankings refresh, reliability (§11), skills curator, steward alerts/briefing/comms/metrics, doc-diet classifier (`scripts/doc-diet/classify.py`), and `archdocs` map/diagram generator (`python -m omniagentos.archdocs.diagram`).
- Mechanical Merge Gate & Requests: `scripts/merge-gate.sh` is the mandatory pre-merge safety gate verifying signed gate evidence and candidate-bound receipts, preventing leak of secrets, committed symlinks, modified database migrations, ruff regressions, empty branches, tracked virtual environments (`.venv`), root `WORKBOOK.md` edits, oracle-path modifications, or non-main branch execution of `archi-morning`. The companion `scripts/merge-request.sh` manages cross-session merge requests (`submit <branch>` and `poll`), enforcing zero self-certification and ensuring nothing is merged without passing the gate.
- Integration Seam: `scripts/integrate.sh` reads lane-level verdicts (`var/swarm/sol-verdicts/*.md`), conducts conflict forecasting across changed paths, lands approved lanes, merges to an integration branch (`integration/reviewed`), and dispatches the aggregate verifier (`scripts/dispatch-verifier.sh`).
- Morning maintenance: `scripts/archi-morning/archi-morning.sh` (com.omniagentos.archi-morning), a daily 05:30 job that executes a read-only repository scan to regenerate the repository inventories (updating `ARCHI.md` and `ARCHI.json` machine truth), regenerates and restamps the mermaid system map and diagram (`docs/architecture/system-map.mmd` and `.md`), and auto-commits the updated doc set with a diff-guarded `archi-morning: refresh map + diagram` commit (mirroring the split-decision pattern to prevent sweeping unrelated changes) to keep the architecture map completely fresh. A recent run verified its successful execution, ensuring maps and diagrams stay aligned with actual repository state.
- Document contracts: Global guidelines in `AGENTS.md` and companion sheets (`ARCHITECTURE.md`, `DECISIONS.md`, `TESTING.md`, `CLAUDE.md`).
- Swarm workspace: Local step runs are bound by a dynamically-generated `TASK.md` (contract) and logged continuously inside `WORKBOOK.md` in `var/swarm/<run_id>/<task_id>/`. Orchestrator logic can be verified using the 20-scenario deterministic swarm simulation harness.

## Database — table groups (by migration)

<!-- generated:begin:migrations -->
- `001_init.sql` (v1)
- `002_events_target.sql` (v2)
- `003_lab.sql` (v3)
- `004_collab.sql` (v4)
- `005_capabilities.sql` (v5)
- `006_broker_tokens.sql` (v6)
- `007_steward.sql` (v7)
- `008_steward_fixes.sql` (v8)
- `009_revenue_facts.sql` (v9)
- `010_approvals_session_id.sql` (v10)
- `011_sessions.sql` (v11)
- `012_session_oneshot.sql` (v12)
- `013_session_ops.sql` (v13)
- `014_projects.sql` (v14)
- `015_routines.sql` (v15)
- `016_board_task_run.sql` (v16)
- `020_banking.sql` (v20)
- `021_banking_credit.sql` (v21)
- `022_routines_last_fired.sql` (v22)
- `023_provision.sql` (v23)
- `030_notifications.sql` (v30)
- `031_project_hierarchy.sql` (v31)
- `032_skill_library.sql` (v32)
- `033_session_messages.sql` (v33)
- `034_session_todos.sql` (v34)
- `035_board_archive.sql` (v35)
- `036_claude_accounts.sql` (v36)
- `037_account_rotation_seq.sql` (v37)
- `038_session_granted_roots.sql` (v38)
- `039_orchestrations.sql` (v39)
- `040_session_error.sql` (v40)
- `041_session_prompt.sql` (v41)
- `042_reliability_company.sql` (v42)
- `043_longhaul.sql` (v43)
- `044_orch_resume.sql` (v44)
- `045_swarm.sql` (v45)
- `046_account_reservations.sql` (v46)
- `047_provider_exec.sql` (v47)
- `048_account_pause.sql` (v48)
- `049_effort_telemetry.sql` (v49)
- `050_notification_kinds.sql` (v50)
- `051_swarm_run_params.sql` (v51)
- `052_swarm_lease_generation.sql` (v52)
- `053_reliability_event_alerted.sql` (v53)
- `054_dispatch_gate.sql` (v54)
- `055_sessions_created_index.sql` (v55)
- `057_repomap_cache.sql` (v57)
- `058_execution_contract.sql` (v58)
- `059_scope_locks.sql` (v59)
- `060_metacog.sql` (v60)
- `061_org_dimensions.sql` (v61)
- `062_graph_runtime.sql` (v62)
- `063_cognitive_budget.sql` (v63)
- `064_projects_kind.sql` (v64)
- `065_formation_selections.sql` (v65)
- `066_csi_runs.sql` (v66)
- `067_csi_approval.sql` (v67)
- `068_migration_checksums.sql` (v68)
- `069_projects_kind_index_normalization.sql` (v69)
- `070_portfolio_board_indexes.sql` (v70)
- `071_hot_lookup_indexes.sql` (v71)
- `072_longhaul_provider_harnesses.sql` (v72)
- `073_chat_workspaces.sql` (v73)
- `074_preflight.sql` (v74)
- `076_reflection.sql` (v76)
- `077_reflection_reconcile.sql` (v77)
- `078_formation_selections_finished_at.sql` (v78)
- `079_task_shape_decisions.sql` (v79)
- `080_gate_evidence.sql` (v80)
- `081_insession_grants.sql` (v81)
- `082_lab_verdict_provenance.sql` (v82)
- `083_improve.sql` (v83)
- `084_pulse_series.sql` (v84)
- `085_lab_jobs.sql` (v85)
- `086_control_plane.sql` (v86)
- `087_board_project_scope.sql` (v87)
- `088_chat_folders.sql` (v88)
- `089_sessions_cost_usd_nullable.sql` (v89)
- `090_memlife.sql` (v90)
- `091_routines_merge_candidate_gate.sql` (v91)
- `092_jira_project_key.sql` (v92)
- `093_routines_meta.sql` (v93)
- `094_provider_call_usage.sql` (v94)
- `095_waku_kimi_core.sql` (v95)
- `096_dag_moe_gating.sql` (v96)
- `097_routine_revision.sql` (v97)
- `098_company_goals.sql` (v98)
- `099_transcript_uploads.sql` (v99)
- `100_plans_durable.sql` (v100)
- `101_provenance_source.sql` (v101)
- `102_provenance_backfill_unknown.sql` (v102)
- `103_run_outcome_class.sql` (v103)
- `104_routine_run_self_report.sql` (v104)
- `105_request_attribution.sql` (v105)
- `106_grant_lifecycle_columns.sql` (v106)
- `107_broker_audit_spine.sql` (v107)
- `108_loop_broker_grants.sql` (v108)
- `109_skill_status_and_selection_fields.sql` (v109)
- `110_skill_version_content_digest.sql` (v110)
- `111_secret_catalog.sql` (v111)
- `112_capability_decisions.sql` (v112)
- `113_capability_requests.sql` (v113)
- `114_swarm_attempts_verdict_hash.sql` (v114)
- `115_grant_project_binding.sql` (v115)
- `116_routine_project_scope.sql` (v116)
- `117_account_weekly_quota.sql` (v117)
- `118_session_cost_estimate.sql` (v118)
- `119_routines_total_cost_nullable.sql` (v119)
- `120_routine_runs_cost_usd_nullable.sql` (v120)
- `121_session_approval_delivery.sql` (v121)
- `122_run_priority.sql` (v122)
- `123_team_work_os.sql` (v123)
- `124_orch_step_blocked_on_review.sql` (v124)
- `125_rediscovery_event_traces.sql` (v125)
- `126_provisioning_latency_events.sql` (v126)
- `127_northstar_phase_baselines.sql` (v127)
- `128_allocation_frontier_reports.sql` (v128)
- `129_swarm_attempt_tool_set_digest.sql` (v129)
- `130_edc_decisions.sql` (v130)
- `131_skill_usage.sql` (v131)
- `132_developer_accountability.sql` (v132)
- `133_daily_automation_slots.sql` (v133)
- `134_goal_loops.sql` (v134)
- `135_goal_readings.sql` (v135)
- `136_sessions_agent_view.sql` (v136)
- `137_session_attention.sql` (v137)

**126 migrations, current version 137.**
<!-- generated:end:migrations -->


## Scheduling — launchd jobs

<!-- generated:begin:launchd -->
- `com.omniagentos.agent-observability` — every 1800s
- `com.omniagentos.banking` — 2:00
- `com.omniagentos.banking.hourly` — every 3600s
- `com.omniagentos.estate-check` — 7:35
- `com.omniagentos.loop-audit-collect` — 7:30
- `com.omniagentos.prototype-autocommit` — every 1800s
- `com.omniagentos.revenue` — 2:00
- `com.omniagentos.revenue.hourly` — every 3600s
- `com.omniagentos.wis-census` — 7:20
- `com.omniagentos.wis-knowledge-index` — every 900s
- `com.omniagentos.wis-report` — 8:00
- `com.omniagentos.wis-retention` — 7:55
- `com.omniagentos.wis-watcher` — every 300s
<!-- generated:end:launchd -->


## Repository layout — top-level packages

<!-- generated:begin:packages -->
- `omniagentos/accounts/`
- `omniagentos/adapters/`
- `omniagentos/agentless/`
- `omniagentos/allocation/`
- `omniagentos/api/`
- `omniagentos/archdocs/`
- `omniagentos/audit/`
- `omniagentos/banking/`
- `omniagentos/brandpacks/`
- `omniagentos/briefing/`
- `omniagentos/budget/`
- `omniagentos/cbm/`
- `omniagentos/chats/`
- `omniagentos/collab/`
- `omniagentos/comms/`
- `omniagentos/company_goals/`
- `omniagentos/compute/`
- `omniagentos/connectors/`
- `omniagentos/context/`
- `omniagentos/conversations/`
- `omniagentos/csi/`
- `omniagentos/db/`
- `omniagentos/deploy/`
- `omniagentos/dispatch/`
- `omniagentos/edc/`
- `omniagentos/employee_transcripts/`
- `omniagentos/execution/`
- `omniagentos/fanin/`
- `omniagentos/filesearch/`
- `omniagentos/fleetcap/`
- `omniagentos/formation/`
- `omniagentos/gates/`
- `omniagentos/goals/`
- `omniagentos/grants/`
- `omniagentos/graph_runtime/`
- `omniagentos/harnesses/`
- `omniagentos/health/`
- `omniagentos/improve/`
- `omniagentos/intake/`
- `omniagentos/integration/`
- `omniagentos/interactions/`
- `omniagentos/knowledge/`
- `omniagentos/lab/`
- `omniagentos/learning/`
- `omniagentos/lease/`
- `omniagentos/ledger/`
- `omniagentos/llm/`
- `omniagentos/longhaul/`
- `omniagentos/maintenance/`
- `omniagentos/memlife/`
- `omniagentos/memory/`
- `omniagentos/metacog/`
- `omniagentos/modelintel/`
- `omniagentos/northstar/`
- `omniagentos/notifications/`
- `omniagentos/orchestrator/`
- `omniagentos/orgdims/`
- `omniagentos/packgovernance/`
- `omniagentos/piedpiper/`
- `omniagentos/plans/`
- `omniagentos/policy/`
- `omniagentos/projects/`
- `omniagentos/prompts/`
- `omniagentos/promptshape/`
- `omniagentos/providers/`
- `omniagentos/provision/`
- `omniagentos/pulse/`
- `omniagentos/reflection/`
- `omniagentos/reliability/`
- `omniagentos/repomap/`
- `omniagentos/retrieval/`
- `omniagentos/revenue/`
- `omniagentos/routing/`
- `omniagentos/runner/`
- `omniagentos/scheduler/`
- `omniagentos/scope/`
- `omniagentos/security/`
- `omniagentos/selfimprove/`
- `omniagentos/semsearch/`
- `omniagentos/sessions/`
- `omniagentos/simprobe/`
- `omniagentos/skills/`
- `omniagentos/slo/`
- `omniagentos/steward/`
- `omniagentos/swarm/`
- `omniagentos/taskcontract/`
- `omniagentos/team/`
- `omniagentos/testobs/`
- `omniagentos/testpolicy/`
- `omniagentos/toolplane/`
- `omniagentos/tracelab/`
- `omniagentos/vault/`
- `omniagentos/vaultgraph/`
- `omniagentos/verify/`
- `omniagentos/voice/`
- `omniagentos/workfs/`
- `omniagentos/workmodes/`
- `omniagentos/workqueue/`
- `omniagentos/worktrees/`
- `omniagentos/wrapper/`
<!-- generated:end:packages -->


## API routes

<!-- generated:begin:routes -->
- `access.py`: GET /agents, GET /agents/{agent_id}, PUT /agents/{agent_id}, POST /call, GET /calls, GET /capabilities, GET /log, GET /servers, GET /tool-search
- `accounts.py`: GET /accounts, POST /accounts, GET /accounts/usage, DELETE /accounts/{account_id}, PATCH /accounts/{account_id}, POST /accounts/{account_id}/pause, POST /accounts/{account_id}/resume
- `alerts.py`: GET "", GET /count, POST /{alert_id}/ack
- `artifacts_preview.py`: GET /preview, GET /preview/raw
- `autonomy.py`: GET "", PUT ""
- `banking.py`: GET ""
- `board_files.py`: GET /{task_id}/files, GET /{task_id}/files/archive, GET /{task_id}/files/download, POST /{task_id}/files/reveal, POST /{task_id}/files/upload
- `briefings.py`: GET "", POST /generate, GET /latest, POST /{briefing_id}/ack
- `categories.py`: GET "", POST "", PATCH /{category_id}
- `cbm.py`: POST /allocate, GET /allocations/{allocation_id}, POST /allocations/{allocation_id}/close, POST /allocations/{allocation_id}/contract, POST /allocations/{allocation_id}/escalate, GET /allocations/{allocation_id}/escalations, GET /health, GET /leaderboard, GET /rungs
- `chats.py`: GET "", POST "", GET /folders, DELETE /folders/{name}, POST /folders/{name}/color, POST /folders/{name}/rename, DELETE /{chat_id}, GET /{chat_id}, PATCH /{chat_id}, POST /{chat_id}/classify, POST /{chat_id}/intent/suggest, GET /{chat_id}/messages, POST /{chat_id}/messages, POST /{chat_id}/plan, POST /{chat_id}/promote, POST /{chat_id}/promote_item, POST /{chat_id}/spawn
- `collab.py`: GET /agents, POST /agents, POST /board, PATCH /board/{task_id}, POST /board/{task_id}/claim, POST /board/{task_id}/release, POST /board/{task_id}/restore, GET /channels, POST /channels, GET /channels/{channel_id}/messages, POST /channels/{channel_id}/messages, GET /messages/search
- `comms.py`: POST /inbound, GET /messages, GET /messages/{message_id}, GET /sources
- `company_goals.py`: GET "", POST "", GET /{goal_id}, PATCH /{goal_id}, GET /{goal_id}/jira-links, POST /{goal_id}/jira-links, DELETE /{goal_id}/jira-links/{link_id}
- `compute.py`: GET /estate
- `connections.py`: GET ""
- `control.py`: GET /approvals, POST /approvals/{approval_id}/decision, GET /budgets, GET /disciplines, POST /disciplines, GET /events, GET /health, GET /ledger, GET /pause, PUT /pause, GET /runs, GET /runs/{run_id}, POST /runs/{run_id}/cancel, GET /tasks, POST /tasks, GET /tasks/{task_id}, POST /tasks/{task_id}/runs
- `decisions.py`: GET "", POST /rules, POST /rules/{rule_id}/promote, GET /{decision_id}, POST /{decision_id}/decide
- `engine.py`: GET /capabilities, GET /runs, GET /runs/{run_id}/snapshot
- `filesearch.py`: GET /filesearch, POST /filesearch/reindex, POST /filesearch/reveal, GET /filesearch/semantic, GET /filesearch/stats
- `goals.py`: GET "", POST "", GET /tree/{goal_id}, GET /{goal_id}, POST /{goal_id}/facts, POST /{goal_id}/pause, PATCH /{goal_id}/target
- `graph.py`: POST /demo/diamond, GET /health, GET /runs, POST /runs, POST /runs/diamond, GET /runs/{run_id}, POST /runs/{run_id}/nodes/{node_key}/complete, GET /runs/{run_id}/ready, GET /runs/{run_id}/view, GET /templates
- `grok_ops.py`: POST /allocate/simulate, POST /contracts/hash, GET /decision-center, POST /gates/eval, GET /grants, POST /grants, POST /grants/{grant_id}/revoke, GET /health, GET /interactions, POST /interactions, POST /interactions/consume, POST /interactions/expire, POST /interactions/{interaction_id}/answer, POST /interactions/{interaction_id}/deliver, GET /recommended-next-action, POST /revise
- `hierarchy.py`: GET /projects/tree, GET /projects/{project_id}/conversation, POST /projects/{project_id}/message, GET /tasks/{task_id}/conversation, POST /tasks/{task_id}/message
- `improvements.py`: GET "", GET /{improvement_id}, POST /{improvement_id}/apply, POST /{improvement_id}/approve, POST /{improvement_id}/pull, POST /{improvement_id}/reject, POST /{improvement_id}/rollback
- `intake.py`: GET /board, POST /board/archive, GET /board/needs-response, POST /board/{board_task_id}/retry, GET /board/{task_id}, POST /board/{task_id}/archive, POST /board/{task_id}/cancel, GET /board/{task_id}/conversation, GET /board/{task_id}/eta, GET /board/{task_id}/longhaul, POST /board/{task_id}/message, POST /board/{task_id}/pause, GET /board/{task_id}/sessions, POST /intake/clarify, POST /intake/dispatch, POST /intake/plan, GET /intake/plan/{job_id}, POST /intake/plan/{job_id}/confirm, POST /intake/quick
- `jira.py`: GET /health, GET /projects, GET /projects/{key}/statuses
- `knowledge.py`: GET /facts/{fact_id}, POST /facts/{fact_id}/demote, POST /facts/{fact_id}/promote, GET /graph, POST /ingest, POST /recall-preview, GET /recalls, GET /search, GET /stats
- `memlife.py`: GET /queue, POST /{candidate_id}/graduate, POST /{candidate_id}/reject, POST /{candidate_id}/reopen
- `metacog.py`: GET /artifacts, POST /artifacts/register, GET /artifacts/{artifact_id}, POST /checkpoints, GET /checkpoints/{checkpoint_id}, POST /checkpoints/{checkpoint_id}/resume, POST /context/compile, GET /health, GET /memories, GET /memory, POST /memory/candidates, POST /memory/search, POST /memory/{memory_id}/promote, POST /metacognition/evaluate, POST /reflection/run, POST /skills/synthesize, POST /strategies/select, POST /strategies/switch
- `models.py`: GET ""
- `models_formation.py`: GET /formation
- `mounts.py`: GET "", GET /{mount_id}/dir, GET /{mount_id}/file
- `notifications.py`: GET "", GET /count, POST /read-all, POST /{notification_id}/read
- `org.py`: GET /agent-requests, POST /agent-requests, POST /agent-requests/{request_id}/approve, POST /agent-requests/{request_id}/reject, GET /agents, GET /agents/{agent_id}, GET /agents/{agent_id}/activity, POST /agents/{agent_id}/toggle, GET /tree
- `orgdims.py`: GET /agents/grok, GET /board/{task_id}, PATCH /board/{task_id}, POST /classify/board_task, POST /classify/bulk, POST /classify/object, GET /companies, GET /health, GET /loops, POST /loops/recommend, GET /objects/{object_type}, PUT /objects/{object_type}/{object_id}, POST /seed, GET /views/matrix, GET /views/portfolio, GET /workstreams
- `projects.py`: GET "", POST "", GET /portfolio, GET /{project_id}, PATCH /{project_id}, GET /{project_id}/activity, GET /{project_id}/files, POST /{project_id}/files/upload, GET /{project_id}/files/{file_path:path}
- `provision.py`: GET /{project_id}, POST /{project_id}/request
- `pulse.py`: GET /metrics, GET /series
- `reflection.py`: GET /proposals, GET /{id}, POST /{id}/approve, POST /{id}/reject
- `reliability.py`: POST /audit/run, GET /audits, GET /audits/{audit_id}, GET /events, POST /events/{event_id}/ignore, POST /events/{event_id}/resolve, GET /scorecards, GET /summary
- `revenue.py`: GET "", GET /verticals
- `routines.py`: GET "", POST "", GET /engine, GET /runs, DELETE /{routine_id}, GET /{routine_id}, PATCH /{routine_id}, POST /{routine_id}/disable, POST /{routine_id}/enable, GET /{routine_id}/runs, POST /{routine_id}/runs
- `semsearch.py`: GET ""
- `session_events.py`: POST /hook
- `session_ledger.py`: GET /claims, GET /tail
- `sessions.py`: GET "", GET /discover, POST /discover/sync, POST /hook-eval, POST /ingest, GET /{session_id}, POST /{session_id}/cancel, POST /{session_id}/kill, POST /{session_id}/message, GET /{session_id}/transcript, GET /{session_id}/transcript/delta, POST /{session_id}/update
- `suggestions.py`: GET "", POST /generate, POST /{suggestion_id}/approve, POST /{suggestion_id}/dismiss
- `swarm.py`: GET "", POST "", GET /jobs/{job_id}, GET /overview, GET /providers, GET /team, GET /{run_id}, GET /{run_id}/activity, POST /{run_id}/cancel
- `system.py`: GET /agent-activity, GET /agents, PATCH /agents/{name}, GET /delta, GET /improvers, PATCH /improvers/{label}/prompt, GET /map, GET /skills, POST /spend-breaker/reset
- `system_jobs.py`: GET ""
- `team.py`: GET /accountability, GET /board, GET /commitments, POST /commitments, PATCH /commitments/{commitment_id}, GET /diagnostics, GET /evidence/unattributed, PATCH /evidence/{evidence_id}, POST /nl-assign, GET /report/preview, GET /scoreboard, POST /sessions/report, POST /tasks, GET /tasks/{task_id}/events, GET /tasks/{task_id}/evidence, POST /tasks/{task_id}/evidence, POST /tasks/{task_id}/unverify, POST /tasks/{task_id}/verify, GET /tree
- `testobs.py`: GET /overview, GET /series, GET /weakspots
- `today.py`: GET /today
- `voice.py`: GET /audio/{artifact_id}, GET /providers, POST /speak
- `workfs.py`: POST /ensure, GET /tree

**354 routes across 56 modules.**
<!-- generated:end:routes -->


## Ports & env flags

- API: `127.0.0.1:8485` (loopback only, no external bind, overridden via `OMNIAGENTOS_API_PORT`).
- Dashboard: `127.0.0.1:3003`, same-origin token proxy (browser never holds the
  session token, overridden via `PORT`).
- Key flags:
  - `OMNIAGENTOS_KNOWLEDGE` (now default-on (`1`) on the launch path, injecting Postgres+pgvector Synapse recall into runner briefs).
  - `OMNIAGENTOS_FILESEARCH_HINT` (default-on via launch-env, injects `<file-search>` block in runner briefs).
  - Nine capability flags defaulting to `shadow` in `launch-env.sh` (task-shape router, scope locks, tool catalog, tool scheduler, autonomy lease, champion routing, lab curation, allowed providers, reflection re-arm) to capture decision telemetry.
  - `OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP` & `OMNIAGENTOS_INDEX_VAULT_ON_STARTUP` (controls automatic seeding and playbook sync/indexing on API startup).
  - `OMNIAGENTOS_MEMORY` (conversation history injection).
  - `OMNIAGENTOS_CASCADE` / `OMNIAGENTOS_REFLEXION` (tier escalation + reflection).
  - `OMNIAGENTOS_ACCOUNT_POOL` (multi-account Claude config-dir pooling).
  - `OMNIAGENTOS_VAULT_AUTOCOMMIT` (vault note git auto-commit).
  - `OMNIAGENTOS_CURATOR_LIVE_AGENT` (curator narrative pass).
  - `OMNIAGENTOS_REQUIRE_PG` (requires Postgres for testing knowledge components).
  - `OMNIAGENTOS_API_BASE` (overrides backend target from nextjs server proxy).
  - `OMNIAGENTOS_VAR_DIR` (overrides default swarm var directory root, defaulting to `var/runtime`).
  - `OMNIAGENTOS_ARCHI_MORNING_HOUR` & `OMNIAGENTOS_ARCHI_MORNING_MINUTE` (controls/overrides the specific hour and minute the daily morning map and diagram refresh job executes, defaulting to 05:30).

## How to extend

- New DB tables → new numbered migration under `omniagentos/db/migrations/`, never
  edit `contracts/schema.sql` (Wave 0 frozen) or earlier migrations.
- New API route module → register in `omniagentos/api/main.py`
  (`app.include_router(...)`); respect the `{"error": {"code","message","detail"}}`
  envelope and the session-token gate.
- New SSE event type → add to `omniagentos.contracts.Events` (frozen; needs an
  explicit amendment) or, for additive V2 surfaces, document as a string type in a
  new `contracts/*.md` file (see `contracts/reliability-api.md`) — the frozen
  `Events`/`useEvents` contract stays untouched.
- New launchd job → follow the `install-steward.sh` idiom: render a plist template,
  install via a `scripts/<subsystem>/install-*.sh`, source `connections.env`, pin
  `.venv/bin/python`. Point the routines tick installer at the product runtime (`var/runtime`) and source `launch-env.sh` to align its database path.
- Agent behavior & workspaces → Always align with `AGENTS.md` rules. Ensure workspace actions respect private-worktree boundaries, keeping coordinator-owned files (e.g., `PLAN.md`) untouched (which are automatically reverted on the branch base and committed by the coordinator on the branch, avoiding `sched-scope` conflicts, before merge to prevent pollution/conflicts). Note that lane landing fetches the branch but does not merge it, requiring explicit merge to prevent stranded branches from falling behind main. Document progress incrementally inside `WORKBOOK.md` in `var/swarm/`.
- Quality assurance & testing → Rigorously enforce lineage-enforcement rules, tests-at-creation patterns, and the reusable invariant corpus. For every new capability/feature lane, implement both a decisive test and a counterfeit test to prove behavior truthfully. Enforce the structural lane acceptance doctrine using ordered blocks, byte-exact assertions, and full witness printing. Ensure all new code passes the strict pre-merge verification pipeline in `merge-gate.sh` protecting against leaked secrets, symlinks, migration mutations, or unverified branch states, though merge operations are permitted if the only refusal is a check that cannot pass under the circumstances. To safeguard code changes on the merge path, apply the three-lineage aggregate verification flow (coder -> reviewer -> aggregate verifier) before merging to main.
- New architecture subsystem or relation → edit the hand-curated node/edge tables in `omniagentos/archdocs/diagram.py` and regenerate the system-map diagram using `python -m omniagentos.archdocs.diagram` (also run as part of the daily `archi-morning` morning maintenance job at 05:30, which regenerates the top map repository inventories, restamps files, and applies a diff-guarded local auto-commit of the updated doc set with the `archi-morning: refresh map + diagram` commit message, ensuring continuous synchronization between diagram code and visual outputs).
- Regenerate this file's inventories → `python -m omniagentos.archdocs.generate`;
  agent-driven narrative edits go through `omniagentos.archdocs.update.apply_update`
  (Tier S — only ever called by the pipeline applying an APPROVED docs improvement).

## Notes (human)

