import type { Approval, Session } from "../../lib/contracts";

/** Enable locally with NEXT_PUBLIC_USE_SESSIONS_FIXTURES=true. */
export const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_SESSIONS_FIXTURES === "true";

const now = new Date();
const minutesAgo = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString();

export const FIXTURE_SESSIONS: Session[] = [
  { id: "ses_bridge_live", source: "bridge", project_dir: "/workspace/omniagentos", provider: "claude", state: "running", model: "claude-opus-4-1", title: "Implement dashboard observability", cost_usd: 1.42, last_activity_at: minutesAgo(1), created_at: minutesAgo(37), approvals_requested: 1, approvals_granted: 1, approvals_denied: 0, company: "AcmeUni", agent_name: "sol-coder", agent_status: "busy", agent_profile: "acmeuni-lead" },
  { id: "ses_bridge_waiting", source: "bridge", project_dir: "/workspace/omniagentos", provider: "claude", state: "awaiting_approval", model: "claude-sonnet-4", title: "Migrate approval audit trail", cost_usd: 0.68, last_activity_at: minutesAgo(4), created_at: minutesAgo(19), approvals_requested: 1, approvals_granted: 0, approvals_denied: 0, company: "Globex", agent_name: "terra-coder", agent_status: "idle", agent_profile: "globex-ops", attention_state: "needs_input", attention_reason: "Approval requested", attention_since: minutesAgo(4) },
  { id: "ses_external_done", source: "external", project_dir: "/workspace/marketing-site", provider: "claude", state: "completed", model: "claude-sonnet-4", title: "Review deployment notes", cost_usd: 0.31, last_activity_at: minutesAgo(42), created_at: minutesAgo(64), approvals_requested: 0, approvals_granted: 0, approvals_denied: 0, attention_state: "finished" },
  { id: "ses_bridge_killed", source: "bridge", project_dir: "/workspace/sandbox", provider: "claude", state: "killed", model: "claude-haiku-3-5", title: "Retry data fixture", cost_usd: 0.07, last_activity_at: minutesAgo(95), created_at: minutesAgo(102), approvals_requested: 2, approvals_granted: 0, approvals_denied: 1, company: "Ops" },
];

export const FIXTURE_APPROVALS: Approval[] = [
  { id: "apr_session_write", run_id: null, session_id: "ses_bridge_waiting", task_id: null, step_seq: null, action_class: "consequential", proposed_action: "Write", params_json: '{"file_path":"/workspace/omniagentos/config/policy.yaml","content":"..."}', risk: "Changes production approval policy.", evidence: "Claude Code PreToolUse hook", state: "pending", decided_by: null, decision_note: null, decided_at: null, expires_at: null, created_at: minutesAgo(4) },
];

export function decideFixtureApproval(id: string, decision: "approved" | "rejected"): Approval | undefined {
  const approval = FIXTURE_APPROVALS.find((item) => item.id === id);
  if (!approval) return undefined;
  approval.state = decision;
  approval.decided_by = "operator";
  approval.decided_at = new Date().toISOString();
  return approval;
}

export function killFixtureSession(id: string): Session | undefined {
  const session = FIXTURE_SESSIONS.find((item) => item.id === id);
  if (!session) return undefined;
  session.state = "killed";
  session.last_activity_at = new Date().toISOString();
  return session;
}

export function updateFixtureSession(id: string, patch: { title?: string; company?: string | null }): Session | undefined {
  const session = FIXTURE_SESSIONS.find((item) => item.id === id);
  if (!session) return undefined;
  if (patch.title !== undefined) session.title = patch.title;
  if (patch.company !== undefined) session.company_override = patch.company;
  return session;
}
