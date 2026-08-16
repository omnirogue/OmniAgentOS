/**
 * Offline fixtures for the project-hierarchy surface. Used when the backend is
 * unreachable (the tree/conversation/activity hooks fall back to these so the
 * views still render and demo cleanly), or forced on with
 * NEXT_PUBLIC_USE_PROJECTS_FIXTURES=true.
 */

import type {
  ActivityEntry,
  ConversationMessage,
  ConversationScope,
  ProjectTreeNode,
} from "./hierarchy";

export const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_PROJECTS_FIXTURES === "true";

const now = Date.now();
const ago = (min: number) => new Date(now - min * 60_000).toISOString();

export const FIXTURE_TREE: ProjectTreeNode[] = [
  {
    project: { id: "prj_growth", name: "Growth Engine", parent_project_id: null },
    status: "running",
    sub_projects: [
      {
        project: { id: "prj_seo", name: "SEO Content", parent_project_id: "prj_growth" },
        status: "running",
        sub_projects: [],
        tasks: [
          { id: "tsk_seo_audit", title: "Audit top 50 landing pages", state: "completed" },
          { id: "tsk_seo_brief", title: "Draft content briefs", state: "running" },
          { id: "tsk_seo_schema", title: "Add schema markup", state: "queued" },
        ],
      },
      {
        project: { id: "prj_ads", name: "Paid Acquisition", parent_project_id: "prj_growth" },
        status: "paused",
        sub_projects: [],
        tasks: [
          { id: "tsk_ads_creative", title: "Refresh ad creative", state: "awaiting_approval" },
          { id: "tsk_ads_budget", title: "Rebalance channel budget", state: "paused" },
        ],
      },
    ],
    tasks: [{ id: "tsk_growth_okr", title: "Set Q3 growth OKRs", state: "completed" }],
  },
  {
    project: { id: "prj_platform", name: "Platform Reliability", parent_project_id: null },
    status: "running",
    sub_projects: [
      {
        project: { id: "prj_obs", name: "Observability", parent_project_id: "prj_platform" },
        status: "completed",
        sub_projects: [],
        tasks: [
          { id: "tsk_obs_traces", title: "Wire trace IDs end-to-end", state: "completed" },
          { id: "tsk_obs_dash", title: "Ship SLO dashboard", state: "completed" },
        ],
      },
    ],
    tasks: [
      { id: "tsk_plat_migrate", title: "Migrate durable runner to 031", state: "running" },
      { id: "tsk_plat_backfill", title: "Backfill conversation history", state: "failed" },
    ],
  },
  {
    project: { id: "prj_research", name: "Research Sandbox", parent_project_id: null },
    status: "draft",
    sub_projects: [],
    tasks: [],
  },
];

const CONVERSATIONS: Record<string, ConversationMessage[]> = {
  "project:prj_growth": [
    msg("project", "prj_growth", 1, "user", "Kick off the growth engine — focus on organic first."),
    msg("project", "prj_growth", 2, "agent", "Understood. I've scoped two sub-projects: SEO Content and Paid Acquisition, and set Q3 OKRs. Organic is the priority lane.", "fable"),
  ],
  "project:prj_seo": [
    msg("project", "prj_seo", 1, "agent", "Landing-page audit is complete — 12 pages need on-page fixes. Drafting briefs next.", "fable"),
  ],
  "task:tsk_seo_brief": [
    msg("task", "tsk_seo_brief", 1, "user", "Prioritise the money pages for the briefs."),
    msg("task", "tsk_seo_brief", 2, "agent", "Working through pricing, integrations and comparison pages first. Two briefs done, three to go.", "fable"),
  ],
  "task:tsk_plat_backfill": [
    msg("task", "tsk_plat_backfill", 1, "agent", "Backfill hit a unique-constraint conflict on (scope_type, scope_id, seq). Paused for review.", "fable"),
    msg("task", "tsk_plat_backfill", 2, "system", "Run failed — see activity for the step trace."),
  ],
};

function msg(
  scope_type: ConversationScope,
  scope_id: string,
  seq: number,
  role: ConversationMessage["role"],
  content: string,
  model: string | null = null,
): ConversationMessage {
  return {
    id: `cnv_${scope_id}_${seq}`,
    scope_type,
    scope_id,
    seq,
    role,
    content,
    model,
    created_at: ago(120 - seq * 8),
    meta_json: "{}",
  };
}

export function fixtureConversation(
  scope: ConversationScope,
  id: string,
): ConversationMessage[] {
  return CONVERSATIONS[`${scope}:${id}`] ?? [];
}

/** Append to an in-memory fixture thread so the composer works fully offline. */
export function appendFixtureMessage(
  scope: ConversationScope,
  id: string,
  role: ConversationMessage["role"],
  content: string,
  model: string | null,
): ConversationMessage {
  const key = `${scope}:${id}`;
  const thread = (CONVERSATIONS[key] ??= []);
  const next = msg(scope, id, thread.length + 1, role, content, model);
  next.created_at = new Date().toISOString();
  thread.push(next);
  return next;
}

export function fixtureActivity(projectId: string): ActivityEntry[] {
  const base: ActivityEntry[] = [
    { id: `${projectId}-a1`, ts: ago(3), kind: "run", title: "Run completed", detail: "fable · planner", state: "completed" },
    { id: `${projectId}-a2`, ts: ago(18), kind: "step.updated", title: "Validation passed", detail: "npm run build", state: "completed" },
    { id: `${projectId}-a3`, ts: ago(41), kind: "run", title: "Run started", detail: "cli-claude", state: "running" },
    { id: `${projectId}-a4`, ts: ago(96), kind: "approval.decided", title: "Approval granted", detail: "operator", state: null },
  ];
  return base;
}
