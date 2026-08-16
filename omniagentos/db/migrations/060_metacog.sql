-- Phase 0–6 metacognition backbone: artifacts, checkpoints, typed memory,
-- metacognitive snapshots, strategies, reflection, skill versions.
-- Additive only. Behaviour gated by configs/metacog.yaml (default shadow).

CREATE TABLE IF NOT EXISTS metacog_artifacts (
    id                  TEXT PRIMARY KEY,          -- art_...
    artifact_type       TEXT NOT NULL,
    company_id          TEXT NOT NULL DEFAULT 'default',
    project_id          TEXT,
    run_id              TEXT,
    task_id             TEXT,
    attempt_id          TEXT,
    producer_agent_id   TEXT,
    content_uri         TEXT NOT NULL,
    content_hash        TEXT NOT NULL,             -- sha256 hex
    format              TEXT NOT NULL DEFAULT 'json',
    schema_version      INTEGER NOT NULL DEFAULT 1,
    base_repo           TEXT,
    base_sha            TEXT,
    provenance_json     TEXT NOT NULL DEFAULT '{}',
    quality_json        TEXT NOT NULL DEFAULT '{}',
    retention_class     TEXT NOT NULL DEFAULT 'project'
        CHECK (retention_class IN ('transient', 'project', 'long_term', 'audit')),
    access_scope        TEXT NOT NULL DEFAULT 'task'
        CHECK (access_scope IN ('task', 'project', 'domain', 'company')),
    supersedes_json     TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metacog_artifacts_hash
    ON metacog_artifacts(content_hash, company_id, project_id);
CREATE INDEX IF NOT EXISTS idx_metacog_artifacts_task
    ON metacog_artifacts(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_metacog_artifacts_run
    ON metacog_artifacts(run_id, created_at);

CREATE TABLE IF NOT EXISTS metacog_artifact_edges (
    id              TEXT PRIMARY KEY,
    from_artifact   TEXT NOT NULL,
    to_artifact     TEXT NOT NULL,
    edge_type       TEXT NOT NULL,  -- input | verifies | supersedes | derived_from | deploys
    created_at      TEXT NOT NULL,
    FOREIGN KEY (from_artifact) REFERENCES metacog_artifacts(id),
    FOREIGN KEY (to_artifact) REFERENCES metacog_artifacts(id)
);
CREATE INDEX IF NOT EXISTS idx_metacog_edges_from ON metacog_artifact_edges(from_artifact);
CREATE INDEX IF NOT EXISTS idx_metacog_edges_to ON metacog_artifact_edges(to_artifact);

CREATE TABLE IF NOT EXISTS metacog_checkpoints (
    id                  TEXT PRIMARY KEY,          -- ckp_...
    run_id              TEXT,
    task_id             TEXT,
    attempt_id          TEXT,
    company_id          TEXT NOT NULL DEFAULT 'default',
    project_id          TEXT,
    state_json          TEXT NOT NULL,             -- authoritative recovery packet
    completed_criteria_json TEXT NOT NULL DEFAULT '[]',
    pending_criteria_json   TEXT NOT NULL DEFAULT '[]',
    side_effects_json   TEXT NOT NULL DEFAULT '[]',
    artifact_ids_json   TEXT NOT NULL DEFAULT '[]',
    context_digest      TEXT,
    safe_to_resume      INTEGER NOT NULL DEFAULT 1 CHECK (safe_to_resume IN (0, 1)),
    replay_risk         TEXT NOT NULL DEFAULT 'none'
        CHECK (replay_risk IN ('none', 'low', 'high')),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metacog_ckp_task ON metacog_checkpoints(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_metacog_ckp_run ON metacog_checkpoints(run_id, created_at);

CREATE TABLE IF NOT EXISTS metacog_memory_records (
    id                  TEXT PRIMARY KEY,          -- mem_...
    type                TEXT NOT NULL
        CHECK (type IN ('fact', 'lesson', 'procedure', 'strategy', 'warning', 'benchmark')),
    statement           TEXT NOT NULL,
    applicability_json  TEXT NOT NULL DEFAULT '{}',
    evidence_json       TEXT NOT NULL DEFAULT '[]',  -- artifact ids
    confidence          REAL NOT NULL DEFAULT 0.5,
    sample_count        INTEGER NOT NULL DEFAULT 0,
    success_count       INTEGER NOT NULL DEFAULT 0,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    promotion_status    TEXT NOT NULL DEFAULT 'pending'
        CHECK (promotion_status IN ('pending', 'shadow', 'promoted', 'rejected', 'superseded')),
    valid_from          TEXT,
    expires_at          TEXT,
    invalidated_by_json TEXT NOT NULL DEFAULT '{}',
    supersedes_json     TEXT NOT NULL DEFAULT '[]',
    owner               TEXT NOT NULL DEFAULT 'memory_curator',
    company_id          TEXT NOT NULL DEFAULT 'default',
    project_id          TEXT,
    domain              TEXT,
    helpfulness_score   REAL NOT NULL DEFAULT 0.0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metacog_memory_status
    ON metacog_memory_records(promotion_status, company_id);
CREATE INDEX IF NOT EXISTS idx_metacog_memory_project
    ON metacog_memory_records(project_id, promotion_status);

CREATE TABLE IF NOT EXISTS metacog_memory_retrieval_events (
    id                  TEXT PRIMARY KEY,
    memory_id           TEXT NOT NULL,
    task_id             TEXT,
    run_id              TEXT,
    query               TEXT,
    rank                INTEGER NOT NULL DEFAULT 0,
    selected            INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    helpfulness         REAL,                       -- filled post-hoc
    created_at          TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES metacog_memory_records(id)
);

CREATE TABLE IF NOT EXISTS metacog_snapshots (
    id                  TEXT PRIMARY KEY,          -- mcs_...
    run_id              TEXT,
    task_id             TEXT,
    snapshot_json       TEXT NOT NULL,             -- MetacognitiveState
    next_control_action TEXT NOT NULL,
    reason_codes_json   TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metacog_snap_task ON metacog_snapshots(task_id, created_at);

CREATE TABLE IF NOT EXISTS metacog_decision_records (
    id                  TEXT PRIMARY KEY,          -- mcd_...
    run_id              TEXT,
    task_id             TEXT,
    action              TEXT NOT NULL,
    from_strategy       TEXT,
    to_strategy         TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    reason_codes_json   TEXT NOT NULL DEFAULT '[]',
    mode                TEXT NOT NULL DEFAULT 'shadow',  -- off|shadow|enforce
    applied             INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metacog_strategies (
    id                  TEXT PRIMARY KEY,          -- strategy id / slug
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    topology            TEXT NOT NULL DEFAULT 'solo',
    effort              TEXT NOT NULL DEFAULT 'medium',
    max_fanout          INTEGER NOT NULL DEFAULT 1,
    requires_verifier   INTEGER NOT NULL DEFAULT 0 CHECK (requires_verifier IN (0, 1)),
    eligible_domains_json TEXT NOT NULL DEFAULT '["coding"]',
    performance_json    TEXT NOT NULL DEFAULT '{}',
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metacog_reflection_reports (
    id                  TEXT PRIMARY KEY,          -- rfl_...
    run_id              TEXT,
    task_id             TEXT,
    outcome             TEXT NOT NULL,             -- completed|failed|repaired|incident
    taxonomy            TEXT NOT NULL DEFAULT 'unknown',
    report_json         TEXT NOT NULL,
    candidate_ids_json  TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metacog_skill_versions (
    id                  TEXT PRIMARY KEY,          -- skv_...
    skill_slug          TEXT NOT NULL,
    version             TEXT NOT NULL,
    body_md             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'shadow'
        CHECK (status IN ('draft', 'shadow', 'canary', 'promoted', 'rolled_back')),
    source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    fixtures_json       TEXT NOT NULL DEFAULT '[]',
    metrics_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (skill_slug, version)
);
