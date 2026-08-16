/**
 * Wire types for the read-only LoopDeck engine surface, typed against
 * `contracts/loopdeck-engine-api.md`.
 *
 * Scope note: the original control-plane feature also carried
 * portfolio/capacity/project/DAG/lifecycle/gates/leases types. Those belong to
 * the `/api/v1/control-plane` runtime, which this slice deliberately does not
 * touch, so they are absent rather than unused.
 */

export interface EngineCapabilities {
  api_version: string;
  product: string;
  read_only: boolean;
  capabilities: {
    swarm: boolean;
    parallel_execution: boolean;
    execution_enabled: boolean;
    worktree_isolation_enabled: boolean;
    memory: boolean;
    reflection: boolean;
    improvements: boolean;
    activity_cursor: boolean;
  };
  links: {
    create_run: string | null;
    run: string | null;
    activity: string | null;
    snapshot: string | null;
    memory_search: string | null;
    improvements: string | null;
  };
}

/** One row of `GET /api/engine/runs` — the authoritative binding source. */
export interface EngineRunSummary {
  id: string;
  status: string;
  goal?: string | null;
  project_id?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
}

/**
 * `url` is optional and nullable on every evidence entry: the server nulls any
 * link outside `https://github.com/` but keeps the entry, and a reporter may
 * omit the field entirely.
 */
export interface EngineEvidenceCommit {
  sha: string;
  message?: string | null;
  url?: string | null;
  /**
   * True when the backend saw an explicit, non-GitHub url and refused it
   * (distinct from the reporter never supplying a url at all). The client
   * must never derive a fabricated github.com link when this is true.
   */
  url_refused?: boolean;
}

export interface EngineEvidenceFile {
  path: string;
  url?: string | null;
  /**
   * True when the backend saw an explicit, non-GitHub url and refused it
   * (distinct from the reporter never supplying a url at all). The client
   * must never derive a fabricated github.com link when this is true.
   */
  url_refused?: boolean;
}

export interface EngineEvidenceTest {
  name: string;
  status?: string | null;
  url?: string | null;
  /**
   * True when the backend saw an explicit, non-GitHub url and refused it
   * (distinct from the reporter never supplying a url at all). The client
   * must never derive a fabricated github.com link when this is true.
   */
  url_refused?: boolean;
}

export interface EngineEvidenceReport {
  name: string;
  status?: string | null;
  url?: string | null;
  /**
   * True when the backend saw an explicit, non-GitHub url and refused it
   * (distinct from the reporter never supplying a url at all). The client
   * must never derive a fabricated github.com link when this is true.
   */
  url_refused?: boolean;
}

export interface EngineEvidence {
  commits: EngineEvidenceCommit[];
  files: EngineEvidenceFile[];
  tests: EngineEvidenceTest[];
  reports: EngineEvidenceReport[];
}

/**
 * Repository binding; any field the server could not validate arrives null.
 *
 * CP-004: this must track exactly what the real projector emits
 * (`_CONTEXT_FIELDS` in engine.py) -- repository/branch/head_sha only. There
 * is no base_sha: the projector never produces one, so the client must not
 * consume or render a field the backend cannot back up.
 */
export interface EngineRunContext {
  repository: string | null;
  branch: string | null;
  head_sha: string | null;
}

export interface EngineApprovalReceipt {
  reviewer?: string | null;
  run_id?: string | null;
  head_sha?: string | null;
  recorded_at?: string | null;
}

/** Approval is reported, never inferred — `approved` without a receipt is not approval. */
export interface EngineApproval {
  approved: boolean;
  receipt: EngineApprovalReceipt | null;
}

export interface EngineRunSnapshot {
  api_version: string;
  run: {
    id: string;
    status: string;
    goal?: string | null;
    project_id?: string | null;
    created_at?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
    error?: string | null;
  };
  tasks: Array<{
    id: string;
    task_key?: string | null;
    title?: string | null;
    description?: string | null;
    status?: string | null;
    parent_task_id?: string | null;
  }>;
  deps: Array<{ task_id: string; depends_on_task_id: string }>;
  attempts: Record<
    string,
    Array<{
      id: string;
      board_task_id?: string | null;
      status?: string | null;
      provider?: string | null;
      model?: string | null;
      session_id?: string | null;
      started_at?: string | null;
      finished_at?: string | null;
      end_reason?: string | null;
    }>
  >;
  progress: Record<string, number>;
  metrics: Record<string, number>;
  activity: Array<{
    id: number;
    action?: string | null;
    created_at?: string | null;
    target_type?: string | null;
    target_id?: string | null;
    payload: Record<string, unknown>;
  }>;
  next_activity_cursor: number;
  artifacts: Array<{
    id: string;
    artifact_type?: string | null;
    run_id?: string | null;
    task_id?: string | null;
    attempt_id?: string | null;
    format?: string | null;
    schema_version?: string | null;
    content_hash?: string | null;
    quality?: string | null;
    created_at?: string | null;
  }>;
  context: EngineRunContext;
  evidence: EngineEvidence;
  approval: EngineApproval;
}
