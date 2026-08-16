# Multidimensional Organization & UI — OmniAgentOS

**Source:** `~/Desktop/OmniAgentOS_Multidimensional_Project_Organization_UI_Upgrade_Plan.docx`  
**Product:** OmniAgentOS only  

## Decision

Do **not** force all meaning into one folder tree. Use:

- **Authoritative hierarchy:** Company → Product → Initiative → Epic → Task → Run  
- **Orthogonal dimensions:** workstream, domain, channel, lifecycle, risk, priority, skills/agents/loops  

## Grok as primary metacognition stack

| Agent slug | Role |
|------------|------|
| **grok-orchestrator** | Metacognitive controller, classification, portfolio routing |
| **grok-self-repair** | Stall/failure repair loops, rollbacks |
| **grok-self-learn** | Reflection → memory → skills |
| **grok-memory-curator** | Promote/invalidate typed memory |
| **grok-verifier** | Evidence-backed acceptance checks |
| **grok-strategy-selector** | Topology / effort / fallback ladder |

Lineage defaults to `cli-grok` / `grok-4.5`.

## Phases in this tree

| Phase | Status |
|-------|--------|
| P1 Taxonomy + migration 061 + company seed | **done** |
| P2 Classification service (deterministic+vocab, Grok orchestrator attribution) | **done** |
| P3 Kanban chips + auto-classify on card create | **done** |
| P4–P6 Skills/Agents/Loops dimension editor UI | **done** (`/orgdims` + object dims API) |
| P3 Matrix/portfolio board views | **done** (board tabs + `/api/orgdims/views/*`) |
| P3 Bulk reclassify | **done** (`POST /classify/bulk` + board button) |
| P7 Align artifacts/memory (metacog) | linked via preferred agents + risk + evaluate tick |
| P8 Live classification + bulk migration | **done** (live apply + bulk tool) |
| Swarm spawn classification | **done** (`UnifiedSpawner.spawn`) |
| Metacog evaluate on board reconcile ticks | **done** (throttled active cards) |

## APIs

| Method | Path |
|--------|------|
| GET | `/api/orgdims/health` |
| POST | `/api/orgdims/seed` |
| GET | `/api/orgdims/companies` |
| GET | `/api/orgdims/workstreams` |
| GET | `/api/orgdims/agents/grok` |
| POST | `/api/orgdims/classify/board_task` |
| GET | `/api/orgdims/board/{task_id}` |

## Workstreams (controlled)

Engineering, Product, Creative, Advertising, Marketing, Sales, Customer Success, Operations, Finance/Admin, Legal/Compliance, Research, Data & Analytics, Executive/Strategy.

Legacy map: Coding→Engineering, Prototypes→Product, Creatives→Creative, Advertising→Advertising.
