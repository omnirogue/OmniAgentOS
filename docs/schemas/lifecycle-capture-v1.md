# lifecycle-capture-v1

Versioned PreToolUse experience envelope written through the existing
`conversations` memory-store interface (migration 031). **No dedicated capture
table** — Phase-2 query schema is deferred; consumers read via conversation
turns with `meta.kind = "lifecycle_capture"`.

## Envelope (JSON in turn `content`)

| Field | Type | Description |
| --- | --- | --- |
| `version` | string | Always `lifecycle-capture-v1` |
| `event` | string | Taxonomy: `tool_call` (future: `prompt`, `result`) |
| `hook_event_name` | string | Claude hook event name (`PreToolUse`) |
| `session_id` | string | Bridge session id when present |
| `tool_name` | string | Tool being invoked |
| `tool_input` | object | Redacted tool arguments |
| `cwd` | string | Working directory from the hook |
| `captured_at` | string | ISO-8601 UTC timestamp |

## Turn metadata (`meta_json`)

| Field | Value |
| --- | --- |
| `kind` | `lifecycle_capture` |
| `version` | `lifecycle-capture-v1` |
| `tool_name` | same as envelope |

## Scope

- `scope_type`: `session`
- `scope_id`: session id (or `unknown`)
- `role`: `system`

## Redaction

Secrets matching api-key / token / password / bearer / PEM private-key patterns
are replaced with `[REDACTED]` before write. Key names that look secret have
their values fully redacted.

## Retention

Same retention as the `conversations` table. Operators may prune by
`meta.kind = 'lifecycle_capture'` and age; no automatic purge in Phase 1.

## Phase-2 U1 consumer query (deferred schema)

```sql
SELECT content, created_at, meta_json
FROM conversations
WHERE scope_type = 'session'
  AND scope_id = ?
  AND json_extract(meta_json, '$.kind') = 'lifecycle_capture'
ORDER BY seq ASC;
```

## Flag

`OMNIAGENTOS_SESSION_MEMORY_CAPTURE_MODE`: `off` | `shadow` | `enforce` (default `off`).
Shadow builds the envelope without writing; enforce appends via `ConversationStore`.
