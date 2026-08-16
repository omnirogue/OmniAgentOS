# Backend realtime / origin / organization contracts (v1)

**Lane:** L12 — Backend Realtime and Route Truth
**Contract version:** `1`
**Consumers:** L13 dashboard adapters and EventSource clients
**Producers:** `omniagentos/api/main.py`, `omniagentos/api/eventbus.py`, `omniagentos/api/routes/org.py`, `omniagentos/revenue/report.py`

This document freezes the backend surfaces L13 must consume. Machine-readable
fixtures live in `contracts/fixtures/backend-realtime-v1.json`.

---

## 1. CORS / product origins (C-08)

### Resolution

| Priority | Source | Behavior |
|---|---|---|
| 1 | `OMNIAGENTOS_CORS_ORIGINS` | Comma-separated full origins; each validated; invalid entries dropped |
| 2 | Dashboard port | `OMNIAGENTOS_DASH_PORT` / `OMNIAGENTOS_DASH_PORT` / default **3001** → `http://127.0.0.1:<port>` and `http://localhost:<port>` |
| + | `OMNIAGENTOS_CORS_EXTRA_PORTS` | Optional extra loopback ports (e.g. `3011`) appended after validation |

### Validation rules (fail closed)

- Scheme: `http` or `https` only
- Host: `127.0.0.1` or `localhost` only
- No path, query, fragment, or userinfo
- Wildcard `*` is **never** accepted
- `allow_credentials` is always `false` (never combined with wildcard)

### Python helpers

```python
from omniagentos.api.main import allowed_cors_origins, validate_cors_origin, cors_middleware_kwargs
```

Canonical product origin default: `http://127.0.0.1:3001` (and localhost twin).

---

## 2. Event hub status (H-41)

### Operator-visible health

`GET /api/health` includes:

```json
{
  "status": "ok" | "degraded",
  "event_hub": {
    "contract_version": 1,
    "state": "ok" | "degraded" | "unknown",
    "degraded": false,
    "consecutive_failures": 0,
    "last_error": null,
    "last_failure_at": null,
    "last_success_at": null,
    "tailer_alive": false,
    "tailer_restarts": 0,
    "max_tailer_restarts": 5,
    "subscriber_count": 0,
    "degraded_after_failures": 3,
    "restart_after_failures": 8
  }
}
```

`status` is `"degraded"` when `event_hub.degraded` is true.

### SSE degraded notice

On enter/exit degraded (and on bounded tailer restart), the hub fans a frame that
the events stream emits as:

```
event: eventbus.status
data: {
  "contract_version": 1,
  "type": "eventbus.status",
  "state": "degraded" | "ok",
  "reason": "persistent_tail_failure" | "tailer_recovered" | "tailer_restart_<n>",
  "degraded": true | false,
  "consecutive_failures": <int>,
  "last_error": <string|null>,
  "tailer_restarts": <int>,
  "max_tailer_restarts": <int>,
  "ts": <unix float>
}
```

### Recovery policy

- Enter degraded after **3** consecutive tick/sample failures
- Attempt tailer restart after **8** consecutive failures
- Cap automatic restarts at **5** (then remain degraded until a successful tick)
- A requested heartbeat/session/event source being unavailable is a sample failure;
  session-reader resolution is retried without requiring a browser reconnect
- A complete successful tick clears degraded and `last_error`, then emits
  `state: "ok"` / `reason: "tailer_recovered"`

Programmatic: `EventHub.status()`, `EventHub.is_degraded()`.

---

## 3. Organization agent-request approval (H-42)

### `POST /api/org/agent-requests/{id}/approve`

Request body (required):

```json
{ "decided_by": "<operator id>" }
```

#### Success (202)

- Request status becomes `designing`
- Event emitted: `type=org.updated`, `action=agent_request.approved`
- Response: `{ id, description, status, requested_by }` with `status=designing`

#### Spawn failure (503) — fail closed

When the design-agent subprocess cannot be started:

- Request status becomes `failed`
- `design_json` includes `{ "error": "...", "phase": "spawn" }`
- Event emitted: `type=org.updated`, `action=agent_request.spawn_failed`
- **No** `agent_request.approved` event is emitted
- Error envelope:

```json
{
  "error": {
    "code": "spawn_failed",
    "message": "design agent subprocess could not be started",
    "detail": {
      "id": "<request_id>",
      "status": "failed",
      "error": "design agent spawn failed: ..."
    }
  }
}
```

Agent request statuses: `pending | designing | awaiting_approval | approved | created | rejected | failed`.

---

## 4. Project tree route ownership (L-01)

| Method | Path | Owner |
|---|---|---|
| `GET` | `/api/projects/tree` | `omniagentos.api.routes.hierarchy.project_tree` only |

Exactly one GET registration. The flat projects router must not re-register `/tree`.
`hierarchy_router` is included **before** `projects_router` so the literal path
wins over `/api/projects/{project_id}`.

---

## 5. Revenue data-quality undercount (L-05)

When a Stripe fact has `meta.truncated == true`, `build_revenue_report(...).data_quality`
**must** include a warn note of the form:

```
{vertical} Stripe: charge pagination was truncated on {day} — revenue is an UNDERCOUNT.
```

This note is independent of `payment_failures` (a truncated day with zero failures
still surfaces the undercount).

---

## Handoff to L13

1. Point dashboard CORS / EventSource origin at the product origin list above (default `:3001`).
2. Treat `event: eventbus.status` and `health.event_hub` as first-class degraded signals.
3. On agent-request approve, handle 503 `spawn_failed` and never treat spawn failure as success.
4. Call `GET /api/projects/tree` only (single owner).
5. Render revenue `data_quality` notes that contain `UNDERCOUNT` as warnings.
