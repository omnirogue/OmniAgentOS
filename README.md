# OmniAgentOS

[![CI](https://github.com/omnirogue/OmniAgentOS/actions/workflows/ci.yml/badge.svg)](https://github.com/omnirogue/OmniAgentOS/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A local-first operating system for governed AI agent workflows.**

OmniAgentOS is a production-grade control plane for running fleets of AI agents
on your own machine: a persistent task/run/step state machine, model-agnostic
routing across Claude / GPT / Grok / Gemini coding CLIs, parallel swarm
execution in fenced git worktrees, a mechanical merge gate with signed
candidate receipts, cognitive budgets, an approvals system, a knowledge and
memory layer, and a real-time mission-control dashboard — all running against
a local SQLite database with no cloud dependency.

It was extracted from a live multi-agent engineering estate that has been
planning, building, reviewing, and merging its own code with minimal human
intervention. This repository is that system, scrubbed and renamed for public
release: real architecture, real gates, real tests — not a demo.

## What's inside

| Subsystem | What it does |
|---|---|
| **Execution** | Task → run → step state machine with a polling runner, per-model adapters, multi-lane integration pipeline (coder → per-lane reviewer → aggregate verifier), and private-worktree fencing for parallel writers |
| **Governance** | ActionClass risk gating, approvals with real-time delivery, cognitive budget management (CBM), an append-only event ledger, and `scripts/merge-gate.sh` — a mechanical pre-merge gate that verifies signed, candidate-bound evidence receipts |
| **Knowledge** | Skills library, knowledge graph, unified `recall()` memory front-door with rank fusion, repo map, federated file search, and automated skill synthesis |
| **UI** | Next.js mission-control dashboard: Chat, Kanban board, loops/connections/pulse observatory, SSE approvals, ANSI terminal follow stream (port 3003) |
| **Scheduling** | launchd job templates + installers, a routines engine, and a daily architecture-map regeneration job |
| **Reliability** | Self-improving failure detection, recovery pipeline, and a 20-scenario deterministic swarm simulation harness (no LLM, no network) |
| **Organization** | Agent org hierarchy — CTO / VPs / managers / specialists / integrators / judges — with role-scoped system prompts in a versioned registry |

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Node 22+, and macOS or Linux
(scheduling integrations are launchd-based, so macOS is the first-class target).

```bash
make sync         # install Python dependencies from uv.lock
make migrate      # apply SQLite schema migrations
make api          # FastAPI backend on http://127.0.0.1:8485
make runner       # step-polling runtime worker (separate terminal)

cd dashboard && npm install && PORT=3003 npm run dev   # http://127.0.0.1:3003
```

The API refuses `/api/**` calls without a trusted-hop assertion by design; use
the documented local dashboard authentication flow rather than bypassing it:
`docs/runbooks/dashboard-local-auth.md`.

Validation ladder (see `TESTING.md`):

```bash
make lint         # ruff
make type         # mypy
make test         # full pytest suite
make e2e          # Playwright dashboard tests
```

## Architecture

`ARCHI.md` is the human-readable map and `ARCHI.json` the machine-readable
truth — both regenerated mechanically (`python -m omniagentos.archdocs.generate`),
never edited by hand. Per-domain deep dives live in `docs/architecture/`
(execution, governance, knowledge, ui, scheduling, reliability, organization),
and `docs/architecture/system-map.md` renders the live Mermaid system diagram.

Agent house rules — orchestration discipline, worker git rules, the memory
ritual — are in `AGENTS.md`. They double as documentation of how a multi-agent
system keeps itself coherent.

## Design principles

- **Mechanical over vibes.** Anything that can be verified by a script is
  verified by a script: the merge gate, reachability checks, doc-honesty
  gates, receipt-bound evidence. Model review supplements gates, never
  replaces them.
- **Local-first.** One machine, one SQLite file, no required cloud services.
  Model access goes through the CLIs and API keys you already have.
- **Fail closed.** Half-configured environments refuse to run rather than
  guessing. Unknown state is never rendered as a favorable value.
- **Agents are staff, not magic.** Roles, queues, ownership, evidence
  requirements, and review classes apply to model workers exactly as they
  would to human contributors.

## Status

Research-grade and under active development. The engine, gates, and dashboard
run real workloads daily in the estate this was extracted from, but the public
cut is young: expect rough edges, macOS bias, and fictional example data
(Initech, Globex, Acme University…) standing in for the original operators.

## License

[MIT](LICENSE)
