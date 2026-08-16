# D3: G3 Security Timing

**Status: DRAFT**

**Draft record:** Date-prefixed canonical path for this packet.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

May fail-closed toolplane authorize-path hardening with store-backed grant proof proceed while
broader G3 broker work is parked?

## Verified current facts

- A0 says capability-spine changes require selected future manifests to be re-audited at their wave opening.
- A0 requires semantic replay against current security, namespace, execution, locking, and factory seams for loops-phase2.
- The 2026-08-01 gap analysis is historical evidence, not a current security certification.
- The governing plan records that the toolplane `_connector_invoke` seam used caller-supplied
  grant state and a bare approval token at the reviewed base, while broker store-backed proof is
  a different hardening path. UP-06 is therefore blocked on this ruling; if it remains blocked,
  UP-16 must record the toolplane class as `blocked-on-ruling`, not covered.

## Options

1. Proceed with fail-closed toolplane grant-store hardening independently of G3 broker work: [                                        ]
2. Bundle toolplane hardening with the broader G3 broker work: [                                        ]
3. Defer decision and retain current state: [                                        ]

## Tradeoffs

Independent hardening narrows the toolplane/store-backed grant seam while broader G3 remains
parked. Bundling preserves one larger security change boundary but delays UP-06. Deferral keeps
UP-06 closed and limits UP-16 to a blocked-on-ruling accounting of this class.

## Affected packages / Dependent packages

The toolplane/broker authorize seam, UP-06, and UP-16 closure accounting.

## Rollback

No implementation or timing authority is created by this unsigned draft.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
