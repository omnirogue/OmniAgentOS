# SSE wire contract (FROZEN, Wave 0)

Endpoint: `GET /api/events?after_id=<int>&types=<csv>` → `text/event-stream`.

- Source of truth is the `events` table; `id` (AUTOINCREMENT) is the SSE `id:` field
  and the client cursor. Reconnect with `after_id` (or standard `Last-Event-ID`
  header — server honors either; query param wins).
- Server behavior: on connect, replay rows with id > after_id (capped at 500 most
  recent). If `after_id` is older than the replay window (i.e. more than 500 rows
  behind `latest_event_id`), the FIRST frame is `event: resync` with
  `data: {"latest_id": <int>}` — the client must full-refresh its lists instead of
  trusting the replay (D-010: never silently skip a terminal run.updated). Then
  poll the table every 250ms for new rows; emit `: keepalive` comment every 15s;
  `types` filters on the event `type` column.
- `worker.heartbeat` frames are SYNTHESIZED by the API from the heartbeats table at
  most once per 15s per worker — heartbeats are NEVER events-table rows (D-010).
- Retention: none in H1 (documented follow-up at >1M rows); the smoke suite asserts
  a 10-run flow stays heartbeat-free in the events table.
- Event frame:

```
id: <events.id>
event: <events.type>            # one of contracts.Events.ALL
data: {"id": <int>, "ts": "<iso>", "type": "<type>", "actor": "<actor>",
       "action": "<action>", "target_type": "<t>", "target_id": "<id>",
       "payload": <payload_json parsed>, "trace_id": "<id>"}
```

Payload minimums by type (dashboard relies on these keys):
- run.updated → payload: {run_id, task_id, state, harness, arm}
- step.updated → payload: {run_id, seq, name, status}
- task.updated → payload: {task_id, state}
- approval.requested / approval.decided → payload: {approval_id, run_id, action_class, state}
- pause.changed → payload: {paused, reason}
- audit.event → payload: free-form (action carries the verb)
- worker.heartbeat → payload: {worker_id, current_run_id}

Writers (runner/api/scheduler) MUST populate `type` with a contracts.Events constant —
unknown types are dropped by the dashboard, not errors.

## Event hub operator status (L12 / H-41)

Persistent event-hub/tailer failure is operator-visible:

* `GET /api/health` exposes `event_hub` (see `contracts/backend-realtime-v1.md`) and
  sets top-level `status` to `"degraded"` when the hub is degraded.
* SSE clients may receive synthetic frames:

```
event: eventbus.status
data: {"contract_version":1,"type":"eventbus.status","state":"degraded"|"ok",
       "reason":"...","degraded":bool,"consecutive_failures":int,...}
```

Degraded after 3 consecutive failures; bounded tailer restart after 8 (max 5
restarts). Recovery emits `state:"ok"` / `reason:"tailer_recovered"`.
