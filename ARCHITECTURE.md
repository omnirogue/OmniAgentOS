# ARCHITECTURE.md — System Architecture Overview

This is the system architecture guide for OmniAgentOS.

- **Machine Truth**: `ARCHI.json` is the authoritative machine-readable map of the system.
- **Narrative Map**: `ARCHI.md` provides a human-readable top-level overview of the subsystems.
- **System Diagram**: `docs/architecture/system-map.md` renders the live visual representation via Mermaid.
- **Rule of Edit**: Never edit architecture files manually. Modify only via `omniagentos.archdocs.update.apply_update`.

## Subsystems

- **Execution** — Fable-conducted task/run/step state machine (runner, orchestrator, routing/adapters). See `docs/architecture/execution.md`.
- **Governance** — ActionClass risk gate, approvals, policy config, budgets, ledger. See `docs/architecture/governance.md`.
- **Knowledge** — skills library, Synapse knowledge graph, memory layer, vaultgraph, repomap, filesearch. See `docs/architecture/knowledge.md`.
- **UI** — Next.js mission-control dashboard (SSE, approvals, sessions, board). See `docs/architecture/ui.md`.
- **Scheduling** — launchd job templates + installers, routines engine. See `docs/architecture/scheduling.md`.
- **Reliability** — V2 self-improving failure-detection/recovery/judge/pipeline system. See `docs/architecture/reliability.md`.
- **Organization** — V2 agent org hierarchy (CTO/VPs/managers/specialists/judges). See `docs/architecture/organization.md`.
- **Longhaul** — long-horizon coding lane: durable board task + executor attempt chain, usage-limit account handoff, task-level steering, per-category WIP serialization, registry-ranked worker routing, fail-closed completion review. See `docs/architecture/longhaul.md`.
