# D11: On-Call and Incident Ownership

**Status: DRAFT**

**Date:** 2026-08-03
**Owner:** the operator
**Base SHA:** `ebf3846178962053e15761de8bd312e7f425ee72`

## Question

What rota, severity, escalation destination, and acknowledgement deadline should govern incident
response?

## Verified current facts

- Current loop-effect documentation distinguishes a reached authority refusal from an unavailable authority; unavailable evidence is absence, not a successful outcome.
- The operator directives reserve consequential external actions and VPN/provider installation to the operator's approval.
- A0 performed no service probes or live database actions; it cannot establish current on-call readiness.

## Options

Separate unselected the operator choices; each may be left blank or explicitly deferred:

1. Human on-call owner and rota: [                                        ]
   - Defer: [    ]
2. Incident severity definitions: [                                        ]
   - Defer: [    ]
3. Escalation destination and acknowledgement deadline: [                                        ]
   - Defer: [    ]
4. Defer the whole decision and retain current state: [    ]

## Tradeoffs

Single-owner accountability is clear but concentrates response burden. A rotation needs an approved roster and operational evidence absent from this packet.

## Affected packages / Dependent packages

UP-08, UP-08B, UP-13, operational runbooks, loop effects, and future incident tooling.

## Rollback

This unsigned draft changes no alert route, schedule, or live service.

## Exact the operator-only signature field

Approved by the operator on ______________ (Signature: ____________)
