# D5: Engine Authority

**Status: DRAFT**

**Draft record:** Date-prefixed canonical path for this packet.

**Evidence refresh:** `UP-05` commit `852b884f7b7c7b0519d4e2946095466eb06bbca6`,
`docs/audits/state-authority-0a29a281.md`.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

How should execution and state authority be distributed across engines and lanes?

## Verified current facts

- UP-05 confirms a distributed authority map at the base: core runner uses durable runs
  transitions; swarm uses DAL claims, lease generation, and terminal attempt CAS; graph
  completion claims node state through CAS before publishing artifacts; and routines settle
  through their own guarded `finished_at IS NULL` seam.
- Capability authority is separate from campaign-grant authority. The former has immutable
  request envelopes, a terminal decision CAS, current capability state, and append-only grant
  evidence; the latter carries its own approval, bounds, expiry, generation, and action-class
  requirements.
- `ExecutionRef` remains a correlation envelope rather than an execution state machine, and
  integration configuration rejects an LLM stage that claims merge authority.
- `omniagentos.swarm.barriers` supplies pure fail-closed predicates, but UP-05 finds no
  scheduler import or call that makes those predicates one live unified decision chain.
- The refreshed audit names remaining boundaries: no graph sweeper producer, no durable
  cross-lane overall terminal authority, and rendered launchd definitions that do not establish
  loaded state or execution.
- UP-05 identifies a source-truth gap in archi-morning cadence: the install producer and job
  script specify 05:30, while the canonical diagram generator hardcodes 07:05. Restamping the
  generated architecture alone retains that stale label; this is a repository-source
  inconsistency, not a claim about a loaded job or operational liveness.

## Options

Separate unselected the operator choices; each may be left blank or explicitly deferred:

1. Option G — ratify / reject / defer: [                                        ]
2. Authority-map distribution across lanes: [                                        ]
   - Defer: [    ]
3. Execution state — localized `ExecutionRef` versus central-store convergence: [                                        ]
   - Defer: [    ]
4. Barrier-exemption disposition: [                                        ]
   - Defer: [    ]
5. Defer the whole decision and retain current state: [    ]

## Tradeoffs

Distributed authority matches the refreshed source audit and keeps each transition seam
legible. A central or unified design would need a separate owner, transition ordering, durable
receipt policy, and implementation scope; this draft creates none.

## Affected packages / Dependent packages

Core runner, swarm, graph runtime, routines, capability/grant stores,
`omniagentos/contracts.py`, `omniagentos/swarm/barriers.py`, UP-05, UP-12, UP-22, and
future state-authority work.

## Rollback

This unsigned draft moves no state or authority boundary.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
