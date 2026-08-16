# LL-0: Live Lane Disposition

**Status: DRAFT**

**Date:** 2026-08-03
**Owner:** the operator
**Evidence baseline:** `ebf3846178962053e15761de8bd312e7f425ee72`; Wave-0 A0 scope-compression audit, SHA-256 `a355fc8980ce076e88e20281a8f90b99fa5b2d4a00f5a53ff1c4ec8eea356be0`.

## Question

What disposition and replay order should govern the protected LL-0 lane inputs?

## Verified current facts

- A0 records `integration/reach-exempt-first@ae39964c81c…` as superseded: its sole patch is represented on main and later corrected; it must not be merged.
- A0 records `lane/jg4-core@94a6c3690a0f…` as divergent. Its source migration 103 collides with main; replay is candidate-only and must use the then-next free ordinal. A0 observed 114 as currently free, not a reservation.
- A0 records `lane/loops-phase2@7e8c56e44599…` as divergent with three substantive post-baseline commits across 32 files. Its 101/102 migration blobs are byte-identical to main and must neither be renumbered nor reapplied.
- After the operator's LL-0 disposition, the effective later order is `jg4-core -> loops-phase2`. This is not a Wave-A blocker.

## Options

1. Confirm the A0 disposition and order: [                                        ]
2. Defer a protected-lane disposition: [                                        ]

## Tradeoffs

Confirming preserves source refs while allowing a later disposable candidate to replay JG4 before loops. Deferral keeps both divergent inputs closed and does not alter their source worktrees.

## Affected packages / Dependent packages

Protected LL-0 inputs; later JG4 and loops-phase2 replay work. No Wave-A package is authorized by this draft.

## Rollback

This unsigned draft changes no ref or candidate. A changed ruling requires a superseding signed ADR.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
