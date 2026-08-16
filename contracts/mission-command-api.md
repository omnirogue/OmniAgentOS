# Mission command-center API (additive, P0-CONTRACT freeze)

Additive contract for the mission command-center surface that lands across
P2–P5. This document freezes **names and ownership** only. Runtime writers,
route wiring, migrations, and OpenAPI regeneration are **out of scope** for
P0-CONTRACT.v1a.

Conventions match `contracts/api.md` and `contracts/reliability-api.md`:

- JSON bodies; error envelope `{"error": {"code", "message", "detail"}}`
- session-token gate on mutating and project-scoped reads
- ids via `contracts.new_id(prefix)`; timestamps via `contracts.utc_now_iso()`
- **reads are authoritative**; SSE frames are invalidation hints

## Shared reliability contracts (Python authority)

Canonical Pydantic models live in the existing shared authorities — not a second
registry:

| Contract | Module | Role |
|---|---|---|
| `ExecutionRef` | `omniagentos.contracts` | Correlation envelope only (`request_id`, `execution_id`, optional company/project/campaign, optional `idempotency_key_hash`, `created_at`). Never an execution state machine. No `executions` table. |
| `EffectiveRoute` | `omniagentos.contracts` | Total route record: role, requested/effective model, lineage, billing provider, **explicit** transport, adapter key, optional effort/profile/price revision, selection reason. Transport is never inferred. |
| `CostObservation` | `omniagentos.contracts` | One provider-call observation (canonical names below). Exact text `0.000003705` must round-trip without float/zero loss. Storage lands later as `provider_call_usage` — this freeze defines the contract only. |
| `CostQuality` | `omniagentos.contracts` | `exact \| estimated \| unknown` (dimension-specific; not mixed with token estimation). |
| `ProviderCallStage` | `omniagentos.contracts` | `planner`, `clarifier`, `planner_retry`, `worker`, `worker_retry`, `reviewer`, `reviewer_retry`, `escalation`, `integrator`, `integrator_retry`. |
| `SwarmPlanDecision` | `omniagentos.swarm.contracts` | Dispositions: `ready`, `needs_clarification`, `impossible`, `policy_denied`, `invalid_plan`, `planner_unavailable`, `draft` (additive 2026-08-10: bounded non-file `resource:<kind>:<name>` ownership — reviewable, never executable). **Only `ready` may carry executable plans**; model validation rejects plan content on every non-ready decision. Non-ready ⇒ zero execution side effects. |

`CostObservation` validates exact decimal text with module-private integer
arithmetic; no public parsing or formatting helper is part of this frozen
contract. Whitespace in exact decimal text is **rejected** (never silently
stripped).

### `CostObservation` fields (canonical)

| Field | Required | Notes |
|---|---|---|
| `call_id` | yes | Caller-generated unique id before network/process launch |
| `request_id` | yes | Allocated before every possible provider call |
| `execution_id` | yes | Allocated before every possible provider call |
| `run_id`, `campaign_id`, `reservation_id`, `task_id`, `attempt_id`, `session_id`, `work_id`, `root_trace_id` | no | Correlation IDs |
| `stage` | yes | `ProviderCallStage` |
| `attempt_index` | yes | Non-negative int |
| `provider`, `transport`, `requested_model`, `effective_model`, `model_lineage`, `billing_provider`, `adapter_key` | yes | Route / call identity (transport never inferred) |
| `provider_request_id` | no | Unique per provider when known |
| `request_state` | yes | Exactly `not_sent \| sent \| indeterminate` |
| `provider_outcome` | no | Free-form outcome label when known |
| `input_tokens`, `output_tokens`, `total_tokens` | no | Nullable; negative rejected |
| `cost_usd_decimal`, `cost_usd_nanos` | quality-dependent | Exact text + integer nano-USD; must reconcile exactly |
| `cost_upper_bound_usd_nanos` | quality-dependent | Conservative upper bound (integer nano-USD) |
| `cost_quality` | yes | `CostQuality` |
| `cost_source` | yes | Provenance string |
| `pricing_revision` | no | Price table / revision pin |
| `created_at` | yes | Observation creation time |
| `settled_at` | no | Settlement time when known |

Quality invariants:

- `exact` requires `cost_usd_decimal` (and reconciled `cost_usd_nanos`)
- `estimated` requires `cost_upper_bound_usd_nanos` and forbids exact cost fields
- `unknown` cannot carry invented exact cost (`cost_usd_decimal` / `cost_usd_nanos` must be null)
- Zero is a fact, never a default for missing/unknown

## Cost DTO (every surface)

```json
{
  "known_usd": null,
  "known_usd_decimal": null,
  "estimated_upper_bound_usd": null,
  "chargeable_usd": null,
  "unknown_call_count": 0,
  "quality": "unknown",
  "safe_to_compare": false
}
```

`quality` ∈ `exact | estimated | mixed | unknown` (aggregate surface DTO per
final plan §4.2 — distinct from per-call `CostObservation.cost_quality`, which
is only `exact | estimated | unknown`). **NULL/unknown never renders as `$0.00`
and never paints green.** Existing float cost columns become projections carrying
`call_id` provenance when the ledger lands.

## Additive mission SSE kinds

Emitted via the existing events table / `GET /api/events` stream as plain string
`type` values — the same additive pattern as reliability V2 and `swarm.event`.
**Frozen `omniagentos.contracts.Events.ALL` is untouched.**

Authority: `omniagentos.contracts.MissionEvents.ALL`

| Kind | Meaning (invalidation signal) |
|---|---|
| `chat.project_binding_changed` | Chat↔project binding changed (including legacy PATCH path) |
| `classification.updated` | Classification axes/revision updated |
| `classification.needs_confirmation` | Classification requires operator confirmation |
| `classification.shadow_compared` | Shadow classifier comparison recorded |
| `context.package_ready` | Context package ready for delivery |
| `context.delivery_failed` | Context package delivery failed |
| `task_contract.created` | Task contract created |
| `task_contract.updated` | Task contract updated |
| `task_contract.would_deny` | Contract evaluator would deny (observe/enforce) |
| `contract_gate.updated` | Contract gate status/evidence updated |
| `contract_budget.updated` | Contract budget usage updated |
| `resource_request.created` | Adaptive resource request created |
| `resource_request.updated` | Resource request status updated |
| `formation.updated` | Formation selection/lifecycle updated |
| `verification.updated` | Verification report updated |
| `receipt.available` | Receipt projection available for read |
| `memory.updated` | Memory/memlife surface updated |

Rejected renames (do not introduce): `verification.lifecycle`, `receipt.updated`,
Gemini `chat.classification.clarification_required`, Backend-Synthesis lifecycle
kinds (`context_resolving`, `gate_passed`, …).

## Additive `swarm.event` group actions

Group lifecycle uses the **existing** `swarm.event` kind and
`SWARM_EVENT_ACTIONS` vocabulary — never a second event-kind registry.

Authority: `omniagentos.swarm.contracts.SWARM_GROUP_EVENT_ACTIONS` (also appended
to `SWARM_EVENT_ACTIONS`):

| Action | Meaning |
|---|---|
| `group_created` | Multi-bundle group row created |
| `group_activated` | Group activation/lease acquired |
| `group_completed` | Group finished successfully |
| `group_failed` | Group finished failed |
| `group_cancelled` | Group cancelled |

Existing swarm actions (`plan_created`, `run_started`, …) are unchanged.

## Planned additive HTTP surface (names frozen; wiring later)

All new GETs require an explicit `AUTHORIZED_READ_PREFIXES` entry in one batched
commit when routes land. The dashboard proxy never default-allows.

| Method & path | Notes |
|---|---|
| `POST /api/chats/{id}/assign` | Binding assign; field `expected_project_id` (CAS) |
| `GET /api/chats/{id}/classification-history` | Binding-decision history |
| `GET /api/orgdims/taxonomy` | Server-owned classification vocabulary |
| `GET/POST /api/orgdims/classifications/{t}/{id}[/resolve\|/history]` | Axis resolution + history |
| `GET /api/context/packages/{id}` | Context package (+items); per-task variants later |
| `GET /api/task-contracts` (+`/{id}`, `/transitions`, `/gates`, `/usage`) | Contract reads |
| `GET /api/intake/formations` | Formation list |
| `POST /api/intake/formation/preview` | Preview + typed override (`reason` required) |
| `GET /api/intake/formation/{selection_id}` | Formation selection detail |
| `GET /api/resource-requests` | Adaptive access requests (over `agent_interactions`) |
| `GET /api/receipts` (+`/{receipt_id}`) | Receipt **projection** only — no receipts table |
| `GET /api/accuracy` | Accuracy projection |
| `GET /api/loops` | Loop composition (writes stay on routines) |
| `GET /api/swarm/groups/{id}` + cancel | Multi-bundle group surface |
| `/api/access/tool-manifest`, `/api/access/tool-call` | Production access plane |

Human decisions remain **`POST /api/approvals/{id}/decision` only** (server stamps
identity). Ambiguity is stateless `409 project_resolution_required` with
candidates — the prompt is not persisted server-side as a clarification machine.

## Dashboard generation

`scripts/gen-dashboard-contracts.py` derives TypeScript declarations from the
Python authorities above. Eventual product output:

`dashboard/src/lib/generated/`

P0-CONTRACT.v1a does **not** write into `dashboard/**`. Tests and CI must pass a
caller-selected temporary output path and assert zero drift. A later phase owns
checking the generated tree into the dashboard under the dashboard path claim.

## Explicit non-goals of this freeze

- No OpenAPI regeneration (`contracts/openapi.json` / `.new` untouched)
- No execution registry / `executions` table / `effective_routes` table
- No second cost ledger and no second event vocabulary
- No runtime writers, route wiring, or migrations
