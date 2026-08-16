-- Map arbitrary parallel executions (And-Then graphs) instead of linear sequence IDs
CREATE TABLE graph_edges (
    id TEXT PRIMARY KEY,
    parent_step_id INTEGER NOT NULL,
    child_step_id INTEGER NOT NULL,
    dependency_type VARCHAR(64) DEFAULT 'sequential',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_step_id) REFERENCES steps(id) ON DELETE CASCADE,
    FOREIGN KEY (child_step_id) REFERENCES steps(id) ON DELETE CASCADE
);
CREATE INDEX idx_graph_edges_parent ON graph_edges(parent_step_id);
CREATE INDEX idx_graph_edges_child ON graph_edges(child_step_id);

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
