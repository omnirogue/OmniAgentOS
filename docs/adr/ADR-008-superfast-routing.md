# ADR-008: Superfast routing — gated fast lane with quality escalation

**Status:** accepted · 2026-07-19

## Decision

Add `superfast` as a first-class model-intelligence and Fusion routing mode.
It retains the existing latency-dominant fit-score but introduces a hard stage
before scoring:

1. Apply availability, role, `autoRoute`, capability-tier, and reasoning-floor
   gates.
2. If at least one eligible worker has `fastLane:true`, score only that cohort.
   The initial fast lane is GPT-5.6 Luna and Terra.
3. If no fast-lane worker clears the floors, restore the complete eligible
   auto-route pool. This is a quality escalation, not a routing failure.
4. Grok 4.5 at low effort may propose a task-sensitive pick, but receives only
   the same eligible cohort and its choice is validated against it. Any failure
   falls back to the identical mechanical policy.

The matching `/superfast` Fusion skill supplies the workflow layer: adaptive
effort, genuine parallel packages, conditional worktrees, integrated
validation, lead diff inspection, and fresh premium review on explicit risk,
size, failure, or complexity triggers.

## Why

The older `ultrafast` profile is only a weighted preference. When measured
startup latencies are close, a premium frontier worker can win moderate work
despite a capable smaller model being available. Superfast needs a predictable
fast path without lowering the correctness floor. Cohort gating supplies that
predictability; restoring the full pool when the cohort is empty prevents the
mode from forcing an underpowered model onto architectural work.

## Consequences

- `ultrafast` remains unchanged for backward compatibility.
- Rankings now declare `fastLane`; Luna/Terra ids are compatibility defaults for
  older cache files.
- `autoRoute:false` is enforced in both Grok and mechanical auto paths, keeping
  pin-only premium agents out of automatic selection.
- The Fusion and OmniAgentOS routers must keep mode weights and fast-lane
  semantics in sync.
- A future Grok router upgrade requires actual account availability and a
  routing evaluation; model-name anticipation is not sufficient.
