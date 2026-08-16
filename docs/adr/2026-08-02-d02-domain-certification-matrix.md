# D2: Domain Certification Matrix

**Status: DRAFT**

**Draft record:** Date-prefixed canonical path for this packet.

**Evidence refresh:** `UP-04` commit `64ab1b7a055f34650fbf794a52a35fe2f3aa21ba`,
`UP-05` commit `852b884f7b7c7b0519d4e2946095466eb06bbca6`, and `UP-10` commit
`847aa47a8e4974e6500f2a6672f69df853ce020c`; the final UP-09 counting-rule input is
also now available to D10.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

Which domains and suites, thresholds, and speed statistic should govern the future
certification matrix, and what is the disposition of the OmniSwarm ranking residual?

## Verified current facts

- A0 removes UP-02 and UP-02B from future scope because they are already landed; no other
  package is fully satisfied.
- UP-04 finds that TaskContract version parsing accepts positive future versions unchanged;
  swarm is the only adoption path and its bridge/handoff behavior is fail-soft. Role packs
  are implemented in swarm but default to `off`; the reviewed runner, orchestrator, sessions,
  chats, loops, recovery, and CSI surfaces have no TaskContract boundary.
- UP-05 maps distinct durable authorities rather than one central state owner: core runner,
  swarm, graph, routines, and capability/grant paths each have their own stores and transition
  seams. Its pure `swarm.barriers` predicates are not a unified live scheduler decision chain;
  graph recovery and cross-lane terminal truth remain gaps.
- UP-10 refutes a generic unknown-outcome approval theory and records that its scoped change
  makes money/customer writes park for bounded and unresolved targets while bank writes remain
  refused. Inbound provenance and rendered-content revalidation are explicitly handed to
  UP-16 and UP-20.
- UP-09's final counting rule is now available to D10; the matrix does not turn that loader
  evidence into a choice of domains, thresholds, statistic, speed, or OmniSwarm disposition.

## Options

Four independent unselected the operator choices; each may be left blank or explicitly deferred:

1. Domains and suites in the certification matrix: [                                        ]
   - Defer: [    ]
2. Per-domain PASS threshold: [                                        ]
   - Defer: [    ]
3. Statistic, change-set, and speed threshold: [                                        ]
   - Defer: [    ]
4. OmniSwarm ranking residual — carry / reject / defer: [                                        ]

## Tradeoffs

The refreshed audits distinguish bounded source findings from operational claims. Each axis is
independent so a suite choice does not imply a statistic, threshold, or OmniSwarm disposition.
UP-09 supplies loader evidence for the counterfeit domain; this unsigned draft still sets no
matrix result.

## Affected packages / Dependent packages

UP-17, UP-04, UP-05, UP-09, UP-10, UP-11, UP-12, UP-16, UP-20, and later planning coordination.

## Rollback

Unsigned drafts confer no certification status; a signed superseding ADR is required for a changed matrix.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
