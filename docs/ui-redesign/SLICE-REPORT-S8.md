# S8 — Connections Control Center · Slice Report

**Slice:** S8 — Connections control center (greenfield)
**Owner directive:** build a Stripe-sleek AI-agent-command-center panel of every
integration, using real brand logos and organized by category. Dramatically
increase usefulness, ease of use, and speed.

## Files owned (all new — no edits to shared files)

### Backend

- `omniagentos/connectors/registry.py` — declarative integration catalog + vault
  parser. 35 integrations seeded from the operator's real stack, grouped into
  the 9 specified categories. Vault parser reads ONLY key NAMES via
  `^[A-Z0-9_]+(?==)` — values are never captured into memory. Falls back to
  `status="error"` for every integration when the vault is unreadable; never
  crashes. Multi-instance integrations (PiedPiper, Meta Ads, PayPal) model their
  sub-accounts as `(label, key_prefix)` tuples with per-instance status chips.
  Legacy connector-store grants overlaid read-only if the store is reachable.

- `omniagentos/api/routes/connections.py` — `GET /api/connections` returning the
  pinned contract shape. Exports `router` prefixed `/api/connections`.

### Backend tests

- `tests/connections/__init__.py`
- `tests/connections/test_registry.py` — pytest suite covering: vault-parser
  key-name-only extraction, comment/blank-line handling, unreadable-vault
  graceful fallback, single-instance connected/configured/not_configured rollup,
  multi-instance per-instance and rollup semantics, category ordering fidelity,
  catalog stable-id spot-check, summary counts, DTO contract shape for the API,
  vault-missing graceful path, and a critical test asserting secret values
  never appear in the response body.

### Frontend

- `dashboard/src/app/connections/page.tsx` — `/connections` page. Single-fetch,
  skeleton-on-load, instant client-side filter. Header: "Connections" title +
  "X of Y services connected" summary chip + search <Input>. Category sections
  in the operator's preferred scan order, each a Card grid of tiles. Vault-error
  banner surfaces if every integration reports `error`. `EmptyState` for
  no-integrations and no-matches paths; `ErrorState` with retry for API
  failure.

- `dashboard/src/features/connections/types.ts` — TypeScript mirror of the
  pinned API contract.

- `dashboard/src/features/connections/connectionsApi.ts` — `fetchConnections()`
  with 8s timeout, same-origin GET. `NEXT_PUBLIC_USE_CONNECTIONS_FIXTURES`
  short-circuits to the full-fixture response for dev without an API.

- `dashboard/src/features/connections/fixtures.ts` — the full catalog rendered
  as "all connected" for frontend iteration.

- `dashboard/src/features/connections/logic.ts` — pure functions:
  `filterConnections` (name/id/instance/category match, case-insensitive),
  `statusBadgeTone` (status→semantic-tone map driving ds-badge),
  `statusSummaryLabel`, `flattenIntegrations`.

- `dashboard/src/features/connections/logic.test.ts` — vitest unit tests for
  filter/summary/rollup logic.

- `dashboard/src/features/connections/brandIcons.tsx` — inline-SVG brand
  glyphs for 24 known integrations (Anthropic, OpenAI, xAI, Gemini, Kimi,
  OpenRouter, Together, Mistral, Replicate, PiedPiper, Gmail, Slack, Telegram,
  Meta, Stripe, PayPal, ElevenLabs, Pexels, Pixabay, Cloudflare, AWS, RunPod,
  HuggingFace, Teller) plus a `plug` fallback for unknown ids. Brand-accurate
  colors on the SVG glyphs only — tile chrome stays neutral per the Design
  Charter.

- `dashboard/src/features/connections/Tile.tsx` — a single integration tile.
  Logo + name + detail + status Badge + instance chips (multi-instance) +
  docs link. Keyboard-accessible (Enter/Space → open detail).

- `dashboard/src/features/connections/DetailDialog.tsx` — uses the design
  `Dialog` + `Badge` + `Button` primitives. Surfaces logo, name, status,
  the "what this unlocks" one-liner, per-instance rollup, and a docs
  button. Family labels only — no env-var names, no secret values.

- `dashboard/src/features/connections/connections.module.css` — per-feature
  CSS Modules using `var(--ds-*)` tokens only; zero inline styles.
  Stripe-like card density, subtle 120ms border-color transition on hover,
  shimmer skeleton during load.

- `dashboard/src/features/connections/index.ts` — public feature surface.

## Pinned contracts consumed

```
GET /api/connections
{
  "categories": [{
    "id": str, "label": str,
    "integrations": [{
      "id", "name", "logo",
      "status": "connected"|"configured"|"not_configured"|"error",
      "instances": [{"label", "status"}],
      "detail": str, "docs_url": str|null
    }]
  }],
  "connected_count": int, "total_count": int
}
```

## Pinned contracts emitted

Same as above. Backend is the single source; frontend types mirror it exactly.

## Design-charter compliance

- **Primitives only**: `Page`, `PageHeader`, `Card`, `Badge`, `Input`, `Dialog`,
  `Button`, `EmptyState`, `ErrorState`, `Icon` — all from `@/design`.
- **Zero inline styles in feature code**: every layout decision lives in
  `connections.module.css` using design tokens.
- **One accent**: accent only in the search focus ring; everything else
  uses semantic tones (ok/running/neutral/warn) on badges.
- **Typography**: uses `--text-h3` / `--text-body` / `--text-small` /
  `--text-micro` — no new sizes.
- **Density & whitespace**: 4px token grid throughout; 9.5rem tile height
  reserve; `auto-fill, minmax(15rem, 1fr)` reflow grid.
- **Motion**: 120ms border-color transition on tile hover only; shimmer
  animation during load (no spinners in empty space).
- **EmptyState/ErrorState**: both paths present (`EmptyState` for "no
  integrations" + "no matches"; `ErrorState` w/ retry for API failure;
  vault-error banner for all-error paths).
- **Speed**: single fetch, skeleton on load, instant client-side filter
  (no re-fetch), no layout shift.
- **Keyboard-first**: each tile is `role="button"` with tabindex=0; Enter /
  Space opens the detail dialog; Esc closes the dialog from the primitive.

## Security invariants

- The vault parser regex `^[A-Z0-9_]+(?==)` is captured-only — values are
  never read as strings, never logged, never returned.
- The response body is an inventory of *names and statuses*, not credentials.
- One test explicitly asserts a known-secret value does not appear anywhere
  in the response body raw bytes.
- Docs URLs are the only external strings surfaced; all other strings are
  hand-authored labels.

## Hand-off notes

1. **Router registration (Kimi's scope)**: Kimi must add one line to
   `omniagentos/api/main.py`:
   ```python
   from omniagentos.api.routes.connections import router as connections_router
   app.include_router(connections_router)
   ```
2. **Nav entry (Kimi's / S7's scope)**: the sidebar must gain a `/connections`
   entry. The page uses `<Page>` / `<PageHeader>` like every other route —
   no nav-specific plumbing required. Icon suggestion: `plug` from
   `@/design/Icon`.
3. **Command-palette route (Kimi's / S7's scope)**: add a `/connections`
   command so ⌘K can reach it.
4. **Vault file**: operator should create
   `~/.config/omni/connections.env` with their `KEY=value` pairs, or set
   `OMNIAGENTOS_CONNECTIONS_VAULT` to a different path.
5. **Frontend fixture**: set `NEXT_PUBLIC_USE_CONNECTIONS_FIXTURES=true` to
   render the full catalog during dev without the API.
6. **Test ladder**: `uv run pytest -q tests/connections` + `cd dashboard &&
   npx vitest run src/features/connections/logic.test.ts`.

## Risks / known assumptions

- **Legacy connector-store overlay is best-effort**: if `get_capability_store`
  fails or the connector id isn't in the legacy registry, the overlay
  silently returns zero. The integration still renders from the catalog
  data alone.
- **Instance prefix matching**: the vault parser treats `ACMEUNI_PIEDPIPER_*` as
  belonging to the "AcmeUni" PiedPiper instance. Renames in the vault file will
  break instance rollup (the integration still reports as "configured"
  or "not_configured" — it just loses the per-instance detail).
- **No SSE / no live updates**: the connections panel is a single-fetch
  snapshot. If the operator changes the vault file, they must reload the
  page. SSE streaming wasn't in scope.
- **PiedPiper multi-instance is the dominant multi**: 3 instances seeded (AcmeUni,
  INITECH, GLOBEX). PayPal and Meta Ads are also modeled as
  multi-instance for parity with PiedPiper's brand structure; add/remove instances
  in the catalog as the operator's real setup differs.

## Verification commands (for Kimi to run after merge)

```bash
# backend
uv run pytest -q tests/connections

# frontend
cd dashboard && npx vitest run src/features/connections/logic.test.ts

# e2e smoke (after router + nav wiring)
cd dashboard && npm run build
# boot API on :8484, dashboard on :3000, play the /connections route
# curl http://localhost:8484/api/connections | jq .
```
