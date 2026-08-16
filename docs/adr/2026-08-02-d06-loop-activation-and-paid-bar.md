# D6: Loop Activation and Paid Bar

**Status: DRAFT**

**Draft record:** Date-prefixed canonical path for this packet.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

What weak-oracle eligibility and paid-loop bar should be retained or tightened for the already
shipped loop, rather than authorizing initial paid re-entry?

## Verified current facts

- Current loop documentation treats the durable row as authority and requires a merge gate including counterfeit verification.
- A0 requires loops-phase2 semantic replay against current execution, locking, security, and factory seams; no loop replay is authorized by this draft.
- Reachability exemptions tied to JG4 are superseded as a source ref, not a waiver for future paid effects.

## Options

Four explicit independent unselected the operator choices:

1. Loop activation — retain / tighten / defer: [                                        ]
2. Weak-oracle criteria, or defer: [                                        ]
3. Per-tick ceiling — retain `$1` / lower / defer: [                                        ]
4. Daily cap — retain `$50` / lower / defer: [                                        ]

## Tradeoffs

Stronger preconditions reduce duplicate-effect risk; holding inactive defers loop value but preserves the paid-action boundary.

## Affected packages / Dependent packages

The shipped paid-loop bar, UP-18, UP-19, and future paid-effect integration.

## Rollback

This draft activates no loop and authorizes no charge or customer action.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
