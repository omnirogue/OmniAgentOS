# SQLite migrations — OmniAgentOS version claim

**Product:** OmniAgentOS (never merge into OmniAgentOS)  
**Claimed range for this product's additive schema:** **060–137**

| Version | File | Purpose |
|--------:|------|---------|
| 060 | `060_metacog.sql` | MetaCognition artifacts / memory / control |
| 061 | `061_org_dimensions.sql` | Multidimensional org taxonomy |
| 062 | `062_graph_runtime.sql` | Swarm Graph V2 |
| 063 | `063_cognitive_budget.sql` | Cognitive Budget Manager |
| 064 | `064_projects_kind.sql` | Portfolio kind column |
| 065 | `065_formation_selections.sql` | Formation/ETAR telemetry |
| 066 | `066_csi_runs.sql` | Continuous self-improvement runs |
| 067 | `067_csi_approval.sql` | CSI-native human approval columns |
| 068 | `068_migration_checksums.sql` | Applied-migration file checksums (H-09/M-06) |
| 069 | `069_projects_kind_index_normalization.sql` | Forward-only repair for migration 064 index drift (M-06) |
| 070 | `070_portfolio_board_indexes.sql` | Portfolio/board listing indexes, including the partial archived-listing index (L-10 archive portion) |
| 071 | `071_hot_lookup_indexes.sql` | Remaining L14-owned L-10 hot lookups: approvals(run,step), swarm_attempts(session), idempotency(run) |
| 072 | `072_longhaul_provider_harnesses.sql` | Long-haul provider harnesses |
| 073 | `073_chat_workspaces.sql` | Chat workspaces |
| 074 | `074_preflight.sql` | Preflight state |
| 076 | `076_reflection.sql` | Reflection state |
| 077 | `077_reflection_reconcile.sql` | Reflection reconciliation |
| 078 | `078_formation_selections_finished_at.sql` | Formation completion timestamp |
| 079 | `079_task_shape_decisions.sql` | Task-shape decisions |
| 080 | `080_gate_evidence.sql` | Gate evidence |
| 081 | `081_insession_grants.sql` | In-session grants |
| 082 | `082_lab_verdict_provenance.sql` | Lab verdict provenance |
| 083 | `083_improve.sql` | Improvement state |
| 084 | `084_pulse_series.sql` | Pulse time series |
| 085 | `085_lab_jobs.sql` | Durable lab jobs |
| 086 | `086_control_plane.sql` | Control-plane state |
| 087 | `087_board_project_scope.sql` | Board project scope |
| 088 | `088_chat_folders.sql` | Chat folders |
| 089 | `089_sessions_cost_usd_nullable.sql` | Nullable session cost |
| 090 | `090_memlife.sql` | MemLife state |
| 091 | `091_routines_merge_candidate_gate.sql` | Routines merge candidate gate |
| 092 | `092_jira_project_key.sql` | Jira project-key binding |
| 093 | `093_routines_meta.sql` | Routine scope and purpose metadata |
| 094 | `094_provider_call_usage.sql` | Exact provider-call usage and cost provenance |
| 095 | `095_waku_kimi_core.sql` | Waku/Kimi cache, budget, and memory telemetry |
| 096 | `096_dag_moe_gating.sql` | DAG step edges and MoE gating telemetry |
| 097 | `097_routine_revision.sql` | Integer routine revision for lifecycle CAS |
| 098 | `098_company_goals.sql` | Company goals spine and employee ownership |
| 099 | `099_transcript_uploads.sql` | Employee transcript upload provenance |
| 100 | `100_plans_durable.sql` | Durable plan jobs and route decisions |
| 101 | `101_provenance_source.sql` | Run provenance source classification |
| 102 | `102_provenance_backfill_unknown.sql` | Honest unknown provenance for historical rows |
| 103 | `103_run_outcome_class.sql` | Routine-run outcome taxonomy |
| 104 | `104_routine_run_self_report.sql` | Separate executor self-report from adjudicated outcome |
| 105 | `105_request_attribution.sql` | Truthful request and emitting-principal attribution |
| 106 | `106_grant_lifecycle_columns.sql` | Capability-grant lifecycle metadata |
| 107 | `107_broker_audit_spine.sql` | Mandatory broker intent/final audit spine |
| 108 | `108_loop_broker_grants.sql` | Broker-side standing grants for live loop callers |
| 109 | `109_skill_status_and_selection_fields.sql` | Skill status vocabulary and selection metadata |
| 110 | `110_skill_version_content_digest.sql` | Skill-version content digests |
| 111 | `111_secret_catalog.sql` | Name-only secret catalog and rotation state |
| 112 | `112_capability_decisions.sql` | Capability decisions and terminal-state guard |
| 113 | `113_capability_requests.sql` | Immutable capability-request envelopes |
| 114 | `114_swarm_attempts_verdict_hash.sql` | Pump-attempt verdict fingerprints for non-progress detection |
| 115 | `115_grant_project_binding.sql` | C11/D-31 project binding on capability grants and requests |
| 116 | `116_routine_project_scope.sql` | M1 project scope on routines |
| 117 | `117_account_weekly_quota.sql` | M5 per-account weekly planner quota |
| 118 | `118_session_cost_estimate.sql` | C4 session token-cost estimate + provenance (cap accrual; `cost_usd` NULL stays "unpriced") |

## Why this matters

`schema_migrations` records only the **integer version**. Two products that ship
different SQL under the same number cause silent skips on convergence: a DB that
already applied "060" will never run a different 060 from another tree.

OmniAgentOS therefore **claims 060–137** exclusively for its product schema.
Sibling forks / worktrees that invent competing `060_*` / `062_*` files must
renumber **their** migrations into a non-overlapping range before any merge or
shared-DB experiment.

Do **not** renumber 060–063 in this tree after they have been applied to operator
databases — that would re-apply different SQL under new numbers or leave ghosts.
New Grok migrations start at **138**.
