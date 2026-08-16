# PLAN2-TASK — M2 Terminal workspace (branch plan2/m2-terminal-workspace)
Full spec: Desktop plan M2 + D-C. Mission: OpenHands-grade readable live terminals.
OWN: omniagentos/api/routes/sessions.py (ONLY add GET /{id}/transcript/delta?offset=<byte>, complete-lines-only, rotation guard reset-to-0), dashboard/src/features/chats/TerminalView.tsx + WorkspaceTabs.tsx + ansi.ts (NEW files only — do not edit M1-owned FolderTree/ChatThread/ChatComposer/SessionFollow), dashboard/src/app/chats/page.tsx (wire tabs — small surgical edit), tests.
Behavior: subscribe session.updated via lib/useEventChannel -> fetch delta; visibility-aware poll fallback (lib/pollWhenVisible.ts); tail-follow autoscroll with pause-on-scroll-up + "Jump to latest"; ansiToSpans for raw stdout. NO xterm, NO new deps, NO second EventSource.
NO migration. Acceptance: npm run build clean; delta endpoint unit-tested (offset math, partial lines).
