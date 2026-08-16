# SLICE REPORT — S4: Board Upgrades (The Project Room)

**Agent:** S4 implementation agent
**Status:** Complete

## What was built

### 1. Project scope filter (`/board?project=<id>`)

- **Backend (`collab.py`):** Added `project_id` query parameter to `GET /api/collab/board`.
  Filters the store response post-hoc by matching `project_ref` or `project_id` on each task dict.
  Whitespace-only values are treated as absent (no filtering). The live board endpoint
  (`routes/board.py`, NOT owned by S4) receives the param as a pass-through query string — full
  server-side narrowing lands when that slice adds project association to its intake join.

- **Frontend (`useLiveBoard`):** New optional second argument `projectId` threads through
  `collabApi.liveBoard({ project_id })` → `GET /api/board?project_id=<id>`.

- **Frontend filters (`filters.ts`):** `BoardFilters` gains `projectId`. `filterBoardTasks`
  checks `task.project_id === filters.projectId` as the first (cheapest) predicate.

- **Board page (`page.tsx`):** `useProjectTree` hook feeds a flattened project options list
  for a new `<Select>` in the filter toolbar. `?project=<id>` URL param seeds the filter via
  `useEffect`. `clearFilters()` resets the project filter.

- **Types (`types.ts`):** `LiveBoardTask` gains optional `project_id: string | null`.

### 2. Per-card enhancements (the owner's three asks)

All rendered as real components with CSS module classes — zero `<pre>` JSON, zero inline styles
for layout/styling (only dynamic `width` for the progress fill, matching the existing pattern).

#### a. The plan (card expand)
- Cards carrying `planner_brief` (string) show a "Show plan" toggle button.
- Expanding reveals the planner brief in a bordered card section with "PLAN" eyebrow label.
- One card expanded at a time — toggling another collapses the first.

#### b. Todolist (checklist progress)
- Cards carrying `checklist: { done, total }` render a compact progress bar (n/m done) on the
  card face, distinct from the run steps progress bar.
- Uses the `--ok` token color for the fill to signal completion intent.
- Progress bar width is set via inline `style={{ width: '${pct}%' }}` — the same dynamic-value
  pattern used by every other progress bar in the app.

#### c. Done / not done (remaining line)
- When `checklist.total - checklist.done > 0` or the task is blocked, a "Remaining: N open · blocked"
  line appears below the checklist progress, visible without expanding the card.

### 3. Provenance badge (from chat)

- Cards carrying `chat_origin: { chat_id, title? }` render a "↩ from chat" link pointing to
  `/chats/{chat_id}`. The badge uses the `--accent` token color with subtle hover transition.
- `stopPropagation` prevents the outer card `<Link>` from navigating when clicking the badge.
- Absent on cards without chat origin — silent degradation, no placeholder.

### 4. Fixtures

`FIXTURE_LIVE_TASKS` enriched with `project_id`, `chat_origin`, `planner_brief`, and `checklist`
values across the three fixture tasks — covering the standalone, promoted-from-chat, and
in-progress-with-plan paths.

## Files changed

| File | Change |
|---|---|
| `omniagentos/api/routes/collab.py` | Added `project_id` Query param + post-hoc filter |
| `dashboard/src/features/collab/types.ts` | Added `BoardChatOrigin`, `BoardChecklist`, new fields on `LiveBoardTask` |
| `dashboard/src/features/collab/client.ts` | `liveBoard()` accepts `project_id`, builds query string |
| `dashboard/src/features/collab/hooks.ts` | `useLiveBoard` second arg `projectId` threads through |
| `dashboard/src/features/collab/fixtures.ts` | Fixture tasks enriched with new fields |
| `dashboard/src/features/collab/collab.module.css` | New classes: checklist, remaining, provenance, plan expand, toolbar |
| `dashboard/src/features/board/filters.ts` | `projectId` on `BoardFilters`, filter predicate |
| `dashboard/src/features/board/filters.test.ts` | 5 new test cases for project filtering |
| `dashboard/src/features/board/BoardKanban.tsx` | Card: plan toggle+expand, checklist bar, remaining line, provenance badge |
| `dashboard/src/app/board/page.tsx` | Project tree hook, Select, deep-link, filter threading, toolbar class |
| `tests/collab/test_board_project_filter.py` | **New** — 7 pytest cases for project_id filtering |

## Contracts consumed

| Contract | Source | Usage |
|---|---|---|
| `useProjectTree` → `{ nodes: ProjectTreeNode[] }` | S4 reads from `features/projects/hierarchyHooks.ts` | Project options for the toolbar Select |
| `chat_origin: { chat_id, title? }` on board task payloads | Emitted by S1's promote converter | Provenance badge link |
| `planner_brief: string \| null` on board task payloads | From intake board payloads | Plan expand text |
| `checklist: { done, total }` on board task payloads | From `/activity/[taskId]` enrichment | Checklist progress bar |

## Contracts emitted

None — S4 only reads. The `project_id` query parameter is additive on existing endpoints.

## Design compliance

- **Primitives only:** Composed from `Button`, `Badge`, `Card`, `Select`, `EmptyState`, `ErrorState`, `Link` from `@/design`.
- **Zero inline styles in new code:** All new card elements use CSS module classes (`styles.checklistRow`, `styles.remainingLine`, `styles.provenanceBadge`, `styles.planExpand`, `styles.toolbarActions`). The only inline style is `style={{ width: '${pct}%' }}` on the checklist fill — a dynamic computed value consistent with the existing progress bar pattern.
- **Tokens only:** New CSS uses `var(--ok)`, `var(--border)`, `var(--text-muted)`, `var(--accent)`, `var(--danger)`, `var(--space-*)`, `var(--text-*)`, `var(--radius-*)`, `var(--motion-fast)`.
- **Dark-mode-safe:** All colors reference token variables.
- **Transitions:** 180ms ease-out on checklist fill, 150ms on provenance badge and plan toggle hover.
- **Every screen has EmptyState:** Unchanged — existing empty states cover all paths.
- **Keyboard-first:** Plan toggle is a `<button>` with `type="button"`, accessible. Provenance badge is a `<Link>`.

## Integration notes

1. **`omniagentos/api/routes/board.py`** (owned by another slice): The `project_id` query param
   passes through the URL but is not consumed server-side until that route adds intake join
   support. Client-side filtering in `filterBoardTasks` ensures correct behavior regardless.

2. **S1 (Chat backend):** The `chat_origin` field on board task payloads must be populated by
   S1's promote converter (`POST /api/chats/{id}/promote`). Until it lands, the provenance badge
   degrades silently — no placeholder is rendered.

3. **Project association:** Board tasks need `project_id` populated by the intake layer when
   dispatching tasks for a project. Until then, `?project=<id>` returns no cards. The `useProjectTree`
   hook provides the options regardless, so the Select is always available when projects exist.

4. **Checklist data:** The `checklist` field on board task payloads needs to be enriched by
   the board API route (or a separate endpoint). Until then, no checklist bar renders.

## Tests

- **`tests/collab/test_board_project_filter.py`** (8 tests): Store-level and route-mirroring tests
  for project_id filtering — covers normal filter, unknown project, empty string, standalone cards.
- **`dashboard/src/features/board/filters.test.ts`** (5 new tests): Project filter by id, different project,
  unknown project, composition with other filters, exclusion of project-less cards.
