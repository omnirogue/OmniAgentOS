# Control-plane API (FROZEN, Wave 0)

FastAPI app `omniagentos.api:app`, bind 127.0.0.1:8485 (contracts.API_HOST/PORT).
CORS (L12 / C-08, see `contracts/backend-realtime-v1.md`): product-scoped validated
allow-list from `OMNIAGENTOS_CORS_ORIGINS` or `OMNIAGENTOS_DASH_PORT`/`OMNIAGENTOS_DASH_PORT`
(default **3001** → `http://127.0.0.1:3001` and `http://localhost:3001`). Never `*`
with credentials (`allow_credentials=false`). No auth in H1 (localhost only;
ADR-001). Persistence ONLY via contracts.Store; state-write ownership per
contracts/statemachine.md ("Ownership of state").
All request/response bodies are JSON built from `omniagentos/contracts.py` models;
errors use the envelope `{"error": {"code": ErrorCode, "message": str, "detail": any|null}}`
with conventional status codes (404 not_found, 409 invalid_state/conflict,
422 validation, 423 paused, 403 policy_denied).

| Method & path | Body → Response | Notes |
|---|---|---|
| GET /api/health | → {status:"ok", version, db:bool, worker:{alive:bool, last_beat_at}} | worker liveness from heartbeats |
| GET /api/pause | → {paused:bool, reason, updated_at} | |
| PUT /api/pause | {paused:bool, reason?} → same as GET | emits pause.changed |
| GET /api/disciplines | → [{id,name,status,created_at}] | |
| POST /api/disciplines | {id,name,metric_contract?} → discipline | 409 on dup |
| POST /api/tasks | {title, discipline_id?, input?, acceptance?, risk?, tools_allowed?} → task (state=ready) | tools_allowed stored in input_json.tools_allowed |
| GET /api/tasks?state=&discipline= | → [task] | |
| GET /api/tasks/{id} | → task + its runs (summaries) | |
| POST /api/tasks/{id}/runs | {harness: HarnessType, arm?: Arm, model?, plan?: [stepSpec], budget?: BudgetSpec, prompt?} → run (state=queued) | default plan if omitted: [agent] ONLY (ledger/vault are runner finalization, D-007); rejects plans with a validate step before a non-validate step (422); emits run.updated; enqueueing while paused is allowed — execution waits |
| GET /api/runs?state=&task_id=&arm=&harness= | → [run summary] | summary = id,task_id,state,harness,arm,model,agent,queued_at,started_at,finished_at,cost_usd,usage_estimated |
| GET /api/runs/{id} | → run (full row) + steps[] + events[] (this run) + artifacts[] + approvals[] + receipts[] (from the idempotency table, D-004) | the run-detail screen contract |
| POST /api/runs/{id}/cancel | {} → {ok:true} | sets runs.cancel_requested=1 only (runner owns the transition); 409 if terminal |
| GET /api/approvals?state=pending | → [approval] | |
| POST /api/approvals/{id}/decision | {decision:"approved"|"rejected", note?, decided_by?} → approval | 409 if not pending; emits approval.decided |
| GET /api/budgets | → [budget] | |
| GET /api/events?after_id=N&types=a,b | SSE stream | contracts/events.md |
| GET /api/ledger?limit=&run_id= | → [RunManifest] | reads JSONL via omniagentos/ledger |

Conventions: ALL ids via contracts.new_id(prefix) — tsk/run/apr/art (D-002);
timestamps via contracts.utc_now_iso(); every mutation writes an events row with
actor="api"; list endpoints ordered newest-first, `limit` default 100 max 500.
PUT /api/pause only writes the pause row + event — re-queuing PAUSED runs on unpause
is the RUNNER's job (D-008).
