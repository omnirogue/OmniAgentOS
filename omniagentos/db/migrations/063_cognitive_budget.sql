-- Cognitive Budget Manager (CBM): allocations, escalations, outcomes, role stats.
-- LIVE by default. No cost objective — quality + ETAR only.

CREATE TABLE IF NOT EXISTS cbm_allocations (
    id                  TEXT PRIMARY KEY,      -- cba_...
    task_id             TEXT,
    stage               TEXT NOT NULL DEFAULT 'execution',
    required_quality    REAL NOT NULL DEFAULT 0.9,
    model_role          TEXT NOT NULL DEFAULT 'fast_implementer',
    reasoning_effort    TEXT NOT NULL DEFAULT 'low',
    parallel_candidates INTEGER NOT NULL DEFAULT 1,
    context_mode        TEXT NOT NULL DEFAULT 'targeted',
    verification_rung   TEXT NOT NULL DEFAULT 'mechanical_plus_independent',
    max_repairs         INTEGER NOT NULL DEFAULT 1,
    max_replans         INTEGER NOT NULL DEFAULT 1,
    hedge_after_p95_seconds INTEGER,
    escalation_path_json TEXT NOT NULL DEFAULT '[]',
    stop_rule           TEXT NOT NULL DEFAULT 'acceptance_gate_passed',
    evidence_required_json TEXT NOT NULL DEFAULT '[]',
    rationale_json      TEXT NOT NULL DEFAULT '[]',
    predicted_quality   REAL,
    predicted_etar_s    REAL,
    rung                INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'escalated', 'contracted', 'closed')),
    fingerprint_json    TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cbm_alloc_task ON cbm_allocations(task_id, created_at);

CREATE TABLE IF NOT EXISTS cbm_escalations (
    id                  TEXT PRIMARY KEY,      -- cbe_...
    allocation_id       TEXT NOT NULL,
    task_id             TEXT,
    from_rung           INTEGER NOT NULL,
    to_rung             INTEGER NOT NULL,
    trigger_code        TEXT NOT NULL,
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    changed_fields_json TEXT NOT NULL DEFAULT '{}',
    next_gate           TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (allocation_id) REFERENCES cbm_allocations(id)
);
CREATE INDEX IF NOT EXISTS idx_cbm_esc_alloc ON cbm_escalations(allocation_id);

CREATE TABLE IF NOT EXISTS cbm_outcomes (
    id                  TEXT PRIMARY KEY,      -- cbo_...
    allocation_id       TEXT,
    task_id             TEXT,
    fingerprint_json    TEXT NOT NULL DEFAULT '{}',
    model_role          TEXT,
    first_pass_accepted INTEGER NOT NULL DEFAULT 0,
    wall_seconds        REAL,
    repair_count        INTEGER NOT NULL DEFAULT 0,
    verifier_score      REAL,
    escalations         INTEGER NOT NULL DEFAULT 0,
    escaped_defect      INTEGER NOT NULL DEFAULT 0,
    production_outcome  TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cbm_out_role ON cbm_outcomes(model_role, created_at);

CREATE TABLE IF NOT EXISTS cbm_role_stats (
    role                TEXT NOT NULL,
    fingerprint_key     TEXT NOT NULL DEFAULT '',
    samples             INTEGER NOT NULL DEFAULT 0,
    accepts             INTEGER NOT NULL DEFAULT 0,
    sum_wall_s          REAL NOT NULL DEFAULT 0,
    sum_repairs         INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (role, fingerprint_key)
);
