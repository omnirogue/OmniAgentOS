-- Swarm Graph V2: typed nodes, artifact-carrying edges, templates.
-- Additive. V1 swarm plans keep working unchanged.

CREATE TABLE IF NOT EXISTS graph_templates (
    id              TEXT PRIMARY KEY,          -- gtp_...
    slug            TEXT NOT NULL UNIQUE,
    version         TEXT NOT NULL DEFAULT '1.0.0',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    body_json       TEXT NOT NULL,             -- full template spec
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_runs (
    id              TEXT PRIMARY KEY,          -- grn_...
    template_id     TEXT,
    template_slug   TEXT,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planning'
        CHECK (status IN (
            'planning', 'running', 'blocked', 'completed',
            'failed', 'cancelled', 'partial'
        )),
    completeness_policy TEXT NOT NULL DEFAULT 'fail_closed'
        CHECK (completeness_policy IN ('fail_closed', 'allow_partial')),
    plan_json       TEXT NOT NULL DEFAULT '{}',
    critical_path_json TEXT NOT NULL DEFAULT '[]',
    cost_usd        REAL NOT NULL DEFAULT 0,
    swarm_run_id    TEXT,                      -- optional link to swarm V1
    company_id      TEXT,
    project_id      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_runs_status ON graph_runs(status, created_at);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id              TEXT PRIMARY KEY,          -- gnd_...
    graph_run_id    TEXT NOT NULL,
    key             TEXT NOT NULL,             -- stable within run
    node_type       TEXT NOT NULL
        CHECK (node_type IN (
            'worker', 'reduce', 'verify', 'synthesize',
            'anchor', 'human_gate', 'fan_out'
        )),
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'ready', 'running', 'completed',
            'failed', 'blocked', 'skipped', 'cancelled'
        )),
    input_ports_json  TEXT NOT NULL DEFAULT '{}',
    output_ports_json TEXT NOT NULL DEFAULT '{}',
    input_bindings_json TEXT NOT NULL DEFAULT '{}',  -- port -> artifact_id
    output_artifacts_json TEXT NOT NULL DEFAULT '{}', -- port -> artifact_id
    resource_claims_json TEXT NOT NULL DEFAULT '[]',
    verify_policy_json TEXT NOT NULL DEFAULT '{}',
    model_role      TEXT,
    effort          TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,
    board_task_id   TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (graph_run_id, key),
    FOREIGN KEY (graph_run_id) REFERENCES graph_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_run ON graph_nodes(graph_run_id, status);

CREATE TABLE IF NOT EXISTS graph_edges (
    id              TEXT PRIMARY KEY,          -- ged_...
    graph_run_id    TEXT NOT NULL,
    from_node_key   TEXT NOT NULL,
    to_node_key     TEXT NOT NULL,
    from_port       TEXT NOT NULL DEFAULT 'output',
    to_port         TEXT NOT NULL DEFAULT 'input',
    artifact_type   TEXT,
    required        INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    edge_kind       TEXT NOT NULL DEFAULT 'data'
        CHECK (edge_kind IN ('data', 'resource', 'safety')),
    created_at      TEXT NOT NULL,
    UNIQUE (graph_run_id, from_node_key, to_node_key, from_port, to_port),
    FOREIGN KEY (graph_run_id) REFERENCES graph_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_run ON graph_edges(graph_run_id);

CREATE TABLE IF NOT EXISTS graph_artifacts (
    id              TEXT PRIMARY KEY,          -- gar_...
    graph_run_id    TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    port            TEXT NOT NULL,
    artifact_type   TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    content_uri     TEXT NOT NULL,
    schema_ok       INTEGER NOT NULL DEFAULT 1 CHECK (schema_ok IN (0, 1)),
    verified        INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    metacog_art_id  TEXT,                      -- optional link to metacog_artifacts
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (graph_run_id) REFERENCES graph_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_graph_arts_run ON graph_artifacts(graph_run_id, node_key);
