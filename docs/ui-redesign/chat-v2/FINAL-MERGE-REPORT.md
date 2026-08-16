# FINAL-MERGE-REPORT — Chat v2 + Kanban, R5 review-round reconciliation

**Merger:** Fable (chief architect) · **Date:** 2026-07-28
**Inputs:** MAIN (`OmniAgentOS`, the R3-merged state) reconciled with two review trees forked from it:
`OmniAgentOS-wtQ` (Qwen, `REVIEW-Q.md` — 2 fixes) and `OmniAgentOS-wtK` (Kimi, `REVIEW-KIMI.md` — 21 fixes).
**Method:** filesystem `diff` of every in-scope file per tree against MAIN; every hunk mapped to a claimed
fix before acceptance (no unexplained hunks found in either tree); SPEC.md cross-check on every contract
surface a hunk touched; backend contract reads (chats routes + store) to verify fix premises. Scope held to
the six frontend dirs (`features/chats`, `features/board`, `features/models`, `app/chats`, `app/board`,
`app/activity`). `features/models` was untouched by both reviewers.

## Reviewer-claim verification

- **Kimi:** all 18 files in REVIEW-KIMI §6 diff against MAIN with exactly the claimed changes; nothing
  undeclared, nothing missing. Claim spot-checks that held: `Tabs` primitive accepts `aria-label` and is
  byte-identical across trees; collab's `.progressFill` already reads `var(--pct)` in MAIN (P2-7 premise);
  the removed `Button`/`Icon`/`Select` imports in WorkspaceTabs were genuinely unused; `CHAT_UPDATED` and
  `useRef` already exist in MAIN's `useChats.ts`; e2e `board.spec.ts:74` asserts the exact title string
  K's P1-1 preserves.
- **Qwen:** both fixes present as described, plus **one undeclared file** — `chatApi.ts` — which is a
  legitimate supporting fix (the fixture-mode `updateChat` ignored `meta` patches; Q's new test needs the
  merge). Verified premises: `ChatPatchRequest.meta` already existed, the real PATCH path passes `meta`
  through, the server store **merges** meta (`{**chat.meta, **meta}`, `store.py:447`) so Q's extra PATCH
  can never clobber `preferred_model`/`orch_mode`, and the re-attach effect reads `meta.plan_job_id`
  (`useChats.ts:687-689`).

## Per-file decisions

| File | Decision | Why |
|---|---|---|
| `features/chats/ChatComposer.tsx` | **K** | P1-4 stale-closure send + P2-3 dialog guard/Esc stopPropagation; Q untouched |
| `features/chats/ChatSidebar.tsx` | **K** | P1-3 archived exclusion, P2-3 dialog/⌫ guard + `<select>` typing check, P3 aria-label + search/all-archived empty states |
| `features/chats/ChatSurface.tsx` | **UNION** (K base + Q hunk) | Disjoint regions: K's `OrchMode` import removal + post-first-send `onChatMutated` + variant-aware no-chat copy (P1-2/P2-4/P3), **plus** Q2's `seedPlan` → persist `meta.plan_job_id` in the grow-a-chat plan branch. No identifier or line overlap |
| `features/chats/chatApi.ts` | **Q** | Fixture `updateChat` meta-merge (undeclared but required by Q2's test); K untouched |
| `features/chats/chatApi.test.ts` | **Q** | New test locking the seedPlan → meta persistence chain; K untouched |
| `features/chats/useChats.ts` | **K** | P1-5 `mergePolledMessages` (poll race no longer erases optimistic/SSE messages) + P1-2 DTO refetch on turn-complete/post-send/8s. Q did **not** touch this file (brief anticipated overlap; diff proves none) |
| `features/chats/WorkspaceTabs.tsx` | **K** | P3 design `Tabs` primitive replaces hand-rolled tab bar |
| `features/chats/SessionFollow.tsx` | **K** | P1-6 de-inline; themed `.activityContainer` |
| `features/chats/TerminalView.tsx` | **K** | P1-6 de-inline; ANSI data colors stay inline (legitimate exception) |
| `features/chats/chats.module.css` | **K** | P1-6 all 11 missing classes defined (+`--terminal-*` scoped vars for the fixed-dark canvas), P1-7 `calc(100vh - 13.5rem)` composer fix, P3 `.ds-tabs` height chain |
| `features/chats/ModelPicker.tsx` | **K** | P3 `Input label=` association (proper `htmlFor`) |
| `features/board/BoardKanban.tsx` | **K** | P2-5 💬 badge client-side nav (href kept for middle-click) |
| `features/board/BoardProgress.tsx` | **K** | P2-7 `--pct` CSS-var form |
| `features/board/RunsTab.tsx` | **K** | P3 unused `task` prop removed |
| `features/board/TaskDetailDrawer.tsx` | **K** | P2-3 Esc yields to a dialog stacked on the drawer |
| `features/board/TaskDetailPanel.tsx` | **UNION** (Q base + K hunk) | Q's wiring is a **superset** of K's P2-8 (memoized `onChatCreated` **and** `onChatMutated` → `refresh()`; K wired only `onChatCreated` inline) — took Q's, then applied K's mandatory `task={task}` removal from the RunsTab call site (type error otherwise against K's RunsTab) |
| `features/board/board.module.css` | **K** | P3 `.backLink` |
| `app/chats/page.tsx` | **K** | P2-1 push-on-select, P2-2 Esc closes workspace drawer, P2-3 dialog guard, P2-4 `onChatCreated` → `selectChat` + `refreshChats` |
| `app/board/page.tsx` | **K** | P1-1 scoped empty state on the `tasks.length === 0` branch (reachable under §3.9 server-side scoping), P2-1 push-on-open, P2-6 close preserves `?project=` |
| `app/activity/[taskId]/page.tsx` | **K** | P3 import hoist + `.backLink` (applied as three surgical edits; byte-equivalent to K's file) |

Union residuals were diff-verified both ways: merged MAIN differs from K's tree by exactly Q's four
contributions, and from Q's tree by exactly K's changes. Nothing else moved.

## Fixes rejected

**None rejected.** Two acceptances carry caveats worth recording:

1. **Q2 severity was overstated for live stacks** — the server already persists `meta.plan_job_id` at
   `POST /api/chats/{id}/plan` (`chats.py:774`), and K's P1-2 DTO refetch would surface it after the next
   turn/refetch anyway. Q's client-side persist is still correct and kept: it is the only thing that makes
   the flow work in **fixture mode** (no server hooks), it closes the gap where the freshly-created chat's
   DTO in client state predates the server write, and the store's meta-merge makes it clobber-proof. The
   new test locks the chain.
2. **K's P1-1 copy drops the literal "N"** from SPEC line 81's example disclosure ("clear the filter to
   see all N" → "clear the filter to see everything"). Deliberate, not a revert: under server-side scoping
   the page only holds the *scoped* count, so N would render as "all 0" — factually wrong — or cost an
   extra unscoped board fetch. The contract's substance (pre-087 disclosure + escape hatch + pinned scoped
   state) is intact, and the e2e asserts only the title, which is unchanged. If the literal total is ever
   wanted, it needs a count endpoint or an unscoped fetch — spec-level decision.

## SPEC contract check

No contract reverted. Surfaces touched and their state after merge: §2.1 back-button-correct selection
(**now honored** — was `router.replace` everywhere), §2.5 grow-a-chat copy (kept verbatim for the dock
`panel` variant; the `/chats` no-selection state no longer misuses dock copy), §2.7 Esc chain + hotkey
scoping (**strengthened** — dialogs own the keyboard), §2.8 empty/error states (scoped board state now
reachable; sidebar gains search-no-match state; failed send still preserves the draft), §2.9 zero inline
styles (TerminalView/SessionFollow violations **removed**; remaining inline styles are the documented
data-driven ANSI colors and `--pct`/`--column-tone` CSS-var forms), §3.9 server-side project scoping
(unchanged; the client now agrees with it).

## MAIN concurrent-session drift

Preserved untouched: `omniagentos/*`, `dashboard/src/design/*` (AppShell/CommandPalette/Icon/theme.css
drift), `dashboard/src/lib`, `features/collab/*`, `features/board/filters.ts` + test, the `ChatThread.tsx`
deletion, `features/models/*`, `e2e/*`, and all non-chat-v2 docs. No hunk in either review tree reverted
post-fork MAIN content (verified: every hunk maps to a claimed fix; tree mtimes postdate MAIN's).

## Left for the verification ladder

This session's sandbox **cannot execute node/npm/npx** (subagent delegation hits the same wall), so the
frontend ladder was **not run post-merge** and is the first order of business:

```
cd dashboard && npx tsc --noEmit && npm run lint && npx vitest run && npm run build
```

Risk assessment while it's pending: both parents passed the full ladder independently in this exact
codebase state (K: tsc clean, lint 0 errors, vitest 283/283, build 55/55; Q: suite green incl. the new
test). The only combination neither tree tested is the two union files; both were verified statically
(props/types exist in MAIN, no identifier collisions, the removed RunsTab prop matches its new signature).
Expected vitest count: 284/284 (Q adds one).

Carried-over follow-ups (deduped across both reviews, neither fixed here):

1. Backend: emit `chat.updated` SSE from `_first_message_hooks` (K §5.1) — makes K's client refetch a fallback.
2. `useTaskDetail` fetches 1–2 full boards + 5 tab fetches per open (Q5 ≡ K §5.5) — wants `/api/board/{id}`.
3. Chat-list cache across `/chats` ↔ `/board` navigation (Q6).
4. Sidebar project-group a11y roles (Q7); ModelPicker popover ↑↓ nav (K §5.11).
5. Legacy `/activity` kinds (run/session/orchestration) inline styles (Q4 ≡ K §5.12) — v1 code, out of scope.
6. `app/chats` global ⌘N fires while typing in inputs (Q9).
7. SpawnCard fan-out chips don't survive reload (K §5.6); stalled-with-text shows no indicator (K §5.8);
   suggestion dismiss is session-only (K §5.9); migration-banner reappearance is a spec tension (K §5.10);
   unused `spawn` passthrough in `useChatThread` (K §5.13); stale `bridge.py:203` comment (K §5.2);
   flaky bridge tailer test under load (K §5.3).
8. Live-provider streamed turn, migration 087 on the live DB, and Playwright e2e against a live stack
   remain unverified (unchanged from MERGE-REPORT; no provider account in this environment).

Reviewer source docs: `OmniAgentOS-wtQ/docs/ui-redesign/chat-v2/REVIEW-Q.md` and
`OmniAgentOS-wtK/docs/ui-redesign/chat-v2/REVIEW-KIMI.md` (worktree-local; copy into MAIN docs in the
next docs pass if the trees are being retired).
