# Slice Report — S2: Models API

## What was built

The `/api/models` endpoint — single source of truth for every model picker in
the dashboard (chat composer, per-message override, spawn dialog). Aggregates
the cascade ladder from `configs/cascade.yaml` with provider-account health
from the existing `claude_accounts` store and returns a flat list the frontend
renders directly.

## Files changed

| File | Status | Purpose |
|---|---|---|
| `omniagentos/modelintel/catalog.py` | **new** | Catalog aggregator: cascade loader + provider health + fuse |
| `omniagentos/api/routes/models.py` | **new** | FastAPI route: `GET /api/models` |
| `tests/models_api/__init__.py` | **new** | Test package marker |
| `tests/models_api/test_catalog.py` | **new** | 28 tests covering all acceptance criteria |

**Not touched** (per slice ownership):
- `omniagentos/api/main.py` — Kimi registers the router after all slices land
- `omniagentos/api/routes/*` (existing) — read-only reference for style conventions
- `omniagentos/accounts/service.py` — consumed read-only via `list_accounts()`
- `configs/cascade.yaml` — consumed read-only

## API contract

The response matches the pinned contract from FINAL-PLAN.md section B exactly:

```json
{
  "models": [
    {
      "id": "auto",
      "label": "Auto — router decides",
      "provider": "router",
      "tier": null,
      "available": true,
      "lineage": null
    },
    {
      "id": "tier0-gemini-coder",
      "label": "gemini-3.1 (low)",
      "provider": "gemini",
      "tier": 0,
      "available": true,
      "lineage": "gemini"
    }
  ],
  "updated_at": "2026-07-27T..."
}
```

- **First entry is always auto** with `available: true` regardless of config state.
- Each cascade ladder entry becomes one row with the adapter key mapped to a
  provider name (`cli-gemini` → `gemini`, `cli-grok` → `grok`, etc.).
- Providers where every account is disabled, errored, rate-limited, or paused
  get `available: false`; providers with no registered accounts default to
  `available: true` (CLI-only adapters authenticate via config dirs).

## Contracts consumed / emitted

| Direction | Contract | Source |
|---|---|---|
| Consumed | `configs/cascade.yaml` ladder schema | Read-only file |
| Consumed | `accounts.service.list_accounts()` | `omniagentos/accounts/service.py` |
| Consumed | `contracts.default_db_path()` | `omniagentos/contracts.py` |
| Emitted | `GET /api/models` response shape | FINAL-PLAN.md §B |

## Registration

Kimi adds one line to `omniagentos/api/main.py`:
```python
from omniagentos.api.routes.models import router as models_router
# ...
app.include_router(models_router)
```

The router carries its own prefix (`/api/models`) so no prefix arg is needed.
No session-token gate (matches the unauthenticated GET pattern of
`/api/accounts`).

## Graceful degradation

| Failure mode | Behavior |
|---|---|
| `cascade.yaml` missing/empty/malformed | Returns auto entry only |
| `cascade.yaml` has no `ladder` key | Returns auto entry only |
| Ladder entries missing fields (effort, cost_rank) | Parsed with defaults; label omits effort suffix |
| `accounts.service` import fails | All providers default to `available: true` |
| `list_accounts()` raises | All providers default to `available: true` |
| `build_model_catalog()` raises internally | Route catches, returns `{"models": [auto]}` |

## Test coverage

28 tests in `tests/models_api/test_catalog.py`:
- **Cascade loading**: full, empty, missing, malformed, no-ladder-key, non-dict entries
- **Provider health**: all healthy, all unhealthy, mixed, empty, default provider, import failure
- **Catalog building**: full config + healthy, empty config, missing config, partial config, malformed, no-ladder-key
- **Provider availability**: all-unhealthy providers, mixed providers, no-accounts default
- **Auto-entry invariants**: always first, exact contract match
- **Entry shape**: all required keys present with correct types
- **Tier parsing**: from name (`tier0-gemini` → 0), index fallback, no-name fallback
- **Lineage extraction**: gemini, grok, gpt, fable
- **HTTP route**: full config, empty config, missing config, response shape contract, unavailable providers, catastrophic failure fallback

## Integration notes for other slices

- **S3 (Chat frontend)**: `useModelOptions()` can now be a thin fetch to
  `GET /api/models` — no fixture needed once S2 ships.
- **S7 (Shell, IA)**: The model picker is no longer hardcoded in
  `hierarchyHooks.ts:286`; grep for remaining hardcoded model lists after S3 lands.
