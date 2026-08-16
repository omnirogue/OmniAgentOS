# D10: Counterfeit Counting Rule

**Status: DRAFT**

**Evidence refresh:** `UP-09` commit `0260512c0780879f4dfc2858087ddbdfd88da8a7`,
`tests/counterfeits/COUNTING-RULE.md`.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

What dynamic accounting and loader constraints should govern the counterfeit corpus?

## Verified current facts

- UP-09 records that the corpus total is `len(load_corpus())` after every eligible manifest
  parses and validates; partial or invalid loads have no valid repository-wide total.
- At the c404 base, its loader snapshot is 29 legacy entries plus 55 eligible fragment entries,
  for 84. After UP-07 contributes one valid entry without changing another, the Batch-A
  expectation is loader-derived 85, not a universal constant.
- The current loader accepts direct regular `.toml` children in filename order and follows
  symlinks through `is_file()`. It permits blank-string `must_fail` and a blank `failure_re`;
  the following options are proposed policy tightening, not a false claim about current loading.

## Options

Each element below is proposed only and unselected:

1. Require eligible direct regular non-symlink TOML files: [                                        ]
2. Adopt a dynamic corpus count: [                                        ]
3. Require nonblank `must_fail` members: [                                        ]
4. Require a nonblank failure regex: [                                        ]
5. Defer decision and retain current state: [    ]

## Tradeoffs

Dynamic loading avoids a stale fixed numeral; 85 is a stated post-UP-07 Batch-A expectation
only. Tighter file, `must_fail`, and regex rules change current loader behavior and need a
separately scoped implementation rather than this draft.

## Affected packages / Dependent packages

`tests/counterfeits/harness.py`, `tests/counterfeits/test_gate.py`, UP-09, and any aggregate counterfeit accounting.

## Rollback

This draft changes neither corpus entry nor loader behavior.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
