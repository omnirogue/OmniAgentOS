-- Gate evidence log for queryable, hash-addressed verification evidence.
-- This records execution details, artifact paths, and metadata for gate runs.
CREATE TABLE gate_evidence (
    id                   TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    run_id               TEXT,
    task_id              TEXT,
    attempt_ref          TEXT,
    gate_name            TEXT NOT NULL,
    command              TEXT NOT NULL,
    exit_code            INTEGER,
    duration_ms          REAL,
    output_artifact_path TEXT,
    output_sha256        TEXT,
    test_counts_json     TEXT,
    commit_sha           TEXT,
    env_digest           TEXT
);
CREATE INDEX idx_gate_evidence_created ON gate_evidence(created_at);
CREATE INDEX idx_gate_evidence_run ON gate_evidence(run_id);
