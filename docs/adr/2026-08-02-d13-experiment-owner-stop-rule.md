# D13: Experiment Owner Stop Rule

**Status: DRAFT**

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

Who owns an automated experiment, and what stop, budget, or time rule must terminate it?

## Verified current facts

- Historical analysis reports experiment-related counts from 2026-08-01 and explicitly marks them non-current.
- Current planning distinguishes later migration-bearing UP-23 work, which must allocate any ordinal serially when opened.
- This docs-only packet has not observed a live experiment or executed an evaluation.

## Options

Separate unselected the operator choices; each may be left blank or explicitly deferred:

1. Accountable owner of an experiment: [                                        ]
   - Defer: [    ]
2. Mandatory stop, budget, or time rule: [                                        ]
   - Defer: [    ]
3. Defer the whole decision and retain current state: [    ]

## Tradeoffs

Explicit ownership and stop rules make accountability legible but need an implementation owner. Keeping experiments unarmed avoids treating historical activity counts as live readiness.

## Affected packages / Dependent packages

`omniagentos/lab/contracts.py` and future UP-23 experiment work.

## Rollback

This draft starts no experiment and changes no budget.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
