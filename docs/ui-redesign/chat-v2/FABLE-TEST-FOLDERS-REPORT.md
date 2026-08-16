# FABLE — Chat v2 Verification + Color-Coded Folders Report

**Author:** Fable (chief architect) · **Worktree:** `/Users/youruser/OmniAgentOS-wtTest` (branch `chat-test`) · **Date:** 2026-07-28
**Outcome:** Phase 1 re-verified green with zero fixes needed. Phase 2 (color-coded, editable folders) implemented end-to-end — backend registry (migration 088, ChatStore, routes, pytest) + frontend (theme tokens, ChatSidebar folders UI, vitest) — and verified live through the API, the test suites, a production build, and a browser walkthrough.

---

## Phase 1 — Independent verification (both flows re-run from scratch)

### 1. SOLO — PASS (no fixes required)

Script: `var/fable-test/solo_test.py` · log: `var/fable-test/solo.log`

| Step | Evidence |
|---|---|
| Chat created | `13:04:05Z` → `cht_ac17ecee678c420fb3ee`, companion `btk_6f1fe0c1f2a947e8b5f2` |
| Message sent | `13:04:05Z` → dispatch session `ses_1889c345a1e44b3c9088`, task `tsk_83f21dd61d054fd59d16`, `steered:false` |
| Agent reply | `13:04:15Z` (first poll, ~10 s) → agent turn `seq=2`, content exactly `FABLE-SOLO-OK` |
| Result | `SOLO RESULT: PASS (content_present=True marker_seen=True)` |

### 2. SWARM — PASS (no fixes required)

Script: `var/fable-test/swarm_test.py` · log: `var/fable-test/swarm.log`

| Step | Evidence |
|---|---|
| Chat created | `13:05:33Z` → `cht_da95018402cf4f3db14d`, companion `btk_4cfeb9e6856b4b9891d7` |
| Spawn ×2 | `task_ids = [btk_bfaab221cc4b4364afc1, btk_029fc411e9244a3696e1]` |
| Children done | `13:05:45Z` (~12 s) → both `status=done`, `work.state=completed` |
| Unfiltered board | 149 cards; **both spawned tasks visible** |
| Session output | `ses_876ac71679c84e5fa5ac` transcript 13,400 B, `ses_53c59a230e6f4e059c00` transcript 12,965 B — **both contain `FABLE-SWARM-OK`** |
| Result | `SWARM RESULT: PASS (all_done=True both_visible=True sessions_ok=True)` |

(One re-run was needed for harness reasons only: the first swarm launch ran from the wrong CWD — `dashboard/` — so the script never started. No product issue.)

---

## Phase 2 — Folders: color-coded + editable

### Design decisions

- **Registry over rewrite.** Folder membership stays where it lives today (`chats.meta_json.folder`, free text). The new `chat_folders` table adds *identity* (color token, manual position) keyed by name. Readers union both sources: unregistered folders (free text only) appear gray; registered folders appear even while empty (drop targets).
- **Tokens, never hex.** The API stores/validates 8 token names (`gray red orange yellow green teal blue violet`). The dashboard maps tokens to `--ds-folder-*` CSS vars.
- **Folder creation = color upsert.** `POST /folders/{name}/color` on an unknown name registers it — no extra "create" endpoint needed; that is the "+ New folder" path.
- **Rename merges.** Renaming onto an existing folder merges into it; the target keeps its color/position. Rename/delete rewrite chats in *all* statuses (deleted included) so an undelete can never resurrect a retired name.
- **Sidebar placement: folder wins.** A chat with a folder renders under that folder only. Moving a chat to a project clears its folder (and vice-versa via `folder:""`), so every chat has exactly one sidebar home — no duplicate rows, no ambiguity.
- **The P0-8 "folders are retiring" migration banner was removed** — it directly contradicted folders becoming first-class, and its only API consumer was the legacy `{folders:[string]}` shape this task replaces. No other dashboard consumer of the old shape exists (searched; `galaxy`/`files` "folders" are unrelated vault concepts). The client parser still *tolerates* the legacy shape for mixed-deploy safety.

### Backend (omniagentos/)

- **`omniagentos/db/migrations/088_chat_folders.sql`** *(new — 088 verified as the next free number; 087 = board_project_scope)*
  `chat_folders(name TEXT PRIMARY KEY, color TEXT NOT NULL DEFAULT 'gray', position INTEGER, created_at, updated_at)`. Palette enforced in ChatStore (not CHECK) so it can grow without another migration.
- **`omniagentos/chats/store.py`** — `FOLDER_COLORS` (8 tokens), `UnknownFolderError`, `_validate_folder_name` (non-empty, no `/`, ≤100 chars), and registry methods:
  - `list_folder_registry()` — registry ∪ chat-derived folders → `[{name,color,position,chat_count}]`, ordered position (NULLs last) then name; rogue DB colors coerced to gray on read.
  - `set_folder_color(name,color)` — validated upsert; new rows appended to the manual order (max+1).
  - `rename_folder(name,new)` — moves every member chat (all statuses), re-keys the registry row (color kept), merge-on-collision, 404 on unknown, same-name no-op.
  - `delete_folder(name)` — clears `meta.folder` on members (chats fall back to Inbox/Recents), drops the row, returns count moved.
- **`omniagentos/api/routes/chats.py`**
  - `GET /api/chats/folders` → **`{folders:[{name,color,chat_count}]}`** (was legacy `{folders:[string]}` — replaced per spec; the pinned OpenAPI response schema was already a loose object, and the contract artifact was regenerated).
  - `POST /api/chats/folders/{name}/rename` `{new_name}` → `{folder}` (404 unknown / 400 invalid).
  - `POST /api/chats/folders/{name}/color` `{color}` → `{folder}` (400 non-token colors, incl. hex).
  - `DELETE /api/chats/folders/{name}` → `{deleted, chats_moved}` — *one endpoint beyond the letter of the spec*: the spec requires a `delete` store method ("chats fall back to Inbox"); without an HTTP surface it would be dead code, and the sidebar's folder menu needs it. Declared with the other folder routes ahead of the `/{chat_id}` matchers.
  - Rename/delete emit `chat.updated` per affected chat so other tabs refresh.
- **`contracts/openapi.json`** — regenerated via `uv run python scripts/generate_openapi.py`; conformance suite green.
- **Pytest** — `tests/chats/test_store.py`: 6 new registry tests (union+ordering, color validation incl. hex rejection, rename across statuses, merge semantics, unknown/no-op, delete fallback preserving unrelated meta). `tests/chats/test_routes.py`: `test_folders` updated to the new contract + 3 new route tests (color/create/list, rename+re-key+404, delete+fallback+404). Note: an encoded `/` in a folder path param (`a%2Fb`) is rejected by the router as 404 before the handler — the store validator covers the JSON-body path (tested).

### Frontend (dashboard/)

- **`src/design/theme.css`** *(additions only; tokens.ts untouched)* — `--ds-folder-{gray,red,orange,yellow,green,teal,blue,violet}` added to all three theme blocks (dark `:root`, `[data-theme="light"]`, and the `prefers-color-scheme: light` fallback). Defined as **aliases of existing theme colors** (`--danger`, `--awaiting`, `--champion`, `--promote`, `--ds-accent-pulse`, `--accent`, `--validating`, `--text-muted`) — the file's established "aliases, no new hex" pattern — so dark/light values flip with the vars they ride and inherit their AA validation.
- **`src/features/chats/folders.ts`** *(new)* — pure logic: `FOLDER_COLORS`, token guards/coercion, `chatFolder()`, `parseFoldersResponse()` (tolerates the legacy string[] shape), `buildFolderGroups()` (registry order → unregistered alpha; empty registered folders included; newest-first chats), `folderNameError()` mirroring server rules.
- **`src/features/chats/chatApi.ts`** — `listFolders(): FolderInfo[]` (new shape), `setFolderColor`, `renameFolder`, `deleteFolder`; fixture mode mirrors server semantics (upsert/merge/fallback) for dev + tests.
- **`src/features/chats/useChats.ts`** — `useChatFolders` now returns the registry (`FolderInfo[]`).
- **`src/features/chats/ChatSidebar.tsx`** — Folders section between Projects and Recents:
  - Folder group headers render a **colored dot** via CSS classes only (`.folderColor<Token>` sets `--folder-color`; `.folderDot`/`.swatch` consume it — zero inline styles).
  - **Options menu** per folder (palette icon button, `aria-haspopup`/`aria-expanded`): `Rename…` (inline edit input — Enter commits, Esc/blur cancels), an 8-swatch color row (`menuitemradio`, `aria-checked`, current selected), and `Delete folder…` (confirm Dialog; chats fall back to Recents). Menu is keyboard reachable: focus moves to the first item on open, Escape closes and restores trigger focus, click-outside closes.
  - **"+ New folder"** button → Dialog with name input (validated) + swatch color picker (radio semantics).
  - Chats are filed by **drag-and-drop onto folder headers** or the per-row move `<select>` (menu equivalent of drag, per the sidebar's a11y rule) which now has *Folders* and *Projects* optgroups.
  - Keyboard nav (`↑↓/Enter/⌫`) spans project → folder → recents groups; folders collapse independently.
- **`src/features/chats/chats.module.css`** — dot/swatch/menu/rename-input/new-folder styles from theme tokens; obsolete migration-banner styles removed.
- **Vitest** — `src/features/chats/folders.test.ts` *(new)*: 13 tests over tokens, parsing (new shape, legacy shape, malformed), `chatFolder`, grouping (order, empty registered folders, newest-first, unfiled ignored), and name rules.

### Fixes made along the way

- **`ChatThread.tsx` deleted** (untracked orphan): it was pre-v2 dead code — zero imports anywhere — that no longer type-checked against the v2 `ChatComposer` (missing 7 required props) and carried banned inline styles. It broke `tsc --noEmit`/`next build` for the whole worktree before any folder work. Removing the orphan restored a clean typecheck.
- **Mechanical `ruff --fix`** on the chats surface (my edited import block in `routes/chats.py`, plus 4 pre-existing auto-fixable findings in `chats/bridge.py` / `tests/chats/test_routes.py`: import sort, `Callable` from `collections.abc`, unused import). `ruff check` on the chats surface is now fully green.

### Known pre-existing issues (not introduced, not fixed — out of folder scope)

- `mypy` reports 9 pre-existing errors in chat-v2 merged code I did not author (`chats/store.py to_dto` meta typing ×6, `chats/classify.py` redef, `routes/chats.py` dispatch literal + `items` redef). My additions contribute **zero** mypy errors.
- The ad-hoc dev server showed console CORS noise from the event channel polling the default `:8485` — an environment default of my temporary dev instance (event URL env not set), unrelated to folders.

---

## Phase 2 verification (all green)

| Check | Result |
|---|---|
| `uv run pytest tests/chats tests/sessions tests/api/test_openapi_contract.py -q` | **449 passed** (69→79 chats tests incl. 9 new/updated folder tests) |
| `npx vitest run src/features/chats` | **42 passed** (3 files, incl. 13 new folder tests) |
| `npx tsc --noEmit` | clean |
| `npx eslint src/features/chats/` | clean |
| `npx next build` | success (production bundle) |
| OpenAPI conformance (`tests/api/test_openapi_contract.py`) | 3 passed after regeneration |
| Migration on live DB | `schema_migrations` has 88; `chat_folders` table present (applied automatically on API restart) |

**Live API roundtrip** (API restarted with the same env: `OMNIAGENTOS_DB=…/var/omniagentos.db`, `OMNIAGENTOS_API_BASE_URL=http://127.0.0.1:8486`, `OMNIAGENTOS_API_PORT=8486`):
`POST /folders/FableTest/color {violet}` → created · `POST /folders/FableTest/rename {FableRenamed}` → re-keyed, color kept · `POST …/color {#fff}` → 400 with token list · `DELETE /folders/FableRenamed` → clean · list empty.

**Live UI walkthrough** (browser-operator against a dev server proxied to :8486): **9/9 PASS** — new-folder dialog (name + 8 swatches), teal folder created with teal dot + toast, options menu (Rename…/swatches with `aria-checked`/red Delete), recolor to violet reflected in dot class, inline rename to "Insights" with toast, chat filed via the move select's Folders optgroup (count badge, gone from Recents), dots clearly visible in **both dark and light** themes (violet `#b48ef0` on `#12161f` dark / `#7a4fc0` on white light), state persisted across full reload. Server state matched afterwards (`{"name":"Insights","color":"violet","chat_count":1}`, chat meta.folder="Insights"); test folder then deleted (`chats_moved:1`), chat fell back to Recents.

---

## Files changed

**Backend:** `omniagentos/db/migrations/088_chat_folders.sql` *(new)* · `omniagentos/chats/store.py` · `omniagentos/chats/__init__.py` · `omniagentos/api/routes/chats.py` · `omniagentos/chats/bridge.py` *(ruff-mechanical only)* · `contracts/openapi.json` *(regenerated)* · `tests/chats/test_store.py` · `tests/chats/test_routes.py`
**Frontend:** `dashboard/src/design/theme.css` · `dashboard/src/features/chats/folders.ts` *(new)* · `dashboard/src/features/chats/folders.test.ts` *(new)* · `dashboard/src/features/chats/chatApi.ts` · `dashboard/src/features/chats/useChats.ts` · `dashboard/src/features/chats/ChatSidebar.tsx` · `dashboard/src/features/chats/chats.module.css` · `dashboard/src/features/chats/ChatThread.tsx` *(deleted — dead orphan)*
**Test artifacts:** `var/fable-test/{solo_test.py,swarm_test.py,solo.log,swarm.log}`

**Runtime state at handoff:** API running on `http://127.0.0.1:8486` (restarted once with identical env to load the new routes; migration 088 auto-applied). Temporary dev server (:3300) stopped. No git mutations performed. No new dependencies.
