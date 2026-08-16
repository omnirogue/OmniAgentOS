-- 066_csi_runs.sql
-- Continuous Self-Improvement run ledger (HANDOFF/self-improvement Phase foundation).
-- Separate from scheduler.routines / routine_runs (those are product cron routines).

CREATE TABLE IF NOT EXISTS csi_runs (
    id TEXT PRIMARY KEY,
    routine_id TEXT NOT NULL,
    status TEXT NOT NULL,
    -- ANALYZING | AWAITING_HUMAN | AWAITING_MERGE | NO_CHANGE | DEFERRED |
    -- REJECTED | QUARANTINED | CANCELLED | INCIDENT
    -- (MERGED only after human merge outside CSI; engine never auto-sets MERGED/COMPLETED)
    window_days INTEGER NOT NULL DEFAULT 7,
    codebase_sha TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    synthesis_json TEXT NOT NULL DEFAULT '{}',
    conflict_json TEXT NOT NULL DEFAULT '{}',
    verdict TEXT,                       -- no_change | propose | insufficient_panel | halted
    no_change_reason TEXT,
    improvement_id TEXT,                -- optional link to improvements card
    wall_clock_s REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_csi_runs_routine ON csi_runs(routine_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_csi_runs_status ON csi_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS csi_model_plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES csi_runs(id),
    planner TEXT NOT NULL,              -- grok | sol | kimi | ...
    lineage TEXT,
    status TEXT NOT NULL,               -- ok | timeout | invalid | error
    plan_json TEXT NOT NULL DEFAULT '{}',
    latency_s REAL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_csi_plans_run ON csi_model_plans(run_id);
