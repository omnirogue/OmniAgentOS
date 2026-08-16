-- W1 Session Bridge repair2 (GROUP E):
-- (1) T-OPS-007: persist killed_by on the session row so it survives a crash
--     between terminalization and the (out-of-transaction) manifest write.
-- (2) T-OPS-003: durable spawn ingress. The API/module spawn() enqueues here; the
--     single long-lived supervisor drains the queue in its serve_forever loop, so
--     spawn requests are owned by the polling runtime instead of an unpolled singleton.
ALTER TABLE sessions ADD COLUMN killed_by TEXT;

CREATE TABLE session_spawn_queue (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    project_dir       TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    budget_usd_max    REAL,
    title             TEXT,
    state             TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'launched', 'failed')),
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_spawn_queue_state ON session_spawn_queue(state);
