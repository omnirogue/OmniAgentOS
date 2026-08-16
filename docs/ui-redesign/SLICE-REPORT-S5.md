# S5 — Loops slice report

**Slice id:** S5
**Theme:** Loops (routines, one honest home)
**Owner directive:** Dramatically increase dashboard usefulness, speed, and polish — Stripe/Apple bar. Chat + Kanban + Loops + Observatory + Approvals.

---

## What was built

### Backend

1. **Cross-routine runs aggregate** — `RoutinesStore.list_recent_runs(limit)` in `omniagentos/scheduler/store.py` (joined `routines × routine_runs`). Returns `{routine_id, routine_name, run_id, gate_passed, accepted, cost_usd, finished_at}` newest-first, exactly matching the pinned section B contract.
2. **`GET /api/routines/runs?limit=50`** in `omniagentos/api/routes/routines.py` — registered BEFORE the `GET /api/routines/{routine_id}` route so FastAPI does not capture `runs` as a routine id. Pydantic models (`RecentRunItem`, `RecentRunsResponse`) enforce the exact envelope shape.

### Frontend

1. **`RecentRunsPanel.tsx`** — timeline of what the system did on its own: routine name, run id, gate passed, accepted, cost, relative finished time. Real components only, no raw JSON dump. Uses the design EmptyState/ErrorState/Loading primitives.
2. **`countdown.ts`** — pure, zero-dep client-side cron parser. Handles standard 5-field expressions with `*`, lists, ranges, and steps. Computes `nextCronFire()` against a bounded horizon (366 days / 525,960 minutes) so unparseable or dead crons return `null` and the UI renders `—` rather than `0s`.
3. **`sparkline.ts`** — pure helpers shaping `routine_runs` (per-routine or cross-bucket aggregate) into the flat `number[]` the design `Sparkline` primitive expects, with a `sparklineToneForPoints` mapping final-point acceptance ≥ 50% → `ok`, < 50% → `danger`.
4. **Page rewrite (`app/routines/page.tsx`):**
   - Chrome renamed to **Loops** (heading, lead, button labels, breadcrumbs; `/routines` URL and route unchanged).
   - Two-column responsive layout: main (table card) + sticky side (RecentRunsPanel + Recommended).
   - Table columns: Name, **Next fire (countdown) + cron raw**, Objective gate, Hard stop, **Acceptance sparkline + rate badge**, $/accepted, **StatusDot** live state + status badge + auto-pause note, Updated, **Pause/Resume** + Delete actions.
   - `useTick(...)` re-computes countdowns once a minute (cheap `setInterval`).
5. **Recommended loops from orgdims:** the page pulls `GET /api/orgdims/loops` via `useRecommendedLoops()`, renders each template as a card row (title, purpose, topology, workstream pills), and the **Accept** action mints a real routine through `POST /api/routines` (pre-populating every validator-required field with sensible defaults so the loop lands active immediately). Accept is disabled when a loop with the canonicalised name already exists (computed against the live routine list).
6. **New page (`app/routines/new/page.tsx`):** zero inline styles (every `style={{…}}` that existed before has been replaced with CSS module classes: `fieldGroupSpaced`, `formRowSpaced`, `fieldHintSpaced`, `backLinkArrow`, `fullWidth`, `formTextarea`).
7. **Fixture fallback:** `NEXT_PUBLIC_ROUTINES_FIXTURES=1` causes the `recentRuns` client to answer from a local 3-row fixture instead of hitting the API, so the Loops page renders before the backend lands (swarm coordination rule C.5).

### Tests

- `tests/routines/test_store.py` — 4 new pytest cases covering the aggregate join (multi-routine, limit enforcement, empty, null-coercion).
- `tests/routines/test_api.py` — 1 new test covering the `GET /api/routines/runs` endpoint's exact response shape, newest-first ordering, and `?limit=` semantics.
- `dashboard/src/features/routines/countdown.test.ts` — vitest cases for `parseCron`, `nextCronFire` (month rollover, dow constraint, invalid crons), `msUntilNextFire`, `formatCountdown` (event / missing cron / day/hour/minute scales).
- `dashboard/src/features/routines/sparkline.test.ts` — vitest cases for `rollingAcceptanceRates`, `bucketAcceptanceRates`, `sparklineToneForPoints` including null-run handling.

---

## Files changed

### Backend (owned)

| File | Change |
|---|---|
| `omniagentos/scheduler/store.py` | Add `RoutinesStore.list_recent_runs(limit)` method (~45 lines) |
| `omniagentos/api/routes/routines.py` | Add `GET /api/routines/runs` route + `RecentRunItem` / `RecentRunsResponse` models |
| `tests/routines/test_store.py` | Add 4 aggregate-query tests |
| `tests/routines/test_api.py` | Add 1 API test for the aggregate endpoint |

### Frontend (owned)

| File | Change |
|---|---|
| `dashboard/src/features/routines/types.ts` | Add `last_fired` to `Routine`, new `RecentRunItem`, `LoopTemplate`, `LoopRecommendation` |
| `dashboard/src/features/routines/api.ts` | Add `routinesApi.recentRuns(limit)` with fixtures fallback, `recommendedLoops` |
| `dashboard/src/features/routines/hooks.ts` | Add `useRecentRuns`, `useRecommendedLoops` hooks |
| `dashboard/src/features/routines/format.ts` | Untouched (existing helpers reused) |
| `dashboard/src/features/routines/countdown.ts` | **NEW** — cron parser + next-fire + countdown formatter |
| `dashboard/src/features/routines/sparkline.ts` | **NEW** — rolling-acceptance shaper + bucket helper + tone mapper |
| `dashboard/src/features/routines/RecentRunsPanel.tsx` | **NEW** — timeline component |
| `dashboard/src/features/routines/countdown.test.ts` | **NEW** — vitest for the cron parser and countdown |
| `dashboard/src/features/routines/sparkline.test.ts` | **NEW** — vitest for the sparkline data shaping |
| `dashboard/src/features/routines/routines.module.css` | Add Loops-page layout classes, timeline styles, recommendation styles, create-form helpers (`fieldGroupSpaced`, `formRowSpaced`, `formTextarea`, etc.) |
| `dashboard/src/app/routines/page.tsx` | **REWRITE** — Loops rename, grid layout, countdown, sparkline, live state, pause/resume, recommended loops |
| `dashboard/src/app/routines/new/page.tsx` | Strip all inline styles; rename UI copy to "Loops" |

---

## Contracts consumed / emitted

| Direction | Contract | Status |
|---|---|---|
| Emit | `GET /api/routines/runs?limit=N` → `{runs: [{routine_id, routine_name, run_id, gate_passed, accepted, cost_usd, finished_at}]}` | Implemented in this slice |
| Consume | Existing `GET /api/routines` (list) | Unchanged |
| Consume | Existing `POST /api/routines` (create) for the orgdims "Accept" action | Unchanged |
| Consume | Existing `POST /api/routines/{id}/enable` and `/disable` for pause/resume | Unchanged |
| Consume | `GET /api/orgdims/loops` (LoopTemplate[]) for the Recommended section | Unchanged |

No other slices' routes consumed or touched.

---

## Integration notes for Kimi (S7 / merge order)

- **Router registration:** the existing `routines` router already registers itself. The new `GET /runs` route is INSIDE `omniagentos/api/routes/routines.py` and is therefore automatically registered via `main.py`'s existing `include_router(routines.router)` — **no new line in `api/main.py` needed**.
- **Route ordering caveat:** the route was placed BEFORE `/{routine_id}` so FastAPI correctly discriminates against the `runs` path segment (documented inline in the code).
- **S7 (shell/IA) handoff:** this slice renames the page chrome to "Loops" inside `app/routines/page.tsx`, but the **nav label** in `AppShell.tsx` still says "Routines". S7's `NAV_SECTIONS` rewire must change that entry to `Loops` (URL stays `/routines`).
- **S6 (Observatory) handoff:** the Observatory's Loops tile should call the same `GET /api/routines/runs` aggregate built here. No extra work needed; contract is pinned in FINAL-PLAN.md §B.
- **S3 (Chat frontend) handoff:** none.
- **S4 (Board) handoff:** none.

---

## Design-charter compliance

- **Primitives only:** composed from `Badge`, `Button`, `Card`, `EmptyState`, `ErrorState`, `Icon`, `Loading`, `Page`, `PageHeader`, `Pill`, `Select`, `Sparkline`, `StatusDot`, `Table`. No hand-rolled tabs, modals, or menus.
- **Zero inline styles** in any file I own (verified by grep — zero `style={` hits in `app/routines/` and `features/routines/`).
- **One accent:** brand accent used only for primary actions; routine status and gate outcomes use semantic tones (ok / warn / danger / neutral).
- **Typography hierarchy:** PageHeader (h1) → Card panelTitle (h3) → body/micro. No new font sizes invented.
- **Density & whitespace:** 4px grid via existing `--space-*` tokens; hairline `--border` dividers; small radii; no gradients, no glassmorphism.
- **Motion:** subtle 160ms ease-out hover transitions on timeline rows (`.timelineRow:hover`) and recommendation cards, all within the 120–200ms Charter requirement.
- **Empty / Error / Loading states:** every data region (table, RecentRunsPanel, Recommended card) has all three states wired.
- **Speed:** list pages paginate natively via existing `Table`, sparkline points computed client-side from the aggregate response without additional fetches.
- **Keyboard-first:** standard `Table` keyboard nav reused; `Pause/Resume` actions are plain buttons reachable by Tab/Enter.

---

## Verification commands (run by Kimi)

```bash
# Backend
uv run pytest -q tests/routines/

# Frontend (from dashboard/)
npx vitest run src/features/routines/countdown.test.ts src/features/routines/sparkline.test.ts
npx tsc --noEmit
npm run lint
```

Expected: all green, zero inline-style lint errors inside `features/routines/` and `app/routines/`.

---

## Risks / caveats

1. **Cron parser scope:** only standard 5-field (`minute hour day month dow`) is supported. 6-field / seconds crons, `@reboot`, `@daily` shortcuts return `null` and render `—`. The system currently only emits 5-field crons (validated on create), so this is adequate for the current dataset; if 6-field support is added later, the parser is the one place that must grow.
2. **Horizon exhaustion:** unparseable or never-matching crons (e.g. `0 0 31 2 *` — Feb 30) return `null` after 366 days / 525,960 minute iterations. The UI shows `—` rather than a misleading countdown.
3. **Accept-from-template defaults:** the minted routine uses `gate_type=exit_code` with `command=true` and `hard_cap_type=max_iterations` with value 5. Every validator-required field is populated so the routine lands active, but the operator should edit the gate/hard-cap/task-template before letting it run unattended. This expectation is communicated in the Recommended card's subtitle ("accept turns one into a real loop") and the existing routine editor is the follow-up action.
4. **Orgdims network dependency:** the Recommended card gracefully renders its `EmptyState` if `GET /api/orgdims/loops` fails, so a misbehaving orgdims service does not break the Loops page itself.
