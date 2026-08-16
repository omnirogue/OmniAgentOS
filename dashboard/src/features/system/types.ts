/**
 * Types for the System transparency surface — mirror of the JSON returned by
 * omniagentos/api/routes/system.py. Every object here is a reflection of an
 * editable file on disk (an agent def, a SKILL.md, a launchd plist, the
 * improvement log); nothing is server-invented state.
 */

export type SystemMap = {
  archi_md: string | null;
  archi_path: string;
  diagram_mmd: string | null;
  diagram_md: string | null;
  diagram_path: string;
  generated_at: string | null;
};

export type SystemAgent = {
  name: string;
  model: string | null;
  effort: string | null;
  category: string;
  delegated_model: string | null;
  delegated_model_editable: boolean;
  tools: string[];
  tools_count: number;
  description: string;
  body_md: string;
  path: string;
  last_activated: string | null;
  last_activated_source: string | null;
};

export type AgentsResponse = {
  agents: SystemAgent[];
  categories: string[];
  agents_dir: string;
};

export type SystemSkill = {
  name: string;
  description: string;
  category: string;
  path: string;
};

export type SkillsResponse = {
  skills: SystemSkill[];
  categories: string[];
  skills_dir: string;
};

export type ImproverKind = "improver" | "guard";

export type ImproverLoop = {
  label: string;
  suffix: string;
  kind: ImproverKind;
  editable: boolean;
  schedule_human: string;
  next_fire: string | null;
  script_path: string | null;
  prompt_path: string | null;
  prompt_md: string | null;
  log_path: string | null;
  last_log_tail: string[];
  last_run_guess: string | null;
  recent_activity: ImprovementEntry[];
};

export type PatchImproverPromptResult = {
  label: string;
  prompt_path: string;
  committed: boolean;
  bytes: number;
};

/** Max prompt size accepted by PATCH /api/system/improvers/{label}/prompt (64KB). */
export const PROMPT_MAX_BYTES = 64 * 1024;

export type ImprovementChange = {
  path?: string;
  kind?: string;
  summary?: string;
};

export type ImprovementEntry = {
  ts?: string;
  improver?: string;
  account_used?: string;
  changes?: ImprovementChange[];
  notes?: string;
  report_path?: string;
};

export type CuratorReport = {
  filename: string;
  path: string;
  first_lines: string[];
};

export type ImproversResponse = {
  loops: ImproverLoop[];
  improvement_log: ImprovementEntry[];
  curator_reports: CuratorReport[];
  optimizer_playbook_tail: string | null;
  improvement_log_path: string;
};

export type ActivityRow = {
  ts: string;
  source: "session" | "attempt" | "improvement";
  model?: string | null;
  provider?: string;
  effort?: string | null;
  status?: string;
  summary?: string;
  ref_id?: string;
  improver?: string | null;
  change_count?: number;
};

export type ActivityResponse = { activity: ActivityRow[] };

export type PatchAgentBody = {
  model?: string;
  effort?: string;
  category?: string;
  delegated_model?: string;
};

export type PatchAgentResult = {
  agent: SystemAgent;
  committed: boolean;
  changed: string[];
};

export const MODEL_FAMILIES = ["haiku", "sonnet", "opus", "fable"] as const;
export const EFFORTS = ["low", "medium", "high", "xhigh"] as const;
