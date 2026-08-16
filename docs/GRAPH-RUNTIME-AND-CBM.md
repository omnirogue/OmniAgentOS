# Graph Runtime V2 + Cognitive Budget Manager

**Product:** OmniAgentOS (separate from OmniAgentOS)  
**Status:** **LIVE** (2026-07-25)

## Graph Runtime (Swarm Graph V2)

Additive typed graph on top of the existing swarm DAG — **not** a second orchestrator.

### Product model

- **Nodes** with declared input/output ports and schema checks
- **Edges** that carry hashed artifacts (not just “start after”)
- First-class types: `fan_out`, `reduce`, `verify`, `synthesize`, `anchor`, `human_gate`, `worker`
- Completeness policies: `fail_closed` (default) | `allow_partial`
- Built-in **diamond** template: fan-out → reduce → verify → synthesize

### API

| Method | Path |
|--------|------|
| GET | `/api/graph/health` |
| GET | `/api/graph/templates` |
| GET | `/api/graph/runs` |
| POST | `/api/graph/runs/diamond` |
| POST | `/api/graph/runs` |
| GET | `/api/graph/runs/{id}` |
| GET | `/api/graph/runs/{id}/view` |
| GET | `/api/graph/runs/{id}/ready` |
| POST | `/api/graph/runs/{id}/nodes/{key}/complete` |
| POST | `/api/graph/demo/diamond` |

### Storage

Migration `062_graph_runtime.sql`: `graph_templates`, `graph_runs`, `graph_nodes`, `graph_edges`, `graph_artifacts`.

### UI

Dashboard: **Executions → Graph & CBM** (`/graph`).

---

## Cognitive Budget Manager (CBM)

**START FAST → VERIFY → ESCALATE ONLY ON EVIDENCE → STOP AT ACCEPTED QUALITY**

- No cost objective — optimizes **accepted quality** and **ETAR** (expected time to accepted result)
- Ladder rungs **0–6** (mechanical → break-glass)
- Fast-first default (rung 1); risk/novelty raise the start rung
- Escalation records trigger + changed fields + next gate
- Contraction after gate pass; outcomes feed role leaderboards
- Multi-model provider hints per role (claude / codex / grok / gemini / kimi)

### API

| Method | Path |
|--------|------|
| GET | `/api/cbm/health` |
| GET | `/api/cbm/rungs` |
| POST | `/api/cbm/allocate` |
| GET | `/api/cbm/allocations/{id}` |
| POST | `/api/cbm/allocations/{id}/escalate` |
| POST | `/api/cbm/allocations/{id}/contract` |
| POST | `/api/cbm/allocations/{id}/close` |
| GET | `/api/cbm/allocations/{id}/escalations` |
| GET | `/api/cbm/leaderboard` |

### Storage

Migration `063_cognitive_budget.sql`: `cbm_allocations`, `cbm_escalations`, `cbm_outcomes`, `cbm_role_stats`.

### Integration

- Swarm spawn best-effort `allocate()` before launch (never blocks spawn)
- Orgdims classification still runs on spawn as before

### Flags

CBM and Graph Runtime ship **LIVE by default** (no opt-in flag required). Optional env:

| Flag | Purpose |
|------|---------|
| (none required) | Always on in product |
| `OMNIAGENTOS_CASCADE=1` | Existing verification-gated cascade (orthogonal) |

---

## Multidimensional org (related)

See `docs/ORG-DIMENSIONS-UPGRADE-PLAN.md` — taxonomy, classify, matrix/portfolio, Grok metacog agents — already LIVE.
