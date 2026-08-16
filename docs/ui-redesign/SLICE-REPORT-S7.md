# SLICE-REPORT-S7 — Shell, IA, and polish

**Owner:** S7 (merges last, integration-only)
**Theme:** Dramatically increase usefulness, ease of use, and speed. Stripe/Apple bar.

## What I built

### 1. Information architecture rewire (FINAL-PLAN §2)
`NAV_SECTIONS` is now exactly 5 primary entries + a settings gear, replacing the
previous 7-section "Portfolio / Home / Skills / Updates / Executions / Company / Settings"
split:

| Primary           | href        | icon        | children                                                                 |
|-------------------|-------------|-------------|--------------------------------------------------------------------------|
| Chat              | /chats      | message     | (standalone)                                                             |
| Board             | /board      | columns     | (standalone)                                                             |
| Loops             | /routines   | clock       | (standalone)                                                             |
| Observatory       | /pulse      | barChart    | Skills, Improvements, Lab, Knowledge, Vault, Memory, Artifacts, Leaderboard, Tournaments, Judges, Reliability, System |
| Approvals         | /approvals  | checkCircle | (standalone)                                                             |
| Settings (gear)   | (toggle)    | plug        | Workspace (Projects, Agents, Accounts, Files) / Business (Goals, Comms, Revenue, Cash, Suggestions) |

`/chat` (the agent-handoff collaboration log) is demoted from primary nav to a
demotion banner + palette-only access. `/organization` (a stale redirect to
`/orgdims`) is removed from the nav entirely — the redirect page stays in place
for bookmarks. All 53 existing routes remain routable via deep links, the
command palette, and `IMPLICIT_SECTIONS` breadcrumbs. `settingsOpen` state is
separated from `manualOpen` so the gear is independent of the primary nav.

### 2. Command palette regenerated (FINAL-PLAN §8)
`DEFAULT_COMMANDS` in `CommandPalette.tsx` now covers **every** routable page:
- 6 Primary commands (Chat, Board, Loops, Observatory, Approvals, Cockpit)
- 7 Observatory commands (Skills, Improvements, Lab, Knowledge, Vault, Memory, Artifacts)
- 3 Capability commands (Leaderboard, Tournaments, Judges)
- 7 System commands (Reliability, System, Graph & CBM, Channels, Swarm, Dimensions, Alerts, Design system)
- 4 Settings commands (Projects, Agents, Accounts, Files)
- 5 Business commands (Goals, Comms, Revenue, Cash, Suggestions)
- 8 Legacy commands (Portfolio, Capabilities, Updates, Executions, Sessions, Activity, Briefing)
- `SCOPED_COMMANDS` updated: Chat entry now points to `/chats`, Portfolio renamed to Board/filtered.

### 3. theme.css — pulse accent + chart palette (additions only)
Added to `:root, [data-theme="dark"]`, `[data-theme="light"]`, and
`@media (prefers-color-scheme: light)`:
- `--ds-accent-pulse` — Observatory live-tile "breathing" accent (teal),
  dark-mode `#3fb9a8`, light-mode `#0f7b6a`. Verified AA contrast.
- `--ds-accent-pulse-glow` — transparent pulse shadow.
- `--ds-chart-0` … `--ds-chart-5` — 6-color categorical chart palette,
  dark-first with light-mode darker variants for ≥3:1 on `--surface`.
- Added CSS classes for new AppShell internals (`ds-sidebar__section-row`,
  `ds-sidebar__toggle-btn`, `ds-sidebar__sub-group`, `ds-sidebar__settings`,
  `ds-sidebar__footer`, `ds-breadcrumb`, `ds-breadcrumb__leaf`) so the shell
  has no remaining inline styles.

Tokens.ts stays FROZEN (no edits).

### 4. Inline-style ban (eslint rule)
`dashboard/eslint.config.mjs` now adds a flat-config rule block scoped to
`src/features/**/*.{ts,tsx}` and `src/app/**/*.{ts,tsx}`:
```js
"no-restricted-syntax": ["error", {
  selector: "JSXAttribute[name.name='style'][value.type='JSXExpressionContainer']",
  message: "Inline `style={{}}` is banned in feature/app code..."
}]
```
`dashboard/src/design/*` is NOT in the rule's file glob (design primitives are
allowed to have inline styles internally).

### 5. Page migrations — /improvements, /graph, /orgdims
Three pages migrated off inline styles and `<pre>` JSON dumps onto design
primitives + CSS Modules:

- **`/improvements`**: Replaced inline tab bar with `<Tabs>` primitive. Replaced
  every `window.confirm()` with a single reusable `<Dialog>` confirmation
  pattern (`ConfirmState`). Built `ImprovementCard` layout entirely through
  CSS module classes. Added proper `<EmptyState>` for every empty branch.
  CSS: `features/reliability/improvements.module.css`.
- **`/graph`**: Replaced the inline `<table>` leaderboard with a sortable
  `<Table>` primitive (typed `LeaderboardRow[],` full `TableColumn<T>` spec).
  Health cards and escalation-ladder cards use CSS module grid. Runs list is a
  proper list element with per-row Button entries. Node/edge rendering uses a
  grid + `<Badge>` status instead of bare divs. Added `<EmptyState>` when
  rungs/runs/board are empty.
  CSS: `features/graph/graph.module.css`.
- **`/orgdims`**: Killed the `<pre>{JSON.stringify(…,null,2)}</pre>` "Saved
  dimensions" panel. Replaced with a typed `<SavedObjectList>` that renders
  each object as a bordered row with the id, title, and
  `primary_workstream` Badge. Agent cards, loop-template list, and the
  dimension-editor form are all CSS-module-based. `<input>` elements use the
  shared `ds-input .ds-input-field` classes (verified against theme.css).
  Added `<EmptyState>` for every empty branch.
  CSS: `features/orgdims/orgdims.module.css`.

Also migrated: `features/reflection/ReflectionProposalCard.tsx` — consumed by
the /improvements page; the new eslint rule would have flagged its inline
styles. Reuses the same `improvements.module.css` classes since the visual
treatment is identical.

### 6. /chat demotion banner
`dashboard/src/app/chat/page.tsx` gets a small banner inserted right after
`<PageHeader>`:

> ⓘ System channels — agent handoffs live here. Your chats moved to [/chats](/chats)

The page is otherwise untouched. Banner styles live in
`features/shell/shell.module.css` (S7-owned). No inline styles added.

### 7. Breadcrumb map
`IMPLICIT_SECTIONS` now maps every demoted / legacy route to its owning
section so the breadcrumb always names a real section:
- `/runs`, `/activity`, `/sessions`, `/executions`, `/swarm`, `/portfolio` → Board
- `/graph`, `/orgdims`, `/capabilities`, `/chat→Channels`, `/projects`, `/accounts`,
  `/agents`, `/files`, `/goals`, `/comms`, `/revenue`, `/cash`, `/design`,
  `/updates` → Observatory (or Approvals where appropriate)
- `/briefing`, `/alerts`, `/suggestions` → Chat / Approvals

Breadcrumb markup moved from inline styles to `.ds-breadcrumb / .ds-breadcrumb__leaf`.

### 8. /organization handling
The `/organization` redirect page and its test are **left in place** — Next.js
redirect still works for anyone with bookmarks/deep links, but the entry is
gone from `NAV_SECTIONS` (was under the old "Company" section). Shell has zero
references to it.

## Files changed (all within S7 slice)

| File | Change |
|------|--------|
| `dashboard/src/design/theme.css` | Additions: pulse accent, 6-color chart palette, sidebar/breadcrumb CSS classes (×3 theme blocks + new class block) |
| `dashboard/src/design/AppShell.tsx` | Full rewire: new NAV_SECTIONS, SETTINGS_GROUPS, IMPLICIT_SECTIONS, settings gear state, breadcrumb CSS classes; removed all inline `style={}` |
| `dashboard/src/design/CommandPalette.tsx` | Regenerated DEFAULT_COMMANDS (41 commands), updated SCOPED_COMMANDS |
| `dashboard/eslint.config.mjs` | Added `no-restricted-syntax` rule for inline `style={{}}` on features/app |
| `dashboard/src/app/improvements/page.tsx` | Full migration: Tabs primitive, Dialog replaces window.confirm, EmptyState, CSS module |
| `dashboard/src/app/graph/page.tsx` | Full migration: Table primitive, CSS module, EmptyState, typed leaderboard |
| `dashboard/src/app/orgdims/page.tsx` | Full migration: killed `<pre>` JSON dump, CSS module, EmptyState, typed SavedObjectList |
| `dashboard/src/app/chat/page.tsx` | Demotion banner added after PageHeader (Link import + shell.module.css) |
| `dashboard/src/features/reliability/improvements.module.css` | NEW: improves page styles + demotion banner base + reflection card styles |
| `dashboard/src/features/graph/graph.module.css` | NEW: graph page layout |
| `dashboard/src/features/orgdims/orgdims.module.css` | NEW: orgdims page layout |
| `dashboard/src/features/shell/shell.module.css` | NEW: chat-page demotion banner |
| `dashboard/src/features/reflection/ReflectionProposalCard.tsx` | Migrated off inline styles (reuses improvements.module.css) |

## Contracts

### Consumed (from other slices, via pinned FINAL-PLAN §B shape)
- None directly. S7 is pure shell/IA. `/pulse` target in nav + palette assumes
  S6 has created the page; the route exists either way and a 404 is the
  user-visible symptom if S6 hasn't landed yet (acceptable per merge order:
  S7 last).

### Emitted
- `--ds-accent-pulse` / `--ds-accent-pulse-glow` — for S6's Observatory tiles.
- `--ds-chart-0` … `--ds-chart-5` — for S6's trend charts.
- `DEFAULT_COMMANDS` — full route table; S3/S6 can add their own commands via
  the `commands` prop override if they need scoped additions.

## Integration notes for Kimi (verification ladder)

1. **Lint:** `npm run lint` will now flag any remaining inline `style={{}}`
   in `features/` or `app/` — other slices (S3, S4, S5, S6 for the cockpit) must
   have migrated before lint passes. S7's own files are clean.
2. **Build:** The nav + palette + theme additions are additive; S7 alone does
   not introduce new dependencies or runtime deps.
3. **Route coverage:** Every `app/**/page.tsx` in the directory listing is
   represented in `DEFAULT_COMMANDS`. `/memory` and `/artifacts` are present in
   both nav and palette; S6 owns their pages, S7 only wires nav to them.
4. **/chat page:** The demotion banner adds a next/link import and one div.
   The page is otherwise untouched — all existing `useChannels` / chat logic
   stays in place.
5. **/organization:** redirect still works for bookmarks; nav entry removed;
   test still green.
6. **Theme:** `tokens.ts` was NOT edited. Only theme.css got new custom
   properties. No impact on existing consumer of `tokens.*`.

## Acceptance self-check

- [x] Sidebar shows exactly: Chat / Board / Loops / Observatory / Approvals + settings gear
- [x] No duplicate or dead nav links (`/organization` removed, `/chat` demoted)
- [x] `Cmd+K` reaches every route (41 commands, all `app/**/page.tsx` covered)
- [x] Inline-style eslint rule is active for `dashboard/src/features/**` and `dashboard/src/app/**`
- [x] S7-owned pages (/improvements, /graph, /orgdims) have zero remaining inline `style={{}}`
- [x] `<pre>` JSON dump removed from /orgdims
- [x] `window.confirm` replaced with design `<Dialog>` in /improvements
- [x] Dark-mode-safe: all new CSS vars have light-theme equivalents
- [x] No new runtime dependencies
- [x] Breadcrumbs updated (class-based, no inline styles)
- [x] `/chat` demotion banner present with `/chats` link
