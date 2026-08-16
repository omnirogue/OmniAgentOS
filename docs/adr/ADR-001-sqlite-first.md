# ADR-001: SQLite-first durable execution; Temporal deferred behind evidence

**Status:** accepted · 2026-07-11 · Blueprint §5 (staged stack)

## Decision
Horizon 1 runs on SQLite (WAL) as both transactional store and durable-execution
substrate: a runner process claims runs and checkpoints every step (contracts/
statemachine.md). No Temporal, Postgres, Redis, or OpenTelemetry. API + runner are
separate processes so restart behavior is real. Localhost-only binding; NO auth
until Horizon 6 (single operator, single machine).

## Why
One operator must be able to run, understand, and repair everything. The G1
requirements (resume-after-restart, idempotent side effects, budgets, approvals)
are satisfiable with careful checkpointing at a fraction of the operational weight.

## Upgrade triggers (each becomes a gate-time check, not a vibe)
- Adopt **Temporal** (`temporal server start-dev`, single binary) when a workflow
  needs multi-day approval pauses surviving restarts under fault injection (G3),
  cross-machine workers, or signal/timer complexity the runner demonstrably fails.
- Adopt **Postgres/pgvector** when concurrent writers exceed WAL comfort or
  governed semantic retrieval lands (Horizon 3).
- Adopt **OTel** at Horizon 6 (production hardening); until then JSONL logs +
  events table are the observability surface.

## Consequences
Multi-process SQLite discipline is mandatory: WAL, busy_timeout=5000, BEGIN
IMMEDIATE for writers, short transactions. The compete review on p02-runner weighs
claim/reclaim concurrency safety heavily.
