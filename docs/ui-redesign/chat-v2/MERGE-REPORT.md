# R3 MERGE REPORT — Chat v2 + Kanban Chat

**Merger:** Fable (chief architect) · **Date:** 2026-07-28
**Inputs:** TREE-Q (`OmniAgentOS-wtQ`, 3 Qwen workers) · TREE-K (`OmniAgentOS-wtK`, Kimi) · SPEC.md · all four team reports · line-level comparison of every domain file in both trees against MAIN.
**Method note:** the team trees sit outside this session's allowed directories, so their relevant subtrees were mirrored read-only into `.merge-src/` via a temporary helper (`scripts/_r3_sync.py`); both were removed after the merge. Fork audit: both trees share an identical fork base equal to MAIN's v1 working tree; MAIN's post-fork commits (loops/audit/control-plane) touch **zero** merge-domain files, so whole-file adoption was safe everywhere.

---

## Verdict up front

**TREE-K (Kimi) wins all five domains.** No TREE-Q code was grafted. This was not a coin-flip: Q's tree failed verification-by-reading in ways its own reports misstate (details per domain), was never run against any test suite, and its three workers produced two overlapping, never-integrated drawer implementations. K's tree is one coherent contract surface, and every K claim I checked against its code was true — including its disclosed deviations.

Merged result validated in MAIN after the merge (see §Validation): **1,362 backend tests + 283 frontend tests passed, tsc/lint/build clean, OpenAPI contract regenerated and green.**

---

## Domain A — chat backend (`omniagentos/chats/`, routes/chats.py, supervisor, runner_hook)

**Winner: K.**
- K's `ChatTurnBridge` implements the full P0-1/P0-2 contract: real transcript tailing via a shared byte-offset reader, cross-process idempotent `close_turn` (SELECT-probe + append under one writer-lock hold), 15-min timeout, 32-cap→503, idle-exit, error surfacing on dead sessions, DAL injection for hermetic tests.
- Q's bridge **claims** the idempotency SELECT in its docstring but contains no such probe (spec's two-writer contract unmet), and it polls `session.output_text` from the DB instead of the transcript — deltas effectively never stream. Q also never extracted the shared reader (spec: "one implementation").
- Q's routes lack tri-state PATCH (`model_fields_set`/sentinel) and **steer-when-live is entirely absent** despite "✅" in Q's report.
- K's name deviation `sessions/transcript_delta.py` (spec said `chain_read.py`) is correct — `chain_read.py` was already taken in MAIN by the T6.4 protocol; verified.

## Domain B — chat frontend (`app/chats`, `features/chats`, `features/models`)

**Winner: K.**
- One `useReducer` turn state with out-of-order-delta drop, completed-dedupe on `meta.session_id + turn`, 6s stall → 3s poll → 15-min cap, module-level thread cache, pinned `SendResult`/ChatDTO types, meta-object attachments both directions, `?c=` URL selection, full §2.7 keyboard map (incl. ↑ recall + Esc chain + sidebar nav), zero native dialogs.
- K deletes `ChatThread.tsx`/`ChatHeader.tsx` per §2.2 (ChatSurface is literally the one primitive); Q kept both alongside ChatSurface and widened the sidebar to 18rem against the pinned 15rem.
- Q's set was never type-checked or built; K's passed lint/tsc/vitest/build in its tree and re-passed in MAIN.

## Domain C — board backend (intake.py, service.py, migration)

**Winner: K.**
- Both migrations are SQL-identical (column + index + two backfills); K's taken for its documentation and **renumbered 086 → 087** (MAIN's 086 is `control_plane`).
- K implements both spec-mandated writes Q omitted: `_persist_board_project_id` at all three card-creation sites (without it, session-mode cards never scope — the exact P0-7 failure the spec called out) and the `planner_brief` write on plan confirm (Q built only the read side, so the field would stay null forever).
- K layers enrichment + ETA in `service.py` (routes stay thin, helpers unit-testable); Q inlined ~240 lines in the route module.
- ETA: all three bases with floors 30s/30s/0s, confidence, sample_size, honest null.

## Domain D — board frontend + dock (`app/board`, `features/board`)

**Winner: K.**
- K's `TaskDetailDrawer` mounts a shared `TaskDetailPanel` (six tabs, per-tab empty/error isolation, `ChatSurface variant="panel"` in the Chat tab — the same deck, no duplication). `?task=` deep link, Esc close, BoardKanban click-intercept keeping the href for middle-click, 💬 chat-origin badge, checklist bar, scoped-empty-state disclosure. `filters.ts` untouched (it becomes correct, per spec).
- Q's `BoardTaskDrawer` re-implements its own six tabs with a placeholder Files tab and a Runs tab that just links out, and `BoardChatDock` duplicates the composer deck around a dynamic-import hack (`import()` + `as string` cast) that its own report flags as a probable lint/tsc problem. Never type-checked.
- `features/collab/*` needed no changes in either tree (v1 types already declared the four card fields) — MAIN's concurrent collab work is untouched.

## Domain E — task detail (`app/activity`, task-detail components)

**Winner: K.**
- Spec §2.6 requires the drawer and `/activity/[taskId]` to render **the same components**. K does exactly that: `kind=board` recomposes `TaskDetailPanel`; legacy run/session activity paths untouched; `AttemptTimeline` extracted once.
- Q's tree has TWO parallel task-detail implementations that never met: the board worker's `BoardTaskDrawer` tabs and the taskdetail worker's `features/taskdetail/*` (14 files, its own hook, a seventh Timeline tab, a `ChatLogTab` that acknowledges it should be ChatSurface "when it lands"). `/board` and `/activity` would ship two divergent UIs.
- Q's `features/taskdetail` package is genuinely granular (itemized acceptance-criteria dots, merged event timeline) but adopting any of it means rewiring unverified code into K's verified whole — declined; logged below as follow-up polish.

## Pieces grafted from the loser

**None.** Candidates evaluated and declined:
1. Q's `AcceptanceCriteria.tsx` (pass/fail dots vs K's prose block in TaskOverview) — cosmetic upgrade, requires cross-package rewiring of unverified code; noted as follow-up.
2. Q's `TimelineTab` (attempts+conversation merged chronology) — nice-to-have beyond spec's six tabs; same reasoning.
3. Q's `/legacy-folders` endpoint and `list_chats_with_legacy_folders` — off-spec surface (§3.12 pins `/api/chats/folders` as the only legacy endpoint); excluded on contract-fidelity grounds.

## Files written into MAIN (all from TREE-K unless noted)

**Backend (11):** `omniagentos/chats/{bridge.py*, classify.py*, store.py, __init__.py}` · `omniagentos/api/routes/{chats.py, intake.py, sessions.py}` · `omniagentos/sessions/{supervisor.py, transcript_delta.py*}` · `omniagentos/memory/runner_hook.py` · `omniagentos/intake/service.py` · `omniagentos/collab/store.py` · `omniagentos/db/migrations/087_board_project_scope.sql*` (renumbered from teams' 086)
**Contracts:** `contracts/openapi.json` — regenerated from MERGED main (not copied: K's copy predates MAIN's `system_jobs` router), green against `tests/api/test_openapi_contract.py`.
**Frontend (30):** `features/chats/{ChatSurface*, ModelPicker*, PlanCard*, SpawnCard*, ProjectSuggestionBar*, ChatComposer, ChatSidebar, chatApi.ts, chatApi.test.ts, useChats.ts, chats.module.css}` · `features/models/useModels.ts*` · `features/board/{TaskDetailDrawer*, TaskDetailPanel*, TaskOverview*, PlanView*, TaskFilesPanel*, RunsTab*, AttemptTimeline*, useTaskDetail*, board.module.css*, BoardKanban}` · `features/projects/{hierarchyHooks.ts, HierarchyViews.tsx, index.ts}` (duplicate `useModelOptions` deleted) · `app/chats/page.tsx` · `app/board/page.tsx` · `app/activity/[taskId]/page.tsx` · `e2e/product.spec.ts` (v0 chat assertions updated) · `e2e/board.spec.ts*`
**Deleted:** `features/chats/ChatThread.tsx`, `features/chats/ChatHeader.tsx` (replaced by ChatSurface).
**Tests (5 files):** `tests/chats/{test_bridge.py*, test_dto.py*, test_routes.py}` · `tests/collab/{test_board_project_scope.py*, test_board_eta.py*}`. Q's test files were not brought — they target Q's incompatible APIs.
(*) = new file.

## Conflicts resolved

1. **Migration collision:** teams' `086_board_project_scope.sql` vs MAIN's committed `086_control_plane.sql` → renumbered to **087** (next free), filename + header + every code/comment/test reference updated (incl. `test_board_project_scope.py`, which reads the migration file by path — it would have applied the wrong SQL otherwise). Zero `086` references remain in merged chat-v2 files.
2. **`api/main.py`:** MAIN carries a post-fork `system_jobs` router the teams' forks predate. Untouched per mandate — all new routes live under existing routers, exactly as the spec pinned. Verified still registered.
3. **MAIN's concurrent v1 fixes preserved:** the runner-lane dual-write (`tests/chats/test_reply_writeback.py`) still passes — K's `runner_hook.py` is a pure superset (adds `meta` threading only). The other session's `/api/collab/board` project filter (`routes/collab.py` + `tests/collab/test_board_project_filter.py`, run-chain read-time enrichment) is a different route from spec's `/api/board` column-based scope; both coexist and both suites pass.
4. **Untouched by mandate, verified intact:** `omniagentos/audit/`, `omniagentos/context/`, `swarm/barriers.py`, `taskcontract/store.py`, `086_control_plane.sql`, loops/system_jobs files, omni-ops/connections/pulse/routines features, `dashboard/src/design/*` (no team touched it), `filters.ts`.

## Validation run in MAIN after the merge

| Check | Result |
|---|---|
| `pytest tests/chats tests/collab` | **88 passed** (incl. MAIN's v1 reply-writeback + collab project-filter suites) |
| `pytest tests/api` (incl. OpenAPI anti-drift + control-plane auth over merged routes) | **362 passed** |
| `pytest tests/dispatch tests/intake tests/sessions tests/memory tests/db` | **912 passed** (stderr shows the pre-existing atexit shutdown-flush traceback; not a failure) |
| `tsc --noEmit` | clean |
| `next lint` | 0 errors (warnings only, pre-existing pattern) |
| `vitest run` | **283 passed / 35 files** |
| `next build` | ✓ all 54 routes compiled |

## Left for the verification ladder

1. **Live-DB migration 087** — applies at next API boot (test-applied clean to fresh DBs; TestMigration087 covers the backfills). Take a copy of `var/*.db` first per TESTING.md habit.
2. **Live streamed reply** — no working provider account in any build/merge environment (Kimi's disclosed caveat stands): boot API + dashboard, send a chat message with a real account, watch `started → delta(s) → completed` render; then promote → `/board?project=` scope → drawer tabs. `e2e/product.spec.ts` + `e2e/board.spec.ts` cover this once Playwright can run against a live stack.
3. **`./scripts/certify-omniagentos.sh`** — requires a committed tree; run after this merge is committed.
4. **`/archi update`** — this merge changes architecture truth (new `chats/bridge.py` + `sessions/transcript_delta.py`, migration 087, changed `/api/board` contract). ARCHI.json/md must only move via archdocs, and the working tree carries other sessions' uncommitted ARCHI edits — run it at commit time, not from this session.
5. **Follow-up polish (from Q, optional):** itemized acceptance-criteria pass/fail rendering; merged attempts+conversation timeline view.
