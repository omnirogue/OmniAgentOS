# Parallel-Autonomy Upgrade (Pruned) — OmniAgentOS matrix

Source: `~/Desktop/OmniAgentOS_Parallel_Autonomy_Upgrade_Pruned.docx`  
Product: **OmniAgentOS only** (never merge to OmniAgentOS)

## Explicitly removed in pruned doc (do NOT implement)

| Item | Status |
|------|--------|
| T4.4 Full vocabulary mapping | **out of scope** |
| T6.7 Symbol ledger (U2) | **out of scope** |
| T6.8 Task-to-symbol translator (U3) | **out of scope** |
| T7.7 Per-mechanism ablations | **out of scope** |
| TN.10 enterprise audit / applications table | **out of scope** (nudge/question kept) |

## Required items → Grok evidence

| ID | Requirement | Grok status | Evidence |
|----|-------------|-------------|----------|
| T0.1–T0.2 | Preserve Posse research | N/A product | External `~/Reference` |
| T1.1 | SSE load harness | **done** | `scripts/loadtest_sse.py` |
| T1.2 | Sessions N+1 + index | **done** | migration 055, sessions/dal |
| T1.3 | Events index | **skipped correctly** | Reverted no-op duplicate |
| T1.4 | One EventSource/tab | **done** | `EventStreamProvider.tsx` |
| T1.5 | In-process EventHub | **done** | `api/eventbus.py` |
| T1.6 | Poll backoff | **done** | `pollWhenVisible.ts` |
| T2.1–T2.4 | Per-thread SQLite | **done** | `db/store.py` reader/writer |
| T3.1–T3.9 | Scope + worktrees + lanes | **done** | `scope/`, `worktrees/`, 059, shadow |
| T4.1–T4.2 | Contracts + migration | **done** | contracts + **058** (not 060; same payload) |
| T4.3 | Floors | **done** | `execution/policy.py` |
| T4.5 | Effort key fix | **done** | vocab + adapters |
| T4.6 / U5 | Worker abstraction | **done** | `routing/workers.py` |
| T4.7–T4.10 | Toolplane CLI | **done** | `toolplane/` (CLI not MCP) |
| T5.1–T5.5 | Verify/assess/violations | **done** | `execution/*` |
| T6.1 | Repomap content-hash | **done** | migration 057 |
| T6.2 | Unified RRF | **done** | retrieval / callers |
| T6.3 | Feedback loop | **done** | knowledge ranker |
| T6.4 | Chain-read | **done** | `sessions/chain_read.py` + hook |
| T6.5 | Insight promotion | **done** | consolidate + reliability memory |
| T6.6 | Intake context | **done** | fastlane toolplane + brand_context |
| T6.9 / U4 | Ghost context | **done** | `context/ghost.py` |
| T6.10 | Learning toolkit | **done** | `learning/` |
| TN.0–TN.9,11–14 | Workmodes/grants/etc. | **done** | workmodes, grants, connectors… |
| TN.10 nudge | Keep lean | **done** | `interactions/` (no applications table) |
| T7.0 | Baseline harness | **done** | loadtest + cert suite |
| T7.1 | Floor validation | **done** | `eval/counterfactual.py` |
| T7.2 | 72h shadow soak | **ops** | shadow default; needs live traffic |
| T7.3–T7.4 | Race + isolation | **done** | tests/scope/* |
| T7.5 | Raise concurrency | **done** | `configs/concurrency.yaml` + ramp doc |
| T7.6 | Counterfactual | **done** | `eval/counterfactual.py` |
| T7.8 | Empirical ceilings | **partial** | docs + measure path; no account buy |

## Certify

```bash
cd ~/OmniAgentOS
./scripts/certify-omniagentos.sh
uv run pytest tests/routing/test_workers.py tests/sessions/test_chain_read.py tests/eval -q
```

---

## Desktop specs → Grok status (closed vs deliberately skipped)

Sources on `~/Desktop/` (2026-07-25). Same habit as the pruned matrix: every
row is either **done**, **wired**, **module-only**, **ops**, or **out of scope**.

### OmniAgentOS Graph Runtime Product Upgrade Spec

| Spec item | Status | Evidence |
|-----------|--------|----------|
| Typed nodes + ports | **done** | `graph_runtime/contracts.py` |
| Artifact-carrying edges + hashes | **done** | `graph_runtime/service.py` complete_node |
| Diamond fan-out → reduce → verify → synthesize | **done** | `diamond-v1` template + demo API |
| fail_closed completeness | **done** | fan-in completeness checks |
| GraphCompiler (cycles, order) | **done** | `compile_template` / `detect_cycles` |
| Live graph view | **done** | `/api/graph/runs/{id}/view` + dashboard `/graph` |
| Reusable versioned templates | **done** | `graph_templates` + seed |
| Default multi-writer swarm path | **module-only / partial** | API LIVE; planner auto-graph not default |
| Layered multi-level reduce trees | **out of scope (v1)** | Single reduce stage only |
| Resource claim compiler (full) | **partial** | fields on nodes; not full arbitration |

### Progressive Escalation & Cognitive Budget Manager Plan

| Spec item | Status | Evidence |
|-----------|--------|----------|
| Fast-first allocate | **done** | `cbm.service.allocate` rung 1 |
| Ladder 0–6 | **done** | `RUNGS` |
| decide_gate contract/escalate/UNKNOWN | **done** | `decide_gate` |
| ETAR recommend | **done** | `recommend_rung` |
| Spawn applies allocation | **wired** | `swarm/spawn.py` effort + prompt stamp |
| Outcome feedback → role stats | **wired** | `close_allocation` + `cbm_role_stats` + learning.api |
| Verifier reservation hard enforcement | **partial** | verification_rung on alloc; not fleet lock |
| Full multi-stage team-leader loop | **module-only** | API + spawn; not every stage |

### Multidimensional Project Organization UI Upgrade Plan

| Spec item | Status | Evidence |
|-----------|--------|----------|
| Taxonomy + workstreams | **done** | `orgdims/taxonomy.py` |
| ClassificationService confidence | **done** | `orgdims/classify.py` |
| Board chips / matrix / portfolio | **done** | dashboard orgdims + board |
| Bulk reclassify | **done** | `bulk_reclassify` |
| Skills/agents/loops dims | **done** | object dims API |
| Cross-company isolation | **done** | inherit + company_id |
| Full AI model classifier (LLM) | **out of scope (v1)** | Deterministic+vocab only |
| Permission RBAC per dimension | **out of scope (v1)** | Single-operator local |

### MetaCognition upgrade plan (Artifacts / Memory / Control)

| Spec item | Status | Evidence |
|-----------|--------|----------|
| Artifacts + content hash | **done** | metacog store |
| Checkpoints safe_to_resume | **done** | create_checkpoint |
| Memory promote guards | **done** | promote_memory; shadow default |
| Evaluate stall/repetition/stop | **done** | `evaluate` |
| Strategy switch | **done** | switch_strategy |
| Reflect → promote_memory path | **wired** | no self-promote bypass |
| Skill synthesis canary | **done** | skill_canary mode |
| Always-on enforce all lanes | **ops** | mode enforce; memory shadow by design |

### Facade resolution (production path)

| Module | Status | Wire point |
|--------|--------|------------|
| `routing/workers.py` | **wired** | `swarm/router.py` preferred-provider boost; exported from `routing/__init__` |
| `skills/select.py` | **wired** | `swarm/spawn.py` prompt stamp; no hard max-8 cap |
| `connectors/globex_studio.py` | **wired** | toolplane `globex_generate_image/video` |
| `intake/brand_context.py` | **wired** | intake `_resolve_working_dir` via `OMNIAGENTOS_BRAND_PACK` |
| `learning/api.py` | **wired** | `cbm.close_allocation` outcome log |

### Migration claim

| Range | Owner |
|-------|--------|
| **060–069** | **OmniAgentOS only** (see `omniagentos/db/migrations/README.md`) |
