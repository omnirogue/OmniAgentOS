# PLAN2-TASK — M1 Chat backend (branch plan2/m1-chat-backend)
Full spec: ~/Desktop/GrokOmni-Upgrade-Plans-20260726/PLAN-2-LONGER-TERM.md (M1 + D-A/D-B).
Mission: chats as first-class objects with hidden companion board tasks + org-linked folder projects.
OWN: omniagentos/db/migrations/073_chat_workspaces.sql (RESERVED: 073), omniagentos/chats/ (new pkg), omniagentos/api/routes/chats.py (new, register in api/main.py), omniagentos/projects/store.py (ensure_org_folder_projects), omniagentos/conversations/store.py ('chat' scope), omniagentos/api/routes/intake.py (live_board origin filter ONLY), tests for all.
Key reuse: omniagentos/memory/runner_hook.py (safe_memory_block/safe_persist_*), intake dispatch_spec(execute="session"), hierarchy.py patterns.
Schema: chats(id,project_id,board_task_id,title,status,promoted_at,meta_json) + board_tasks.origin 'board'|'chat' + projects.org_company_id/org_product_id.
DO NOT touch: dashboard/ (M2 owns UI), sessions.py, formations/preflight.
Acceptance: POST /api/chats + /messages runs a solo agent turn on the companion task; /api/board excludes origin='chat'; uv run pytest -q tests/chats tests/swarm green.
