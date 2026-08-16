# LoopDeck Engine API v1

LoopDeck and OmniAgentOS are separate products. Integration is over authenticated HTTP only; they do not share a checkout, database, runtime directory, or application modules.

All endpoints require `X-Session-Token`. Errors use `{"error":{"code","message","detail"}}`. Consumers must reject an `api_version` other than `loopdeck-engine/v1` unless they explicitly support it.

The normative example is `contracts/fixtures/loopdeck-engine-v1.json`.

## Schema visibility

This router is registered with `include_in_schema=False`, so its routes do not appear in the generated `contracts/openapi.json` artifact or in any inventory derived from `app.openapi()`. That artifact is frozen for this change, and a hidden route cannot drift it.

The cost is that `tests/api/test_auth_posture.py`, which walks the OpenAPI inventory, cannot see these routes. Authentication is therefore asserted **directly** in `tests/api/test_engine_routes.py` (`test_capabilities_requires_auth`: an unauthenticated read returns 401 `unauthorized`). This is a compensated blind spot and must stay compensated: any route added to this router needs its own direct auth assertion.

## Discovery

- `GET /api/engine/capabilities` returns the version, read-only flag, capability flags, and relative links. It never returns host paths, ports, tokens, or credentials.

## Run binding

- `GET /api/engine/runs?limit=20` returns `{"runs":[…]}`, newest first by `created_at`, each row projected through the run allow-list. `limit` is bounded to 1..50 (default 20); out-of-range values are rejected 422 `validation`.

This list is the authoritative binding source for a consumer: it picks a `run_id` from here and holds it. There is no binding table and no inference of a run from a project or task identifier.

## Snapshot

- `GET /api/engine/runs/{run_id}/snapshot?after=0&limit=200` returns an allow-listed run projection: tasks, dependencies, attempts, progress, metrics, cursor activity, artifact metadata, plus `context`, `evidence`, and `approval`.

`run_id` must match `^[A-Za-z0-9_.-]{1,64}$`; a malformed identifier is rejected 422 `validation` **before** any data-access call, so it can neither reach the store nor be echoed back. `after` is the last consumed numeric event ID; the next request uses `next_activity_cursor` and consumers deduplicate by event ID.

Each snapshot is strictly scoped to one `run_id` — evidence from another run is never merged in. Consumers must discard accumulated evidence when their bound `run.id` changes; the server cannot do this for them because it is stateless per request.

### `context`

`{"repository", "branch", "head_sha"}`, read from the run's `plan_json` and falling back to `metrics_json`. Each field is validated independently and becomes `null` if it fails; an invalid value is never echoed raw.

| Field | Rule |
|---|---|
| `repository` | `^[\w.-]+/[\w.-]+$` |
| `branch` | `^[\w./-]{1,255}$`, no `..`, no leading or trailing `/` |
| `head_sha` | `^[0-9a-f]{7,40}$` |

Malformed `plan_json`/`metrics_json` yields all-`null` context, never a 500.

### `evidence`

`{"commits", "files", "tests", "reports"}`, accumulated from every event in the fetched window whose `action` is `evidence.reported`. Payloads are partial and may overlap between polls, so entries are deduplicated by identity — commits by `sha`, files by `path`, tests and reports by `name` — keeping the **first** occurrence, its values, and its position.

Any `url` on an entry that does not begin `https://github.com/` is set to `null`; the entry itself is kept, so the evidence still shows but renders as inert text rather than a navigable link.

### `approval`

`{"approved": false, "receipt": null}` unless an event with `action == "approval.recorded"` names this `run_id` — and, when `context.head_sha` is set, the same `head_sha`. Only then does `approved` become `true` and `receipt` carry the recorded `reviewer`/`run_id`/`head_sha`.

**Approval is never inferred.** Artifacts, review documents, and passing tests are evidence, not consent, and must never flip this field.

### Redaction

Event payloads and database rows are projected through fixed allow-lists so future columns cannot become accidental API fields. Artifact metadata intentionally omits `content_uri`; run rows omit `working_dir`.

Additionally, every string projected anywhere in a response passes a redaction filter that replaces token-like substrings (`sk…`, `ghp_…`, `gho_…`, `ghs_…`, `github_pat_…`, `xox…`, `AKIA…`, `Bearer …`) and host paths (`/home/…`, `/Users/…`, `/tmp/…`) with `[redacted]`. This covers operator-authored free text — commit messages, report names — that no allow-list can vet.

## Existing command surface

Run creation and operation remain authoritative on the existing swarm API:

- `POST /api/swarm` with `{"brief": string, "working_dir": string, "project_id"?: string, "budget_usd_max"?: number, "params"?: object}` returns HTTP 202 and a creation job by default.
- `GET /api/swarm/jobs/{job_id}` returns `running`, `ready` (with `swarm_run_id`), or `error`.
- `GET /api/swarm/{run_id}` and `GET /api/swarm/{run_id}/activity` remain available for full first-party detail.
- `POST /api/swarm/{run_id}/cancel` is idempotent.

The LoopDeck server, never its browser client, maps a repository identifier to an allow-listed checkout and supplies `working_dir`. A client must not blindly resubmit after losing a creation job response because v1 does not define a durable idempotency key for creation.

Memory/reflection use `/api/metacog/*`; governed improvements use `/api/improvements/*`. Memory promotion and improvement decisions are explicit governed actions, not automatic run-completion effects.

## Reserved run event stream (disabled in v1 runtime)

The additive future route `GET /api/engine/runs/{run_id}/events` is reserved but
MUST NOT be advertised as enabled until its authenticated implementation lands.
Its durable cursor is the existing numeric event ID; `after_id` takes precedence
over `Last-Event-ID`, overlaps are deduplicated by event ID, and synthetic
heartbeat/session frames never advance the cursor. Event data uses the versioned
`omniagentos.event/v1` envelope and the same strict redaction boundary as the
snapshot.
