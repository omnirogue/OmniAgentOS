# D4: Self-Improvement Authority

**Status: DRAFT**

**Draft record:** Date-prefixed canonical path for this packet.

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

Should low-risk self-improvement merge and skill-update authority be granted independently of
D3 timing?

## Verified current facts

- The historical gap analysis identifies G23/G34 as a self-improvement defect but expressly says its live-DB counts drift and must be re-measured before current use.
- Current `omniagentos/integration/config.py` rejects configuration in which an LLM stage claims merge authority.
- A0 assigns re-audits of curator and fail-closed surfaces to later package openings.

## Options

1. Grant low-risk self-improvement merge and skill-update authority now, decided independently
   of D3 timing: [                                        ]
2. Tie that authority to the D3 toolplane security timing: [                                        ]
3. Defer decision and retain current state: [                                        ]

## Tradeoffs

Independent authority separates curator/skill-update policy from the toolplane ruling. Tying it
to D3 creates a shared timing dependency. Deferral avoids treating historical counts as current
runtime evidence and leaves UP-16 unable to make a closure claim that depends on this authority.

## Affected packages / Dependent packages

Curator and skills surfaces, self-improvement policy, and UP-16 closure accounting.

## Rollback

This draft does not arm, admit, or publish any self-improvement change.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
