# PLAN2-TASK — M4 Memory + artifacts productized (branch plan2/m4-memory-artifacts)
Full spec: Desktop plan M4. DEPENDENCY: `git merge main` after the plan1/agent-context lane lands (metacog memory_promotion=enforce + /memories convention) before finalizing.
OWN: omniagentos/memory/anthropic_tool.py (new — Anthropic memory_20250818 client-side handler, BetaAbstractMemoryTool subclass backed by metacog store; path-traversal guards), session-end harvest hook (sessions completion path -> metacog create_memory_candidate), omniagentos/api/routes/metacog.py (add GET list endpoints for memory + artifacts), omniagentos/chats/service.py compaction (ONLY if M1 merged; else skip), dashboard/src/app/memory/page.tsx + app/artifacts/page.tsx + features/memory/ (new), tests.
Migration: RESERVED 075 if list indexes needed, else none.
Acceptance: candidate appears after a session with learnings; promote via UI; /artifacts previews blobs via existing artifacts_preview routes.
