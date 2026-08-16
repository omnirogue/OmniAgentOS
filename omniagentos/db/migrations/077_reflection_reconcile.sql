-- 077_reflection_reconcile.sql
-- Forward-only reconciliation of the reflection schema after the three-lane
-- integration (R1 harvest / R2 propose-apply / R3 runner). 076 shipped the R1
-- draft and was applied before the consumers (R2/R3) landed with a different
-- contract; per the append-only rule 076 stays byte-identical and this
-- migration moves the tables to the reconciled shape. The only data present
-- at reconcile time was same-day smoke-run rows (2026-07-26) — dropped.

DROP TABLE IF EXISTS reflection_outcomes;
DROP TABLE IF EXISTS reflection_proposals;
DROP TABLE IF EXISTS reflection_runs;

CREATE TABLE reflection_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    sources_read TEXT,                     -- JSON list of sources parsed (harvest)
    bytes_read INTEGER NOT NULL DEFAULT 0,
    caps_hit TEXT,                         -- JSON list of caps exceeded (harvest)
    harvest_status TEXT,                   -- per-stage statuses (runner)
    propose_status TEXT,
    validate_status TEXT,
    apply_status TEXT,
    report_status TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE reflection_proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT,                           -- provenance; nullable (proposer may run standalone)
    kind TEXT NOT NULL CHECK (kind IN (
        'model_config', 'formation', 'router_weight', 'effort_override',
        'category_pin', 'brief_template', 'lesson', 'skill', 'rollback'
    )),
    target TEXT NOT NULL,                  -- file/key or doc target (stringified)
    current TEXT NOT NULL,
    proposed TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    predicted_impact TEXT,
    risk_class TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'shadow', 'promoted', 'rejected', 'superseded'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_reflection_proposals_status2 ON reflection_proposals(status);
CREATE INDEX idx_reflection_proposals_run2 ON reflection_proposals(run_id);

CREATE TABLE reflection_outcomes (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    outcome_metrics_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'monitored' CHECK (status IN ('monitored', 'success', 'regression')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES reflection_proposals (id) ON DELETE CASCADE
);
CREATE INDEX idx_reflection_outcomes_proposal2 ON reflection_outcomes(proposal_id);
