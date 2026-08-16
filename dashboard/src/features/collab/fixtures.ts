import type { Agent, BoardTask, Channel, LiveBoardTask, Message } from "./types";

/** Enable locally with NEXT_PUBLIC_USE_COLLAB_FIXTURES=true. */
export const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_COLLAB_FIXTURES === "true";

export const FIXTURE_AGENTS: Agent[] = [
  { id: "agt-1", name: "Claude", lineage: "claude", model: "Claude Opus", expertise: ["prompts", "coding", "analysis"], trust_level: "T4", status: "idle", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
  { id: "agt-2", name: "Codex", lineage: "cli-codex", model: "GPT-5", expertise: ["coding", "testing", "typescript"], trust_level: "T3", status: "busy", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
  { id: "agt-3", name: "Grok", lineage: "cli-grok", model: "Grok", expertise: ["research", "analysis", "writing"], trust_level: "T2", status: "idle", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
];

/**
 * Append a newly-created agent to the in-session fixture roster (mutates the same
 * array `collabApi.agents()` reads from, so /agents and /agents/[id] see it on the
 * next fetch without a real backend). Used by features/capabilities's create-agent
 * flow when NEXT_PUBLIC_USE_COLLAB_FIXTURES is also enabled.
 */
export function pushFixtureAgent(agent: Agent): void {
  FIXTURE_AGENTS.push(agent);
}

export const FIXTURE_TASKS: BoardTask[] = [
  { id: "task-1", title: "Map evaluator regressions", description: "Review the latest benchmark deltas and isolate the highest-impact causes.", required_expertise: ["analysis", "testing"], discipline: "research", priority: "high", status: "open", claimed_by: null, claim_version: 0, result_ref: null, archived: false, created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:00Z" },
  { id: "task-2", title: "Tighten handoff protocol", description: "Document the structured context required for agent-to-agent handoffs.", required_expertise: ["writing", "prompts"], discipline: "operations", priority: "normal", status: "claimed", claimed_by: "agt-3", claim_version: 1, result_ref: null, archived: false, created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:00Z" },
  { id: "task-3", title: "Ship run timeline", description: "Add timeline context to completed run records.", required_expertise: ["typescript"], discipline: "frontend", priority: "urgent", status: "in_progress", claimed_by: "agt-2", claim_version: 1, result_ref: null, archived: false, created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:00Z" },
];

/** Live-board fixtures (GET /api/board) for local dev without a backend.
 * S4 additions: project_id, chat_origin, planner_brief, checklist — exercised
 * by the board's new project filter, provenance badge, plan expand, and
 * checklist progress. task-1 is standalone (no project, no chat origin),
 * task-2 is promoted from a chat on project "proj-1", task-3 is on project
 * "proj-2" with a planner brief and a partial checklist. */
export const FIXTURE_LIVE_TASKS: LiveBoardTask[] = FIXTURE_TASKS.map((task) => ({
  ...task,
  run_id: task.status === "open" ? null : `run-${task.id}`,
  run_state: task.status === "in_progress" ? "running" : task.status === "done" ? "completed" : task.status === "open" ? null : "queued",
  run_agent: task.claimed_by,
  run_progress: task.status === "in_progress" ? { steps_done: 1, steps_total: 3 } : null,
  run_error: null,
  paused_run: task.status === "in_progress" ? `run-${task.id}` : null,
  paused_session: task.status === "in_progress" ? `session-${task.id}` : null,
  project_id: task.id === "task-2" ? "proj-1" : task.id === "task-3" ? "proj-2" : null,
  chat_origin: task.id === "task-2" ? { chat_id: "chat-promoted-2", title: "Tighten handoff protocol" } : null,
  planner_brief: task.id === "task-3" ? "Add timeline context to completed run records. Update the run record schema to include started_at and ended_at. Migrate existing records." : null,
  checklist: task.id === "task-3" ? { done: 2, total: 5 } : task.id === "task-2" ? { done: 3, total: 3 } : null,
}));

export const FIXTURE_CHANNELS: Channel[] = [
  { id: "general", name: "General", kind: "group", members: ["agt-1", "agt-2", "agt-3"], topic: "Coordination and handoffs", created_at: "2026-01-01T00:00:00Z" },
  { id: "broadcast", name: "Mission control", kind: "broadcast", members: [], topic: "System-wide notices", created_at: "2026-01-01T00:00:00Z" },
  { id: "claude-codex", name: "Claude + Codex", kind: "direct", members: ["agt-1", "agt-2"], topic: "Implementation pairing", created_at: "2026-01-01T00:00:00Z" },
];

export const FIXTURE_MESSAGES: Message[] = [
  { id: "msg-1", channel_id: "general", from_agent: "agt-1", to_agent: null, kind: "message", body: "I have mapped the current research queue. The evaluator review is ready to claim.", ref: "task-1", ts: "2026-01-02T11:45:00Z" },
  { id: "msg-2", channel_id: "general", from_agent: "agt-2", to_agent: null, kind: "handoff", body: "Frontend timeline work is underway; I will leave the implementation notes with the run record.", ref: "task-3", ts: "2026-01-02T11:52:00Z" },
  { id: "msg-3", channel_id: "broadcast", from_agent: "system", to_agent: null, kind: "status", body: "Collaboration workspace initialized.", ref: null, ts: "2026-01-02T10:00:00Z" },
];
