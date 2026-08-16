CREATE TABLE IF NOT EXISTS orchestrations (
    id            TEXT PRIMARY KEY,
    board_task_id TEXT,
    working_dir   TEXT,
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','planning','running','completed','failed','cancelled')),
    stage         TEXT,
    error         TEXT,
    heartbeat_at  TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_status ON orchestrations(status);
CREATE INDEX IF NOT EXISTS idx_orch_board ON orchestrations(board_task_id);
