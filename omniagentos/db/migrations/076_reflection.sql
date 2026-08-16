-- 076_reflection.sql
-- Reserved migration 076 for reflection runs, proposals, and outcomes.
-- Proposal lifecycle promotion_status CHECK constraint matches metacog memory record lifecycle.

CREATE TABLE IF NOT EXISTS reflection_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    sources_read TEXT,                     -- JSON list of sources parsed
    bytes_read INTEGER NOT NULL DEFAULT 0,
    caps_hit TEXT                          -- JSON list of caps exceeded
);

CREATE TABLE IF NOT EXISTS reflection_proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES reflection_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'model_config', 'formation', 'router_weight', 'effort_override',
        'category_pin', 'brief_template', 'lesson', 'skill', 'rollback'
    )),
    target_json TEXT NOT NULL,             -- JSON representing target config location/doc
    current TEXT NOT NULL,
    proposed TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]', -- JSON array of source references
    predicted_impact TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK (risk_class IN ('low', 'medium', 'high')),
    promotion_status TEXT NOT NULL DEFAULT 'pending' CHECK (promotion_status IN (
        'pending', 'shadow', 'promoted', 'rejected', 'superseded'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_proposals_run ON reflection_proposals(run_id);
CREATE INDEX IF NOT EXISTS idx_reflection_proposals_status ON reflection_proposals(promotion_status);

CREATE TABLE IF NOT EXISTS reflection_outcomes (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES reflection_proposals(id) ON DELETE CASCADE,
    applied_at TEXT NOT NULL,
    applied_by TEXT NOT NULL DEFAULT 'reflection-loop',
    rollback_proposal_id TEXT,             -- Nullable reference to proposal that triggered rollback
    outcome_status TEXT NOT NULL CHECK (outcome_status IN ('success', 'failure', 'rolled_back', 'pending_evaluation')),
    metrics_delta_json TEXT,               -- JSON of realized delta metrics
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflection_outcomes_proposal ON reflection_outcomes(proposal_id);
