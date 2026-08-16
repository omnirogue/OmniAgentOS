# Jira Goals API (additive contract)

Phase JG-1 surface. Full OpenAPI regen for these paths lands at merge (not in
the JG1-BE lane commit); this document is the durable human contract.

Site: `example-team.atlassian.net` (G-A3).
Live project keys: `ACM` · `CA` · `INI` · `HOO` · `OAOS` (G-A2).
Canonical review status name: **In Review** (G-A1 — never bare "Review").

## Routes

### `GET /api/jira/health`

Probe bot credentials via Jira `myself`.

**200**
```json
{
  "ok": true,
  "displayName": "string",
  "accountId": "string"
}
```

**401 / 502** — sanitized error envelope (`error.code`, `error.message`). Response
body and server logs must never contain `JIRA_API_TOKEN` bytes or the
`Authorization` header value.

**503** — credentials not configured (`JIRA_EMAIL` / `JIRA_API_TOKEN` unset).

### `GET /api/jira/projects`

List live Jira projects visible to the bot.

**200** — array of:
```json
{
  "id": "string",
  "key": "string",
  "name": "string",
  "projectTypeKey": "string | null"
}
```

### `GET /api/jira/projects/{key}/statuses`

Statuses for one project, **deduplicated by name**. Consumer: queue edit dialog
status options (U5). Apply-worker per-issue transition discovery remains the
truth check at write time.

**200** — array of:
```json
{
  "id": "string",
  "name": "string",
  "statusCategoryKey": "string | null"
}
```

## Project mapping

### `PATCH /api/projects/{id}`

Accepts optional `jira_project_key` (in addition to existing `parent_project_id`).

```json
{
  "jira_project_key": "ACM"
}
```

- `null` clears the mapping.
- Uniqueness is enforced by the DB partial unique index on non-NULL keys
  (Migration A). Duplicate keys → **409**.
- Multiple projects may have `jira_project_key IS NULL`.

## Explicit non-routes (C7 / C8)

There is **no** public:

- `/api/jira/issue…`
- `/api/jira/search…`
- `/api/jira/jql…`

`JiraClient.search_jql` / `get_issue` are internal-only.

## Client rules (ground truth)

- Search path: **`POST /rest/api/3/search/jql` only** (legacy `/search` removed).
- Pagination: `nextPageToken` + `isLast` only — never invent `total`/`startAt`.
- Retry: GETs (and read-shaped POST search) honour `Retry-After`, ≤4 retries;
  transition / create / comment POSTs are never blindly retried.
- Custom fields: logical names `{department, ai_proposal_state}` via
  `configs/jira_fields.yaml`; raw `customfield_*` refused at the boundary.
- ADF for description/comment bodies; users addressed by `accountId` only.
