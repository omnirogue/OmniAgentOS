# OmniAgentOS — Artifacts, Memory & Metacognition Upgrade

**Source:** `~/Desktop/MetaCognitionOmniAgent OS/`  
**Product:** OmniAgentOS only (never merge into OmniAgentOS)  
**Mode default:** `enforce` (**LIVE**) — strategy switches apply, memory promotes, skills can promote. Set `shadow`/`off` in `configs/metacog.yaml` or env to dial back.

## Closed loop

```
Task → Plan → Execute → Produce Artifacts → Verify → Reflect
  → Promote Memory → Update Skills/Policies → Improve Future Planning
```

## Reuse vs build

| Spec need | Existing Grok surface | Build |
|-----------|----------------------|-------|
| Conversation context | `omniagentos.memory.assemble` | Extend via context compiler |
| Knowledge graph | `omniagentos.knowledge` | Typed memory_records layer on top |
| Run step checkpoints | `runner.core` checkpoint_json | First-class checkpoint packets + recovery API |
| File previews | `/api/artifacts/preview` | Full artifact registry (hash + provenance) |
| Reflexion paragraphs | `selfimprove.reflexion` | Structured reflection_reports + candidates |
| Skills capture | `selfimprove.skills` (gate=PASSED only) | Skill synthesis canary pipeline |
| Strategy routing | swarm router / modelintel | Strategy registry + metacog control actions |
| Verification | execution assess / swarm review | Evidence-backed verification reports as artifacts |

## Phases (all implemented in this tree)

| Phase | Deliverable | Gate |
|-------|-------------|------|
| **0** | Schemas, feature flags (`configs/metacog.yaml`), metrics hooks, plan doc | Import-safe; mode defaults **enforce** (live) |
| **1** | Artifact store, content hash, provenance edges, checkpoints | Register/dedupe/list tests |
| **2** | Context compiler (task + artifacts + memory + skills) | Bounded packet + omissions log |
| **3** | Typed memory records, search, candidates, promote/invalidate | Shadow promotion path |
| **4** | Progress snapshots, stall/repetition detection | Two no-progress → not CONTINUE |
| **5** | Strategy registry, select/switch, control actions | Switch records decision_records |
| **6** | Reflection → candidates → skill synthesis | Candidates require evidence |
| **7** | Coding-domain hooks (runner/swarm optional attach) | Domain fingerprint helpers |
| **8** | Autonomous enforcement flags (shadow → canary → enforce) | Env/config only; fail closed |

## Feature flags

```yaml
# configs/metacog.yaml  (LIVE defaults)
mode: enforce          # off | shadow | enforce
memory_promotion: enforce
strategy_switch: enforce
skill_canary: enforce
```

Env overrides (any direction):

- `OMNIAGENTOS_METACOG_MODE`
- `OMNIAGENTOS_METACOG_MEMORY_PROMOTION`
- `OMNIAGENTOS_METACOG_STRATEGY_SWITCH`
- `OMNIAGENTOS_METACOG_SKILL_CANARY`

## APIs

| Method | Path |
|--------|------|
| POST | `/api/metacog/artifacts/register` |
| GET | `/api/metacog/artifacts/{id}` |
| POST | `/api/metacog/checkpoints` |
| GET | `/api/metacog/checkpoints/{id}` |
| POST | `/api/metacog/context/compile` |
| POST | `/api/metacog/memory/search` |
| POST | `/api/metacog/memory/candidates` |
| POST | `/api/metacog/memory/{id}/promote` |
| POST | `/api/metacog/metacognition/evaluate` |
| POST | `/api/metacog/strategies/select` |
| POST | `/api/metacog/strategies/switch` |
| POST | `/api/metacog/reflection/run` |
| POST | `/api/metacog/skills/synthesize` |
| GET | `/api/metacog/health` |

## Acceptance (from source plan)

Covered by unit tests under `tests/metacog/`:

1. Checkpoint resume packet is durable and side-effect safe flags  
2. Artifacts reconstruct evidence without chat history  
3. Stale memory down-ranked / invalidated  
4. Two no-progress cycles refuse CONTINUE  
5. Reflection requires evidence for candidates  
6. Strategy switch records reason codes  
7. Enforcement stays shadow until flags flip  

## Non-goals (this wave)

- Full multi-day shadow soak on live operator traffic  
- Automatic canary traffic splitting in production  
- Replacing swarm/runner control loops wholesale (attach points only)
