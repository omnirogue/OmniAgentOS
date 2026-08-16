-- Multidimensional organization: controlled taxonomy + per-object dimensions.
-- Source: OmniAgentOS_Multidimensional_Project_Organization_UI_Upgrade_Plan.docx
-- Additive only. Existing board/skills/agents load unchanged.

-- Per-object organization + classification blob (board tasks first).
ALTER TABLE board_tasks ADD COLUMN org_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS org_companies (
    id              TEXT PRIMARY KEY,          -- co_...
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_products (
    id              TEXT PRIMARY KEY,          -- prd_...
    company_id      TEXT NOT NULL,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    UNIQUE (company_id, slug),
    FOREIGN KEY (company_id) REFERENCES org_companies(id)
);

CREATE TABLE IF NOT EXISTS org_taxonomy_values (
    id              TEXT PRIMARY KEY,
    dimension       TEXT NOT NULL,             -- workstream|domain|channel|lifecycle|risk_class|priority
    slug            TEXT NOT NULL,
    label           TEXT NOT NULL,
    parent_slug     TEXT,                      -- optional hierarchy within dimension
    aliases_json    TEXT NOT NULL DEFAULT '[]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE (dimension, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_tax_dim ON org_taxonomy_values(dimension, enabled);

CREATE TABLE IF NOT EXISTS org_classifications (
    id              TEXT PRIMARY KEY,          -- ocl_...
    object_type     TEXT NOT NULL,             -- board_task|skill|agent|loop
    object_id       TEXT NOT NULL,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    confidence      REAL NOT NULL DEFAULT 0.0,
    source          TEXT NOT NULL DEFAULT 'ai_classification',
    rationale       TEXT NOT NULL DEFAULT '',
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    taxonomy_version INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'applied'
        CHECK (status IN ('suggested', 'provisional', 'applied', 'locked', 'rejected')),
    classifier_agent TEXT,                     -- e.g. grok-orchestrator
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_class_object
    ON org_classifications(object_type, object_id, created_at);

CREATE TABLE IF NOT EXISTS org_agent_profiles (
    id              TEXT PRIMARY KEY,          -- oap_...
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    lineage         TEXT NOT NULL DEFAULT 'cli-grok',
    model           TEXT NOT NULL DEFAULT 'grok-4.5',
    role            TEXT NOT NULL,             -- orchestrator|memory_curator|self_repair|...
    metacog_primary INTEGER NOT NULL DEFAULT 0 CHECK (metacog_primary IN (0, 1)),
    expertise_json  TEXT NOT NULL DEFAULT '[]',
    workstreams_json TEXT NOT NULL DEFAULT '[]',
    risk_ceiling    TEXT NOT NULL DEFAULT 'bounded_external',
    charter         TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
