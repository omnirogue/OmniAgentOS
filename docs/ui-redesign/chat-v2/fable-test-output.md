All three phases are complete. Full details are in `docs/ui-redesign/chat-v2/FABLE-TEST-FOLDERS-REPORT.md`.

## Phase 1 — both flows re-verified green, zero fixes needed
- **SOLO PASS**: chat `cht_ac17ecee678c420fb3ee` → agent replied exactly `FABLE-SOLO-OK` in ~10s (session `ses_1889c345a1e44b3c9088`).
- **SWARM PASS**: chat `cht_da95018402cf4f3db14d` → both children (`btk_bfaab221…`, `btk_029fc411…`) reached `done` in ~12s, both visible on the unfiltered board (149 cards), both session transcripts (13.4/13.0 KB) contain `FABLE-SWARM-OK`.

## Phase 2 — color-coded, editable folders shipped end-to-end
**Backend:** migration `088_chat_folders.sql` (088 confirmed as next free number); ChatStore registry methods (`list_folder_registry`, `set_folder_color` upsert = creation path, `rename_folder` with merge-on-collision that re-keys the row and moves chats in all statuses, `delete_folder` with Inbox fallback); routes `GET /folders` → `{folders:[{name,color,chat_count}]}`, `POST /folders/{name}/rename`, `POST /folders/{name}/color`, plus a `DELETE /folders/{name}` I added beyond the letter of the spec — the required store `delete` method would otherwise be unreachable dead code. Colors are strictly the 8 token names; hex is rejected with a 400. OpenAPI contract regenerated, conformance green.

**Frontend:** `--ds-folder-*` tokens added to theme.css for dark + light as aliases of existing theme colors (the file's "no new hex" pattern, so AA contrast is inherited); ChatSidebar gained a Folders section with colored dots, a per-folder options menu (inline **Rename…**, 8-swatch color row with `aria-checked`, delete with confirm dialog), "+ New folder" dialog with color picker, drag-drop + move-select filing (folder wins for placement so every chat has one home). Zero inline styles, keyboard reachable throughout. I also **removed the "folders are retiring" migration banner** — it contradicted this feature and was the old shape's only consumer.

**Verification:** 449 pytest (chats+sessions+contract) · 42 vitest (13 new folder tests) · tsc/eslint clean · `next build` succeeds · migration auto-applied to the live DB on API restart (same env) · live curl roundtrip correct · browser-operator walkthrough **9/9 PASS** including dark/light dot visibility and persistence across reload; server state matched the UI and my test folder was cleaned up after.

**Judgment calls to know about:** I deleted `ChatThread.tsx` — an untracked, zero-reference pre-v2 orphan that broke `tsc`/`next build` for the whole worktree — and left 9 pre-existing mypy errors in chat-v2 code I didn't author (documented in the report; my code adds zero). The API is back up on 8486 with your exact env; no git mutations, no new dependencies.
