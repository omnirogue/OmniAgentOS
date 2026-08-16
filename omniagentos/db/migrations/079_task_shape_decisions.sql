-- TASK-SHAPE ROUTING DECISION TELEMETRY (PKG-TASK-SHAPE-ROUTING).
-- Every route/topology decision made by the planner/router appends one row here.
-- This provides the fine-tune corpus and observation stream for learning loops.
-- Additive and best-effort: record_decision swallows every error so telemetry
-- can never break planning.
CREATE TABLE task_shape_decisions (
    id                      TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    board_task_id           TEXT,                      -- nullable
    run_id                  TEXT,                      -- nullable
    brief_hash              TEXT NOT NULL,
    config_version          TEXT,
    feat_d                  REAL,
    feat_i                  REAL,
    feat_s                  REAL,
    feat_u                  REAL,
    feat_v                  REAL,
    feat_g                  REAL,
    feat_c                  REAL,
    feat_m                  REAL,
    feat_r                  REAL,
    feat_k                  REAL,
    feat_w                  REAL,
    feat_p                  REAL,
    confidence              REAL,
    task_class              TEXT,                      -- JSON array of strings
    tool_density            REAL,                      -- derived; NULL when not cheaply computable
    context_breadth         REAL,                      -- derived; nullable
    merge_cost              REAL,                      -- derived; nullable
    shared_state_coupling   REAL,                      -- derived; nullable
    route                   TEXT NOT NULL,             -- solo_strong|centralized_team|parallel_review
    topology                TEXT,                      -- sequential|map_reduce|generator_critic|specialist_panel|hierarchical
    worker_count            INTEGER,
    rationale               TEXT NOT NULL DEFAULT '',
    applied                 INTEGER NOT NULL DEFAULT 0, -- 1 when the route actually changed the plan
    latency_ms              REAL,
    cbm_parallel_candidates INTEGER                   -- advisory CBM probe signal; nullable
);

CREATE INDEX idx_task_shape_decisions_created ON task_shape_decisions(created_at);
