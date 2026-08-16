# PLAN2-TASK — C1 Engine consolidation (branch plan2/c1-engine-consolidation)
Full spec: Desktop plan C1 (audit: 4 durable engines, keep swarm). PHASE 1 ONLY on this branch: freeze, do not delete.
OWN: deprecation shims + log-on-use warnings at the entrypoints of omniagentos/reliability/ (engine entry only — NOT the api routes), omniagentos/csi/, omniagentos/longhaul/; inventory doc docs/consolidation/C1-ENGINE-INVENTORY.md mapping every caller of each engine (rg-based, exhaustive) + which pieces are live (072 longhaul provider-harness config) and must port to swarm scheduler config.
DO NOT delete code on this branch. DO NOT touch omniagentos/swarm/.
Acceptance: importing/invoking a frozen engine logs a deprecation warning once; full targeted test suite still green (uv run pytest -q tests/reliability tests/longhaul tests/csi if present).
