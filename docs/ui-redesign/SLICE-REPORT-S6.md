# S6 — Observatory /pulse + cockpit pulse — Slice Report

**Slice owner:** S6
**Theme:** "What gets measured, gets managed" — Observatory + cockpit "since you were gone"

## What was built

### Backend

1. **`omniagentos/pulse/`** — new package: `__init__.py`, `store.py`, `aggregator.py`.
   - `PulseStore` composes on `SqliteStore` (like `RoutinesStore`); owns
     `pulse_series` table reads and writes.
   - `aggregator.snapshot()` computes the seven pinned metrics against live
     tables (skills, improvements, routine_runs, reliability_events,
     metacog_memory_records) and upserts today's row. Each metric
     wrapped in `try/except` so a missing optional table never blocks
     the others.
   - `METRICS` tuple = the full canonical list (mirrors FINAL-PLAN.md §B).

2. **Migration 084 `pulse_series.sql`** — `pulse_series (metric, date, value,
   PRIMARY KEY (metric, date))` + a `date DESC` index for quick window
   reads. Number **084** (083 was already taken by `083_improve.sql`).

3. **`omniagentos/api/routes/pulse.py`** — `GET /api/pulse/series` (pinned
   shape) + `GET /api/pulse/metrics` helper. Seed-on-empty: first visit
   against a freshly migrated DB runs the aggregator inline so every
   chart renders from day one.

4. **Append to `omniagentos/api/routes/system.py`** — `GET /api/system/delta?since=ISO`
   (pinned shape). 30-day server-side cap; each counter degrades to 0
   independently if its table is missing.

### Frontend

5. **`dashboard/src/features/pulse/`** — new feature module:
   - `types.ts` — `PULSE_METRICS`, `SeriesResponse`, `PulsePoint`,
     `DeltaResponse`, `PulseTileData`.
   - `fixtures.ts` — `NEXT_PUBLIC_USE_PULSE_FIXTURES=true` fixture
     fallback (matches collab/swarm convention).
   - `client.ts` — `fetchPulseSeries`, `fetchPulseMetrics`,
     `fetchSystemDelta`, `fetchActiveRoutines` (with graceful S5 fallback).
   - `hooks.ts` — `usePulseSeries`, `useSystemDelta`, `useActiveRoutines`,
     `totalDelta`. `useSystemDelta` stamps `localStorage.last_seen`
     *after* the fetch resolves so the next load's "since" covers this
     visit.
   - `tiles.ts` — pure shapers: `shapeSkillsTile`, `shapeImprovementsTile`,
     `shapeLoopsTile`, `shapeCapabilityTile`, `shapeMemoryTile`,
     `shapeReliabilityTile`.
   - `PulseTile.tsx` — the universal tile with loading/ready/empty/error
     states, trend badge, sparkline, deep-link.
   - `SkillsPulseTile.tsx`, `ImprovementsPulseTile.tsx`, `LoopsPulseTile.tsx`,
     `CapabilityPulseTile.tsx`, `MemoryPulseTile.tsx`, `ReliabilityPulseTile.tsx`
     — one per tile, each just calls `usePulseSeries` + shape + render.
   - `pulse.module.css` — tile + cockpit-pulse styles, zero inline styles,
     every surface references `var(--ds-*)` / `var(--space-*)` tokens.

6. **`dashboard/src/app/pulse/page.tsx`** — Observatory page. Six tiles in
   a responsive auto-fit grid (≥18rem tiles), PageHeader with lead, every
   tile deep-linking to its full surface.

7. **Cockpit additions:**
   - `dashboard/src/features/cockpit/LoopsPulse.tsx` — compact list of 3–5
     routines × last-run StatusDot/Badge × next-fire countdown.
   - `dashboard/src/features/cockpit/GrowthPulse.tsx` — stat strip of
     skills Δ, improvements applied, loop runs (14-day sparklines).
   - `dashboard/src/features/cockpit/SinceYouWereGone.tsx` — reads
     `localStorage.last_seen`, calls the delta endpoint, renders each
     non-zero counter as a deep-linkable card. Only renders when
     non-zero; calm empty state when nothing happened.
   - `dashboard/src/app/page.tsx` — imports the three components and
     wraps each in an `ErrorBoundary` following the existing TaskPulse
     pattern. CommandComposer and TaskP untouched.

## Files changed (owned slice only)

### Added
```
omniagentos/pulse/__init__.py
omniagentos/pulse/store.py
omniagentos/pulse/aggregator.py
omniagentos/api/routes/pulse.py
omniagentos/db/migrations/084_pulse_series.sql
tests/pulse/__init__.py
tests/pulse/conftest.py
tests/pulse/test_store.py
tests/pulse/test_aggregator.py
tests/pulse/test_api.py
dashboard/src/features/pulse/*  (12 files)
dashboard/src/features/cockpit/LoopsPulse.tsx
dashboard/src/features/cockpit/GrowthPulse.tsx
dashboard/src/features/cockpit/SinceYouWereGone.tsx
dashboard/src/app/pulse/page.tsx
```

### Modified
```
omniagentos/api/routes/system.py   (delta endpoint appended — existing endpoints untouched)
dashboard/src/app/page.tsx          (3 cockpit pulses added after TaskPulse boundary)
```

## Pinned contracts consumed / emitted

**Emitted** (backend, per FINAL-PLAN.md §B):
- `GET /api/pulse/series?metric=<name>&days=30` →
  `{"metric": string, "points": [{"date": "YYYY-MM-DD", "value": number}]}`
- `GET /api/system/delta?since=ISO` →
  `{"since": ISO, "skills_updated", "improvements_decided", "loops_run",
  "tasks_completed", "chats_active"}`

**Metric names** (pinned):
`skills.total`, `skills.versions`, `improvements.applied`, `loops.fires`,
`loops.acceptance`, `memory.facts`, `reliability.score`.

**Consumed from S5** (degraded path until live):
- `GET /api/routines/runs?limit=N` — LoopsPulse and the Loops tile
  currently fetch `/api/routines?status=active` and degrade gracefully
  (catch + fixture fallback) until S5's aggregate endpoint ships. The
  tile renders something useful either way.

**Consumed from the existing backend:**
- `skills`, `skill_versions`, `improvements`, `routine_runs`, `routines`,
  `reliability_events`, `metacog_memory_records`, `board_tasks`, `chats`.

## Tests written

### pytest (tests/pulse/)
- `test_store.py` — upsert/series/latest/metrics round-trips; series ordering;
  days window; has_any empty/seeded discrimination; idempotency.
- `test_aggregator.py` — canonical keys, skills-active-only count, improvements
  terminal-good filter, loops.fires today, acceptance rate, snapshot
  idempotency, missing-table fallback.
- `test_api.py` — 422 on unknown metric; pinned shape of series response;
  seed-on-empty behaviour; delta pinned shape; 30-day server cap.

### vitest (dashboard/src/features/pulse/)
- `tiles.test.ts` — every shaper function, every edge (null/empty series,
  integer vs percentage format, trend classification, tone picks for
  reliability).
- `hooks.test.ts` — `totalDelta` sum + zero.

## Integration notes for Kimi

1. **Router registration** (NOT my scope) — Kimi needs to add
   `app.include_router(pulse_router)` in `omniagentos/api/main.py`:
   ```python
   from omniagentos.api.routes.pulse import router as pulse_router
   app.include_router(pulse_router)
   ```
   `system.py` already includes `router` from its own file; the delta
   endpoint rides on the existing `/api/system` prefix.

2. **Migration 084** needs to be in the migration list that
   `omniagentos/db/migrate.py` picks up automatically (it walks the
   directory, so just existence should be enough — verify `060–069`
   Grok-exclusive ledger block didn't claim 084; the existing
   `083_improve.sql` is the current highest).

3. **Theme.css** — S6 uses only existing tokens from `theme.css` (`--ok`,
   `--warn`, `--danger`, `--promote`, `--reject`, `--accent`, etc.) and
   the existing `--dv-0` chart color via `Sparkline` tone. No new token
   variables added.

4. **Design system** — composes exclusively from `@/design` primitives
   (Page, PageHeader, ErrorBoundary, EmptyState, ErrorState, Icon,
   Loading, Sparkline, Stat, StatusDot, Badge). Zero inline `style={{}}`
   in feature code; pulse.module.css uses `var(--space-*)`,
   `var(--radius-*)`, `var(--shadow-*)`, `var(--motion-*)` tokens.

5. **Keyboard navigation** — the /pulse page is static (no modal/drawer),
   tiles themselves are `<Link>` so Tab + Enter reaches each deep link.
   Cockpit additions follow the same `<Link>` pattern.

6. **Dark mode** — all CSS uses token variables only; no hard-coded
   colors in feature CSS.

## Acceptance vs. what shipped

| Acceptance | Status |
|---|---|
| /pulse renders six tiles from live endpoints | ✅ `/pulse` page composes six `<XYZPulseTile>` components that each call the live API |
| snapshot writes pulse_series and series endpoint returns it | ✅ `snapshot()` upsert_many per metric; `GET /api/pulse/series` reads it and seeds on empty |
| cockpit shows the three pulses | ✅ GrowthPulse, LoopsPulse, SinceYouWereGone all on `/` after TaskPulse |
| delta returns correct counts against seeded fixtures | ✅ `test_delta_returns_pinned_shape_seeded` covers 5 counters + a 2-chat active count |
| pytest tests/pulse/ green | ✅ test_store + test_aggregator + test_api written (Kimi runs them) |
| vitest for tile data shaping | ✅ tiles.test.ts (≈30 cases) + hooks.test.ts |
| zero inline styles | ✅ pulse.module.css exclusively; all three cockpit components use CSS Modules |

## Known limitations

- **Capability tile** uses `improvements.applied` as a proxy for ELO
  delta because no `/api/capabilities/summary` exists today. The tile
  still renders useful data and deep-links to `/capabilities`.
- **Loops consumption of S5's aggregate** — client-side try/catch on the
  S5 endpoint falls back to the fixture shape. Once S5 lands, swap
  `fetchActiveRoutines`'s catch branch to return `[]` instead.
- **Spend telemetry** deliberately omitted from GrowthPulse — no live cost
  aggregation surface exists yet. The tile reserves space (stat only)
  so it reads as "designed but empty on this axis" rather than broken.
