# Reliability system API (FROZEN 2026-07-23, wave V2)

New, ADDITIVE contract for `omniagentos/api/routes/{reliability,improvements,org,
autonomy}.py` (§9, `docs/architecture/V2-DESIGN.md`). Does not amend
`contracts/api.md` or `contracts/events.md` — those stay Wave-0 frozen. Same
conventions as `contracts/api.md`: JSON bodies, error envelope
`{"error": {"code", "message", "detail"}}`, session-token gate on every route,
`utc_now_iso()` timestamps, `new_id(prefix)` ids (`evt_`, `imp_`, `vot_`, `aud_`,
`sc_`, `org_`, `agt_`, `areq_`). Registration into `omniagentos/api/main.py`
(`include_router`) is integration-wave (W10) work — check that file for whether
these are live before assuming so.

**Dual-token routes** (codex #3): human decision routes — improvement
`approve|reject|apply|rollback` and `PUT /api/autonomy` — require an ADDITIONAL
`X-Autonomy-Token` header (constant-time-compared against
`var/secrets/autonomy-token`, 0600) on top of the normal session-token gate. The
dashboard's server-side proxy injects BOTH tokens for exactly these paths; neither
token ever reaches browser code. All other routes below are session-token-gated
reads/writes only.

**Async 202 pattern** (codex #14): `audit/run` and every improvement decision route
that triggers pipeline work return `202` immediately (CAS transition/row insert,
then a DETACHED `subprocess.Popen(["python","-m","omniagentos.reliability", ...],
start_new_session=True)` worker) — no route ever runs sandbox/judges/apply inline.
The dashboard follows progress via `GET` polling + the SSE events below.

## `reliability.py` — `/api/reliability`

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/reliability/summary | → versioned `reliability-summary.v1` (see below) | measured health tiles; unknown values are `null`, never healthy-looking zeros |
| GET /api/reliability/events?status=&severity=&limit=&offset= | → `[ReliabilityEvent]` (full row: `id,failure_class,severity,signature,occurrence_key,source,ref_type,ref_id,evidence_json,status,recovery_json,improvement_id,audit_id,detected_at,updated_at`) | `limit` 1-500 default 100 |
| POST /api/reliability/events/{id}/ignore | `{}` → updated event (`status:"ignored"`) | 404 if missing; idempotent (already-ignored returns as-is, no duplicate event) |
| GET /api/reliability/audits?kind=&status=&limit= | → `[ReliabilityAudit]` | |
| GET /api/reliability/audits/{id} | → `ReliabilityAudit` | 404 if missing |
| POST /api/reliability/audit/run | `{kind}` → `202 {audit_id, status:"queued", kind}` | `kind` validated against `taxonomy.AuditKind`; 422 if invalid; spawns `python -m omniagentos.reliability audit --once --kind <kind>` |
| GET /api/reliability/scorecards?subject_type=&subject_id=&window=&limit= | → `[Scorecard]` (`id,subject_type,subject_id,window,period_start,metrics_json,computed_at`) | |

### `reliability-summary.v1`

The canonical backend fixture is
`contracts/fixtures/reliability-summary.v1.json`. The response retains the
legacy flattened count/audit/cursor fields, while adding explicit component
state:

- `health`: `healthy | degraded`, with machine-readable `degraded_reasons`.
- `open_events_state`: `current | unavailable`; counts are `null` when the
  aggregate read fails.
- `last_audit_state`: `current | never_run | unavailable`.
- `watch.state`: `never_run | current | stale | last_known_good | unavailable`.
  `watch.heartbeat_at` is the durable state-row update time, distinct from the
  scan high-water mark in `watch.cursor_at`.
- `incidents`: visible component failures. Store-read failures are also
  persisted as critical reliability events when the store remains writable.

## `improvements.py` — `/api/improvements`

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/improvements?status=&origin=&kind=&limit=&offset= | → `[Improvement]` (full row, see `reliability/contracts.py::Improvement`) | |
| GET /api/improvements/{id} | → `Improvement` + `votes:[{id,judge_agent,model_family,verdict,scores_json,reasoning,conditions,created_at}]` | 404 if missing; sandbox/rollback detail is embedded in `sandbox_json`/`rollback_point_id` on the improvement itself |
| POST /api/improvements/{id}/approve | `{decided_by}` → `202 Improvement` (dual-token) | 409 unless status=`awaiting_human`; CAS `awaiting_human→approved`; spawns `apply` worker; emits `improvement.updated` |
| POST /api/improvements/{id}/reject | `{decided_by, reason?}` → `200 Improvement` (dual-token) | 409 unless status=`awaiting_human`; CAS `awaiting_human→rejected`; emits `improvement.updated` |
| POST /api/improvements/{id}/apply | `{decided_by}` → `202 Improvement` (dual-token) | 409 unless status=`approved`; CAS `approved→applying`; spawns `apply` worker; emits `improvement.updated` |
| POST /api/improvements/{id}/rollback | `{decided_by}` → `202 Improvement` (dual-token) | 409 unless status=`monitoring`; CAS `monitoring→rolling_back`; spawns `rollback` worker; emits `improvement.updated` |

Every decision route: 404 if the improvement doesn't exist; 409 with
`{id, current_status}` on a `TransitionConflict` (status changed under us — CAS
per §5b.1, never retried blind by the route).

## `org.py` — `/api/org`

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/org/tree | → `{company, departments:[OrgUnit], teams:[OrgUnit]}` | |
| GET /api/org/agents | → `[Agent]` (`id,name,model,org_unit_id,org_role,title,charter,schedule_json,harness,enabled,vault_note_path,created_at,updated_at`) | |
| GET /api/org/agents/{id} | → `Agent` | 404 if missing |
| GET /api/org/agents/{id}/activity | → runs/events/votes for this agent | |
| POST /api/org/agents/{id}/toggle | `{}` → updated `Agent` (`enabled` flipped) | |
| POST /api/org/agent-requests | `{description}` → `AgentRequest` (`status:"pending"`) | |
| GET /api/org/agent-requests?status=&limit= | → `[AgentRequest]` | |
| POST /api/org/agent-requests/{id}/approve | `{decided_by}` → `202 AgentRequest` | spawns agent-creation worker; emits `org.updated` |
| POST /api/org/agent-requests/{id}/reject | `{decided_by, reason?}` → `AgentRequest` (`status:"rejected"`) | |

## `autonomy.py` — `/api/autonomy`

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/autonomy | → `{global:{mode,max_auto_risk}, departments:[{id,mode,max_auto_risk}], agents:[...], kinds:[...]}` | read-only, NO token required |
| PUT /api/autonomy | `{scope_type, scope_id?, mode, max_auto_risk}` → same shape as one scope entry, `+updated_at` (dual-token — 403 without a valid `X-Autonomy-Token`) | 422 on invalid `scope_type`/`mode`/`max_auto_risk` (must be 0/1/2); emits `autonomy.changed` |

## SSE additive event types (5)

Emitted via the EXISTING `_emit`/`insert_event` mechanism (`omniagentos/api/routes/
control.py::_emit`) — the frozen `contracts.Events` enum is untouched; the Python
side of `_emit` accepts a plain string `type`, so these are valid `events` rows on
the SAME `GET /api/events` SSE stream as Wave-0 types (`contracts/events.md`), just
not enumerated in the frozen `Events.ALL` tuple. Dashboard side: a SEPARATE additive
registry (`dashboard/src/lib/reliabilityContracts.ts`) + dedicated hook
(`dashboard/src/lib/useReliabilityEvents.ts`) — the frozen `EVENT_TYPES`/
`useEvents.ts` contract in `dashboard/src/lib/contracts.ts` is NOT edited (codex
#15).

| Event type | Payload | Emitted by |
|---|---|---|
| `reliability.event` | `{severity, ...}` (event-specific; e.g. `event.ignored` action carries `{severity}`) | detector/watch, `POST /events/{id}/ignore` |
| `improvement.updated` | `{decided_by?, reason?}` — `action` carries the verb (`improvement.approved\|rejected\|applying\|rolling_back\|...`) | every improvement decision route + pipeline stage transitions |
| `audit.completed` | `{kind}` (queued) → richer stats on actual completion | `POST /audit/run`, `audit.py` |
| `autonomy.changed` | `{scope_type, scope_id, mode, max_auto_risk}` | `PUT /api/autonomy` |
| `org.updated` | agent/org-unit id + change kind | agent-request approve, `company/org.py` seed, toggle |

## Notes (human)
