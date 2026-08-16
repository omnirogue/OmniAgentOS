# D1: Branch of Record

**Status: DRAFT**

**Evidence refresh:** `UP-01A` commit `f09d3e981407bd1a7be9a336fe674a75e79f2231`,
`docs/status/gap-01-status-packet-0a29a281.md`.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

Should `main` be the branch of record, and should UP-01B rewrite the identity language in
`SEPARATE-PRODUCT.md` and related status material?

## Verified current facts

- UP-01A's fresh revision-bound packet records that `main` and its producing lane
  resolve to `ebf3846178962053e15761de8bd312e7f425ee72`; the lane is not a competing
  branch-of-record claim.
- The same packet treats `main` as the active canonical branch under the task-authorized
  branch policy, while retaining the operator-only authority for product-identity choices.
- The prior `grok-architecture-integration` wording in `SEPARATE-PRODUCT.md` is an older
  narrative statement, not current ref evidence.
- UP-01A's status packet separates static source facts from runtime claims: configured
  ports, disk migration inventory, and branch references do not establish listener,
  launchd, serving-root, or live-database state.

## Options

1. Declare `main` the sole canonical branch of record and route the identity-document rewrite
   through UP-01B: [                                        ]
2. Retain the current documented branch naming and route any correction through UP-01B without
   declaring a canonical branch: [                                        ]
3. Defer decision and retain current state: [                                        ]

## Tradeoffs

Naming main follows the fresh revision-bound ref evidence but does not decide product identity
or operational state. Deferral makes no identity change and leaves old narrative references
unreconciled.

## Affected packages / Dependent packages

UP-01B, `SEPARATE-PRODUCT.md`, `STATUS.md`, and future identity-document consumers.

## Rollback

This is an unsigned draft; no branch is moved or protected-state changed. A later ruling supersedes it.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
