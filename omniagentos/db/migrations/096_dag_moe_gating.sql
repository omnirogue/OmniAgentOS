-- Map arbitrary step executions (And-Then graphs) without colliding with the
-- graph-runtime `graph_edges` table owned by migration 062. Migration 096 was
-- never successfully applicable with its original table name because 062 is
-- always applied first.
CREATE TABLE dag_step_edges (
    id TEXT PRIMARY KEY,
    parent_step_id INTEGER NOT NULL,
    child_step_id INTEGER NOT NULL,
    dependency_type VARCHAR(64) DEFAULT 'sequential',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_step_id) REFERENCES steps(id) ON DELETE CASCADE,
    FOREIGN KEY (child_step_id) REFERENCES steps(id) ON DELETE CASCADE
);
CREATE INDEX idx_dag_step_edges_parent ON dag_step_edges(parent_step_id);
CREATE INDEX idx_dag_step_edges_child ON dag_step_edges(child_step_id);

-- Stable LatentMoE & Quantile Balancing Gating Logs
CREATE TABLE moe_gates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent_skill_id VARCHAR(128) NOT NULL,
    routing_weight DECIMAL(5, 4) NOT NULL,
    activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX idx_moe_gates_run ON moe_gates(run_id);
