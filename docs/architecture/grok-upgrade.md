# Grok architecture upgrade

## SEPARATE PRODUCT — no merge

- OmniAgentOS (`~/OmniAgentOS`) is a **separate product** from OmniAgentOS (`~/OmniAgentOS`).
- **Do not merge**, PR, or cherry-pick campaign between them as integration.
- Sibling forks may share design ideas; re-implement intentionally in each tree.
- See root [`SEPARATE-PRODUCT.md`](../../SEPARATE-PRODUCT.md).

Working branch: `grok-architecture-integration` (OmniAgentOS fork of OmniAgentOS).

## Plan of record

Desktop plan: `~/Desktop/Grok OmniAgent Architecture/OMNIAGENTOS-UPGRADE-PLAN.md`
(Volumes I–III + parallel-autonomy handoff). This repo implements foundations in-tree.

## Foundations landed here

| Package | Role |
| --- | --- |
| `omniagentos/allocation` | Task characterization + quality-first fan-out topologies |
| `omniagentos/gates` | G0–G10 decision envelope + stub GateService seams |
| `omniagentos/learning` | Thin facade over `routing.learn` + decision/outcome JSONL API |
| `omniagentos/promptshape/assembly` | Layered prompt assembly with content hashes |
| `dashboard/src/lib/pollWhenVisible.ts` | T1.6 visibility-aware safety polls |
| `tests/scope/test_cross_lane_races.py` | Thread-level cross-lane lock invariant |
| `vault/prompts/*` | Universal base + executor/verifier role overlays |

## Related docs

- Feature flag inventory: [`FEATURE-FLAGS.md`](../../FEATURE-FLAGS.md)
- Residual risks (#12 bench false-pass, #13 reader-lock scope): [`RESIDUAL-RISKS.md`](../../RESIDUAL-RISKS.md)
- System map: [`ARCHI.md`](../../ARCHI.md)
